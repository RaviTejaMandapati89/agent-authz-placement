"""Item 11: every agent arm builds its MCP client with only an Authorization:
Bearer header — no X-User, no X-Agent."""
from unittest.mock import MagicMock

import pytest


def _assert_only_bearer(arm_path, agent_name, user, monkeypatch):
    """Verify that run() in *arm_path* passes only Authorization: Bearer to MCPClient."""
    captured = {}

    class FakeMCPClient:
        def __init__(self, url=None, headers=None, **kw):
            captured["headers"] = dict(headers or {})

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def list_tools_sync(self):
            return []

    mock_result = MagicMock()
    mock_result.metrics.accumulated_usage = {"inputTokens": 0, "outputTokens": 0}

    mock_agent_instance = MagicMock(return_value=mock_result)

    monkeypatch.setattr(f"{arm_path}.MCPClient", FakeMCPClient)
    monkeypatch.setattr(f"{arm_path}.BedrockModel", MagicMock)
    monkeypatch.setattr(f"{arm_path}.Agent", lambda **kw: mock_agent_instance)

    import importlib
    mod = importlib.import_module(arm_path)
    mod.run(agent_name, user, ["hello"], "http://fake/mcp")

    hdrs = captured["headers"]
    assert "X-User" not in hdrs, f"X-User found in {arm_path}"
    assert "X-Agent" not in hdrs, f"X-Agent found in {arm_path}"
    assert "Authorization" in hdrs, f"no Authorization header in {arm_path}"
    assert hdrs["Authorization"].startswith("Bearer "), f"not a Bearer token in {arm_path}"


def test_arm_a_sends_only_bearer(monkeypatch):
    _assert_only_bearer("arms.a_guides.agent", "expense-assistant", "alice", monkeypatch)


def test_arm_b_sends_only_bearer(monkeypatch):
    _assert_only_bearer("arms.b_spec.agent", "expense-assistant", "alice", monkeypatch)


def test_arm_c_sends_only_bearer(monkeypatch):
    monkeypatch.setattr(
        "arms.c_hook.agent.PolicyHook", MagicMock,
    )
    _assert_only_bearer("arms.c_hook.agent", "expense-assistant", "alice", monkeypatch)


def test_arm_d_sends_only_bearer(monkeypatch):
    _assert_only_bearer("arms.d_boundary.agent", "expense-assistant", "alice", monkeypatch)
