"""
Contract tests for the generated checkpoint module.

These tests verify FORMAT only:
  - PolicyHook class exists with the required constructor signature
  - BeforeToolCallEvent hook is registered
  - Decision log entries contain all required fields

They do not assert any allow or deny outcome for any policy rule.
"""
import inspect
import json
import os
import pathlib

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _write_minimal_config(config_dir: pathlib.Path, agent_name: str = "test-agent") -> None:
    """Write a minimal agent config YAML with an empty allowed_tools list."""
    (config_dir / f"{agent_name}.yaml").write_text(
        f"agent: {agent_name}\nacts_for: any\nallowed_tools: []\nfingerprints: {{}}\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# class and constructor
# ---------------------------------------------------------------------------

def test_policyhook_class_exists():
    """The generated module must export a PolicyHook class."""
    from checkpoint import PolicyHook  # noqa: F401
    assert PolicyHook is not None


def test_constructor_accepts_agent_name_user_base_url(tmp_path):
    """PolicyHook constructor must accept agent_name, user, and base_url."""
    from checkpoint import PolicyHook
    sig = inspect.signature(PolicyHook.__init__)
    for arg in ("agent_name", "user", "base_url"):
        assert arg in sig.parameters, f"constructor missing parameter {arg!r}"


def test_constructor_accepts_optional_args(tmp_path):
    """PolicyHook constructor must accept run_id, limit_override, config_dir, bearer_token."""
    from checkpoint import PolicyHook
    sig = inspect.signature(PolicyHook.__init__)
    for arg in ("run_id", "config_dir", "bearer_token"):
        assert arg in sig.parameters, f"constructor missing optional parameter {arg!r}"


def test_constructor_instantiates_with_config_dir(tmp_path):
    """PolicyHook must instantiate without error when given a valid config_dir."""
    from checkpoint import PolicyHook
    _write_minimal_config(tmp_path)
    hook = PolicyHook(
        agent_name="test-agent",
        user="testuser",
        base_url="http://localhost:9999",
        config_dir=tmp_path,
    )
    assert hook is not None


# ---------------------------------------------------------------------------
# hook registration
# ---------------------------------------------------------------------------

def test_register_hooks_registers_before_tool_call_event(tmp_path):
    """register_hooks must register a callback for BeforeToolCallEvent."""
    from checkpoint import PolicyHook
    from strands.hooks import BeforeToolCallEvent

    _write_minimal_config(tmp_path)
    hook = PolicyHook(
        agent_name="test-agent",
        user="testuser",
        base_url="http://localhost:9999",
        config_dir=tmp_path,
    )

    registered_event_types = []

    class _FakeRegistry:
        def add_callback(self, event_type, callback):
            registered_event_types.append(event_type)

    hook.register_hooks(_FakeRegistry())
    assert BeforeToolCallEvent in registered_event_types, \
        "register_hooks did not register a BeforeToolCallEvent callback"


# ---------------------------------------------------------------------------
# decision log format
# ---------------------------------------------------------------------------

def test_decision_log_entry_has_required_fields(tmp_path, monkeypatch):
    """A tool call must produce a log entry containing all required fields."""
    from checkpoint import PolicyHook

    log_file = tmp_path / "hook_decisions.jsonl"
    monkeypatch.setenv("HOOK_LOG", str(log_file))

    _write_minimal_config(tmp_path)
    hook = PolicyHook(
        agent_name="test-agent",
        user="testuser",
        base_url="http://localhost:9999",
        config_dir=tmp_path,
        run_id="run-test-001",
        bearer_token="",
    )

    class _FakeEvent:
        tool_use = {"name": "some_tool", "input": {"x": 1}}
        selected_tool = None
        cancel_tool = None

    hook._before_tool_call(_FakeEvent())

    lines = [json.loads(ln) for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines, "no log entries written after tool call"
    entry = lines[-1]

    required = ("call_id", "run_id", "decision", "rule", "reason")
    for field in required:
        assert field in entry, f"log entry missing required field {field!r}"


def test_decision_log_decision_field_is_string(tmp_path, monkeypatch):
    """The decision field must be a non-empty string."""
    from checkpoint import PolicyHook

    log_file = tmp_path / "hook_decisions.jsonl"
    monkeypatch.setenv("HOOK_LOG", str(log_file))

    _write_minimal_config(tmp_path)
    hook = PolicyHook(
        agent_name="test-agent",
        user="testuser",
        base_url="http://localhost:9999",
        config_dir=tmp_path,
        bearer_token="",
    )

    class _FakeEvent:
        tool_use = {"name": "some_tool", "input": {}}
        selected_tool = None
        cancel_tool = None

    hook._before_tool_call(_FakeEvent())

    lines = [json.loads(ln) for ln in log_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert lines
    entry = lines[-1]
    assert isinstance(entry.get("decision"), str) and entry["decision"], \
        "decision field must be a non-empty string"
