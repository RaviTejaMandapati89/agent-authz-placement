"""
Arm A: policy in the system prompt, no other enforcement.

The system prompt contains the agent's identity and sections 1 and 2 of
policy.md (the setting and the rules). No checks are performed outside the
model's own judgment.
"""
import datetime
import pathlib
import re
from typing import Any

from dotenv import load_dotenv
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

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


def build_system_prompt(agent_name: str, policy_change: dict | None = None, user: str = "") -> str:
    """
    Build the system prompt for the named agent.

    If policy_change contains expense_limit, the limit in rule P2 is rewritten
    in-place so the prompt contains exactly one limit value.
    """
    ident = identity_paragraph(agent_name, user)
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


def run(
    agent_name: str,
    user: str,
    turns: list[str],
    mcp_url: str,
    run_id: str | None = None,
    between_turns_fn: Any = None,
) -> tuple[str, dict]:
    """
    Run a multi-turn conversation and return (final_reply, usage).

    usage has keys input_tokens, output_tokens (both ints or None).
    """
    model = BedrockModel(
        model_id=MODEL_ID,
        region_name=AWS_REGION,
        temperature=TEMPERATURE,
    )
    system_prompt = build_system_prompt(agent_name, _current_policy_change, user)
    headers = {"X-User": user, "X-Agent": agent_name}

    final_reply = ""
    total_input = 0
    total_output = 0
    turns_data: list[dict] = []

    mcp_client = MCPClient(url=mcp_url, headers=headers)
    with mcp_client:
        tools = list(mcp_client.list_tools_sync())
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
