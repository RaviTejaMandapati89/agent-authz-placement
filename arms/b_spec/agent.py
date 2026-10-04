"""
Arm B: rule enforcement generated into the tool code by Spec Kit.

The system prompt identifies the agent and who it acts for.
No policy rules are included in the prompt — enforcement is inline in the
generated domain/server.py for each generation.

apply_policy_change is a no-op: arm B requires regeneration to change the
policy (that is what the experiment measures for S7).
"""
import datetime

from dotenv import load_dotenv
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

from domain.identity import identity_paragraph
from runner.config import AWS_REGION, MODEL_ID, TEMPERATURE

load_dotenv()


def apply_policy_change(change: dict) -> None:
    """No-op: S7 changes only the server's data, as for every arm
    (PREREG section 9, 2026-10-03)."""


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
