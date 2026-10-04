"""
Arm C: policy enforced by a BeforeToolCallEvent hook, not the system prompt.

The system prompt identifies the agent and who it acts for. No policy rules are
included. All P1–P7 checks run in the hook before each tool call.
"""
import datetime
import pathlib

from dotenv import load_dotenv
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

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


def run(
    agent_name: str,
    user: str,
    turns: list[str],
    mcp_url: str,
    run_id: str | None = None,
    between_turns_fn=None,
) -> tuple[str, dict, list[dict]]:
    """Run a multi-turn conversation and return (final_reply, usage, turns_data)."""
    model = BedrockModel(
        model_id=MODEL_ID,
        region_name=AWS_REGION,
        temperature=TEMPERATURE,
    )
    system_prompt = identity_paragraph(agent_name, user)
    headers = {"X-User": user, "X-Agent": agent_name}

    # Derive the base HTTP URL from the MCP URL (strip the /mcp path suffix).
    base_url = mcp_url.rsplit("/mcp", 1)[0]

    # Apply in-memory limit override only to agents whose config declares expense_limit.
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
    )

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
