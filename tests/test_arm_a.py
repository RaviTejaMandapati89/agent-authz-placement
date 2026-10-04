"""
Arm A prompt tests. No Bedrock calls, no MCP connections.
"""
from unittest.mock import MagicMock, patch

import pytest
import arms.a_guides.agent as arm_a
from arms.a_guides.agent import build_system_prompt, run as arm_run


def test_prompt_excludes_experiment_metadata():
    prompt = build_system_prompt("expense-assistant")
    lower = prompt.lower()
    for word in ("experiment", "arm", "kyc", "prereg"):
        assert word not in lower, f"system prompt contains forbidden word {word!r}"


def test_prompt_includes_policy_rules():
    prompt = build_system_prompt("expense-assistant")
    assert "P1" in prompt
    assert "P2" in prompt
    assert "P7" in prompt


def test_prompt_includes_agent_identity():
    for agent_name in ("expense-assistant", "travel-assistant", "payments-agent"):
        prompt = build_system_prompt(agent_name)
        assert "You are the" in prompt


def test_policy_change_rewrites_limit_in_rules():
    prompt = build_system_prompt("expense-assistant", {"expense_limit": 300})
    assert "£300" in prompt
    assert "£500" not in prompt


def test_policy_change_different_limit():
    prompt = build_system_prompt("expense-assistant", {"expense_limit": 750})
    assert "£750" in prompt
    assert "£500" not in prompt


def test_no_policy_change_has_default_limit():
    prompt = build_system_prompt("expense-assistant", {})
    assert "£500" in prompt


def test_policy_change_does_not_affect_other_agents():
    # Changes to expense_limit only substitute in P2 text, not anywhere else.
    prompt = build_system_prompt("payments-agent", {"expense_limit": 300})
    assert "£300" in prompt
    assert "£500" not in prompt


def test_apply_policy_change_reset_restores_default_limit():
    arm_a.apply_policy_change({"expense_limit": 300})
    arm_a.apply_policy_change({})
    prompt = build_system_prompt("expense-assistant")
    assert "£500" in prompt
    assert "£300" not in prompt


def test_run_handles_dict_usage():
    """Strands returns accumulated_usage as a dict; run() must use dict-style access."""
    mock_result = MagicMock()
    mock_result.__str__ = MagicMock(return_value="reply text")
    mock_result.metrics.accumulated_usage = {"inputTokens": 42, "outputTokens": 17}

    mock_agent_instance = MagicMock(return_value=mock_result)

    mock_mcp = MagicMock()
    mock_mcp.__enter__ = MagicMock(return_value=mock_mcp)
    mock_mcp.__exit__ = MagicMock(return_value=False)
    mock_mcp.list_tools_sync.return_value = []

    with (
        patch("arms.a_guides.agent.MCPClient", return_value=mock_mcp),
        patch("arms.a_guides.agent.Agent", return_value=mock_agent_instance),
        patch("arms.a_guides.agent.BedrockModel"),
    ):
        reply, usage, turns_data = arm_run(
            agent_name="expense-assistant",
            user="alice",
            turns=["submit expense"],
            mcp_url="http://localhost:9999/mcp",
        )

    assert reply == "reply text"
    assert usage["input_tokens"] == 42
    assert usage["output_tokens"] == 17
    assert len(turns_data) == 1
    assert turns_data[0]["user_message"] == "submit expense"
    assert turns_data[0]["reply"] == "reply text"


def test_arm_c_prompt_equals_arm_a_without_rules():
    """Arm C's identity paragraph is exactly arm A's prompt with the rules removed."""
    from domain.identity import identity_paragraph
    agent_name = "travel-assistant"
    user = "dan"
    arm_a_prompt = arm_a.build_system_prompt(agent_name, None, user)
    arm_c_prompt = identity_paragraph(agent_name, user)
    # arm A's prompt begins with the shared identity paragraph
    assert arm_a_prompt.startswith(arm_c_prompt), (
        f"arm A prompt does not start with arm C identity paragraph"
    )
    # arm A has additional content (the policy rules)
    assert len(arm_a_prompt) > len(arm_c_prompt)
    rules_section = arm_a_prompt[len(arm_c_prompt):].strip()
    assert "P1" in rules_section
    assert "P2" in rules_section
