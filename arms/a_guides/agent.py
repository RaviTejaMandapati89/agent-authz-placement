"""
Arm A: policy in the system prompt, no other enforcement.

The system prompt contains the agent's identity and sections 1 and 2 of
policy.md (the setting and the rules). No checks are performed outside the
model's own judgment.
"""
import datetime
import json
import pathlib
import re
import uuid
from typing import Any

from dotenv import load_dotenv
from strands import Agent, tool
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

import httpx

from domain.identity import identity_paragraph
from runner.config import AWS_REGION, MODEL_ID, TEMPERATURE

load_dotenv()

_POLICY_PATH = pathlib.Path(__file__).parent.parent.parent / "policy.md"

_FORBIDDEN_IN_PROMPT = ("experiment", "arm", "kyc", "prereg")

_policy_sections_text: str | None = None
_current_policy_change: dict = {}


def _load_policy_sections() -> str:
    """
    Extract sections 1 and 2 from policy.md, removing the paragraph that
    begins "Identity is taken as given" (which references the experiment and
    external repos and must not appear in the agent's prompt).
    """
    global _policy_sections_text
    if _policy_sections_text is not None:
        return _policy_sections_text

    text = _POLICY_PATH.read_text(encoding="utf-8")
    lines = text.splitlines()
    result = []
    in_section = False
    for line in lines:
        m = re.match(r"^## (\d+)\.", line)
        if m:
            num = int(m.group(1))
            in_section = num in (1, 2)
        if in_section:
            result.append(line)

    # Split by blank lines into paragraph blocks, drop the Identity paragraph.
    raw = "\n".join(result)
    blocks = [
        b for b in raw.split("\n\n")
        if not b.strip().startswith("Identity is taken as given")
    ]
    _policy_sections_text = "\n\n".join(blocks).strip()
    return _policy_sections_text


def apply_policy_change(change: dict) -> None:
    """Store a policy override to be reflected in the next agent's system prompt."""
    global _current_policy_change
    _current_policy_change = change or {}


def build_system_prompt(agent_name: str, policy_change: dict | None = None, user: str = "",
                        rules_in_prompt: bool = True) -> str:
    """
    Build the system prompt for the named agent.

    With rules_in_prompt False the prompt is the identity paragraph alone, the
    same prompt approaches 2 and 3 use.

    If policy_change contains expense_limit, the limit in rule P2 is rewritten
    in-place so the prompt contains exactly one limit value.
    """
    ident = identity_paragraph(agent_name, user)
    if not rules_in_prompt:
        return ident
    policy = _load_policy_sections()

    if policy_change and "expense_limit" in policy_change:
        new_limit = policy_change["expense_limit"]
        policy = policy.replace("£500", f"£{new_limit}")

    prompt = f"{ident}\n\n{policy}"

    lower = prompt.lower()
    for word in _FORBIDDEN_IN_PROMPT:
        assert word not in lower, (
            f"system prompt for {agent_name!r} contains forbidden word {word!r}"
        )

    return prompt


def _make_ask_payments_agent(base_url: str, bearer_token: str, use_gateway: bool = False):
    """Create the ask_payments_agent local tool."""
    @tool
    def ask_payments_agent(request: str) -> str:
        """Send a request to payments-agent via A2A."""
        endpoint = "/gateway/a2a/payments-agent" if use_gateway else "/a2a/payments-agent"
        body = {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": "message/send",
            "params": {
                "message": {
                    "messageId": str(uuid.uuid4()),
                    "role": "user",
                    "parts": [{"kind": "text", "text": request}],
                },
            },
        }
        resp = httpx.post(
            f"{base_url}{endpoint}",
            json=body,
            headers={"Authorization": f"Bearer {bearer_token}", "Content-Type": "application/json"},
            timeout=30.0,
        )
        result = resp.json()
        if "error" in result:
            return result["error"].get("message", str(result["error"]))
        msg = result.get("result", {}).get("message", {})
        parts = msg.get("parts", [])
        texts = [p["text"] for p in parts if p.get("kind") == "text"]
        return " ".join(texts) if texts else json.dumps(result.get("result", {}))

    return ask_payments_agent


def _make_wired_tool(name: str, url: str, headers: dict):
    """A tool the agent's own spec wires in, whether or not the tool list the
    gateway served includes it. Calls go to the same endpoint as every other
    tool call, so the endpoint, not the agent, decides."""
    if name != "pay_vendor":
        raise ValueError(f"no wiring defined for {name!r}")

    @tool(name=name, description="Pay a vendor.")
    def pay_vendor(vendor: str, amount: float, reference: str) -> str:
        """Pay a vendor."""
        arguments = {"vendor": vendor, "amount": amount, "reference": reference}
        base = {"Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2025-11-25", **headers}
        init = httpx.post(url, headers=base, timeout=30.0, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "wired-tool", "version": "0"}}})
        sid = init.headers.get("mcp-session-id")
        call_headers = dict(base)
        if sid:
            call_headers["mcp-session-id"] = sid
        r = httpx.post(url, headers=call_headers, timeout=30.0, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": name, "arguments": arguments}})
        body = r.json().get("result", {})
        text = " ".join(c.get("text", "") for c in body.get("content", []))
        return text or str(body)

    return pay_vendor


def run(
    agent_name: str,
    user: str,
    turns: list[str],
    mcp_url: str,
    run_id: str | None = None,
    between_turns_fn: Any = None,
    bearer_token: str = "",
    model: Any = None,
    use_gateway: bool = False,
    extra_tool_names: list[str] | None = None,
    rules_in_prompt: bool = True,
) -> tuple[str, dict]:
    """
    Run a multi-turn conversation and return (final_reply, usage).

    usage has keys input_tokens, output_tokens (both ints or None).
    """
    if model is None:
        model = BedrockModel(
            model_id=MODEL_ID,
            region_name=AWS_REGION,
            temperature=TEMPERATURE,
        )
    if not hasattr(model, "stateful"):
        model.stateful = False
    system_prompt = build_system_prompt(agent_name, _current_policy_change, user, rules_in_prompt)
    headers = {"Authorization": f"Bearer {bearer_token}"}

    base_url = mcp_url.rsplit("/mcp", 1)[0]
    effective_mcp_url = f"{base_url}/gateway/mcp" if use_gateway else mcp_url

    final_reply = ""
    total_input = 0
    total_output = 0
    turns_data: list[dict] = []

    ask_tool = _make_ask_payments_agent(base_url, bearer_token, use_gateway)

    mcp_client = MCPClient(url=effective_mcp_url, headers=headers)
    with mcp_client:
        tools = list(mcp_client.list_tools_sync()) + [ask_tool]
        wired = {getattr(t, "tool_name", None) for t in tools}
        for extra in extra_tool_names or []:
            if extra not in wired:
                tools.append(_make_wired_tool(extra, effective_mcp_url, headers))
        agent = Agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            callback_handler=None,
        )
        for i, turn in enumerate(turns):
            ts_start = datetime.datetime.now(datetime.timezone.utc).isoformat()
            result = agent(turn)
            ts_end = datetime.datetime.now(datetime.timezone.utc).isoformat()
            final_reply = str(result)
            usage = result.metrics.accumulated_usage
            total_input = usage["inputTokens"] or 0
            total_output = usage["outputTokens"] or 0
            turns_data.append({
                "user_message": turn,
                "reply": final_reply,
                "ts_start": ts_start,
                "ts_end": ts_end,
            })
            if i < len(turns) - 1 and between_turns_fn:
                between_turns_fn()

    return final_reply, {"input_tokens": total_input, "output_tokens": total_output}, turns_data
