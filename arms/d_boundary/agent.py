"""
Arm D: policy enforced by the Cedar PEP at the tool server.

The system prompt identifies the agent and who it acts for; no policy rules
are included.  No hook runs in the agent loop — every decision is made by
the PEP when the MCP call reaches the server.
"""
import datetime

from dotenv import load_dotenv
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

from domain.identity import identity_paragraph
from runner.config import AWS_REGION, MODEL_ID, TEMPERATURE

load_dotenv()

_files_affected: int = 0


def apply_policy_change(change: dict) -> None:
    """Record how many policy artefacts were affected by the change.

    For arm D a single in-memory edit to policy.cedar covers all agents,
    so the count is 1 when an expense_limit change is present, else 0.
    """
    global _files_affected
    _files_affected = 1 if (change and "expense_limit" in change) else 0


def run(
    agent_name: str,
    user: str,
    turns: list[str],
    mcp_url: str,
    run_id: str | None = None,
    between_turns_fn=None,
    bearer_token: str = "",
) -> tuple[str, dict, list[dict]]:
    """Run a multi-turn conversation and return (final_reply, usage, turns_data)."""
    model = BedrockModel(
        model_id=MODEL_ID,
        region_name=AWS_REGION,
        temperature=TEMPERATURE,
    )
    system_prompt = identity_paragraph(agent_name, user)
    headers = {"Authorization": f"Bearer {bearer_token}"}

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
            total_input  = usage["inputTokens"]  or 0
            total_output = usage["outputTokens"] or 0
            turns_data.append({
                "user_message": turn,
                "reply":        final_reply,
                "ts_start":     ts_start,
                "ts_end":       ts_end,
            })
            if i < len(turns) - 1 and between_turns_fn:
                between_turns_fn()

    return final_reply, {"input_tokens": total_input, "output_tokens": total_output}, turns_data
