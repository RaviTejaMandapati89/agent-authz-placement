"""
Approach 3: policy enforced by a generated checkpoint (PolicyHook).

The runner instantiates the checkpoint from the selected gen-N directory,
passes bearer_token and config_dir, and attaches it as a hook.
"""
import datetime
import importlib.util
import pathlib

from dotenv import load_dotenv
from strands import Agent
from strands.models.bedrock import BedrockModel
from strands.tools.mcp import MCPClient

from domain.identity import identity_paragraph
from runner.config import AWS_REGION, MODEL_ID, TEMPERATURE

load_dotenv()

_APPROACH3 = pathlib.Path(__file__).parent

_current_policy_override: dict = {}


def apply_policy_change(change: dict) -> None:
    """Store a policy override in memory for the duration of the next run."""
    global _current_policy_override
    _current_policy_override = change or {}


def _load_checkpoint(gen: int):
    """Import PolicyHook from gen-N/checkpoint.py."""
    gen_dir = _APPROACH3 / f"gen-{gen}"
    checkpoint_path = gen_dir / "checkpoint.py"
    if not checkpoint_path.exists():
        raise FileNotFoundError(
            f"checkpoint.py not found for gen-{gen}: {checkpoint_path}"
        )
    spec = importlib.util.spec_from_file_location(
        f"arm3_gen{gen}_checkpoint", checkpoint_path
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.PolicyHook


def run(
    agent_name: str,
    user: str,
    turns: list[str],
    mcp_url: str,
    run_id: str | None = None,
    between_turns_fn=None,
    bearer_token: str = "",
    config_dir: pathlib.Path | None = None,
    gen: int = 1,
) -> tuple[str, dict, list[dict]]:
    """Run a multi-turn conversation and return (final_reply, usage, turns_data)."""
    model = BedrockModel(
        model_id=MODEL_ID,
        region_name=AWS_REGION,
        temperature=TEMPERATURE,
    )
    system_prompt = identity_paragraph(agent_name, user)
    headers = {"Authorization": f"Bearer {bearer_token}"}

    base_url = mcp_url.rsplit("/mcp", 1)[0]

    limit_override: int | None = None
    if "expense_limit" in _current_policy_override:
        limit_override = int(_current_policy_override["expense_limit"])

    PolicyHook = _load_checkpoint(gen)
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
