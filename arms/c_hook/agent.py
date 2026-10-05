"""
Arm C: policy enforced by a BeforeToolCallEvent hook, not the system prompt.

The system prompt identifies the agent and who it acts for. No policy rules are
included. All P1–P7 checks run in the hook before each tool call.
"""
import datetime
import json
import pathlib
import uuid
from typing import Any

from dotenv import load_dotenv
from strands import Agent, tool
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

import httpx

from arms.c_hook.hook import PolicyHook, _load_config
from domain.identity import identity_paragraph
from runner.config import AWS_REGION, MODEL_ID, TEMPERATURE

load_dotenv()

# Module-level state for apply_policy_change; reset between runs by the runner.
_current_policy_override: dict = {}
_configs_affected: int = 0


def apply_policy_change(change: dict) -> None:
    """Store a policy override in memory for the duration of the next run.

    Updates the expense_limit in-memory for every agent config that declares one.
    Never writes to disk. Records how many configs were affected.
    """
    global _current_policy_override, _configs_affected
    _current_policy_override = change or {}
    _configs_affected = 0
    if "expense_limit" in _current_policy_override:
        for agent_name in ("expense-assistant", "travel-assistant", "payments-agent"):
            cfg = _load_config(agent_name)
            if "expense_limit" in cfg:
                _configs_affected += 1


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


def run(
    agent_name: str,
    user: str,
    turns: list[str],
    mcp_url: str,
    run_id: str | None = None,
    between_turns_fn=None,
    bearer_token: str = "",
    config_dir: pathlib.Path | None = None,
    model: Any = None,
    use_gateway: bool = False,
) -> tuple[str, dict, list[dict]]:
    """Run a multi-turn conversation and return (final_reply, usage, turns_data)."""
    if model is None:
        model = BedrockModel(
            model_id=MODEL_ID,
            region_name=AWS_REGION,
            temperature=TEMPERATURE,
        )
    if not hasattr(model, "stateful"):
        model.stateful = False
    system_prompt = identity_paragraph(agent_name, user)
    headers = {"Authorization": f"Bearer {bearer_token}"}

    base_url = mcp_url.rsplit("/mcp", 1)[0]
    effective_mcp_url = f"{base_url}/gateway/mcp" if use_gateway else mcp_url

    limit_override: int | None = None
    if "expense_limit" in _current_policy_override:
        cfg = _load_config(agent_name)
        if "expense_limit" in cfg:
            limit_override = int(_current_policy_override["expense_limit"])

    hook = PolicyHook(
        agent_name=agent_name,
        user=user,
        base_url=base_url,
        run_id=run_id,
        limit_override=limit_override,
        config_dir=config_dir,
        bearer_token=bearer_token,
    )

    final_reply = ""
    total_input = 0
    total_output = 0
    turns_data: list[dict] = []

    ask_tool = _make_ask_payments_agent(base_url, bearer_token, use_gateway)

    mcp_client = MCPClient(url=effective_mcp_url, headers=headers)
    with mcp_client:
        tools = list(mcp_client.list_tools_sync()) + [ask_tool]
        agent = Agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            hooks=[hook],
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
