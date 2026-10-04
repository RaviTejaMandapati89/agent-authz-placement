"""
Unit tests for the arm C hook (P1–P7) and an integration test that verifies
every tool's live fingerprint matches the reviewed one in the config files.

Unit tests use fake BeforeToolCallEvent objects and a fake httpx.Client; no
Bedrock and no live server are needed.

The integration test starts the clean domain server as a subprocess, loads tools
the way the agent does (via MCPClient), and asserts every fingerprint matches.
"""
import json
import os
import pathlib
import tempfile
from unittest.mock import MagicMock

import httpx
import pytest
import yaml

import arms.c_hook.agent as arm_c
from arms.c_hook.hook import PolicyHook, _load_config, fingerprint
from domain import tokens
from strands.hooks.events import BeforeToolCallEvent


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_CONFIG_DIR = _REPO_ROOT / "arms" / "c_hook" / "config"


def _make_tool_spec(name: str, description: str, input_schema: dict | None = None) -> dict:
    return {
        "name": name,
        "description": description,
        "inputSchema": {"json": input_schema or {"type": "object", "properties": {}}},
    }


def _make_selected_tool(spec: dict):
    t = MagicMock()
    t.tool_spec = spec
    return t


def _make_event(
    tool_name: str,
    input_args: dict,
    selected_tool=None,
) -> BeforeToolCallEvent:
    return BeforeToolCallEvent(
        agent=MagicMock(),
        selected_tool=selected_tool,
        tool_use={"toolUseId": "t-001", "name": tool_name, "input": input_args},
        invocation_state={},
    )


def _make_client(responses: dict) -> httpx.Client:
    """Return a mock httpx.Client whose .get() returns pre-built Response objects."""
    client = MagicMock(spec=httpx.Client)

    def fake_get(url: str, **kwargs):
        for path_suffix, body in responses.items():
            if url.endswith(path_suffix):
                if body is None:
                    resp = MagicMock(spec=httpx.Response)
                    resp.status_code = 404
                    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                        "404", request=MagicMock(), response=MagicMock()
                    )
                    return resp
                if isinstance(body, int):  # treat as status code (e.g. 503)
                    resp = MagicMock(spec=httpx.Response)
                    resp.status_code = body
                    resp.raise_for_status.side_effect = httpx.HTTPStatusError(
                        str(body), request=MagicMock(), response=MagicMock()
                    )
                    return resp
                resp = MagicMock(spec=httpx.Response)
                resp.status_code = 200
                resp.raise_for_status.return_value = None
                resp.json.return_value = body
                return resp
        # Fall-through: 404
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 404
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "404", request=MagicMock(), response=MagicMock()
        )
        return resp

    client.get.side_effect = fake_get
    return client


def _full_scope_bearer(agent_name: str, user: str) -> str:
    """Issue a real on-behalf-of token with the full set of scopes.

    Used as the default bearer_token in _hook so that the SCOPE check (added
    in task 4) passes for all pre-existing tests that are not testing scope.
    """
    all_scopes = sorted(tokens.FIXED_SCOPES)
    user_tok = tokens.issue_user_token(
        sub=user, aud=agent_name, scope=all_scopes, lifetime=3600,
    )
    agent_tok = tokens.issue_agent_token(
        sub=agent_name, aud=tokens.SERVER_AUDIENCE, scope=all_scopes, lifetime=3600,
    )
    return tokens.exchange(user_tok, agent_tok)


def _hook(
    agent_name: str,
    user: str,
    dir_responses: dict,
    limit_override: int | None = None,
    config_dir: pathlib.Path | None = None,
    bearer_token: str | None = None,
) -> PolicyHook:
    if bearer_token is None:
        bearer_token = _full_scope_bearer(agent_name, user)
    return PolicyHook(
        agent_name=agent_name,
        user=user,
        base_url="http://fake",
        run_id="test-run",
        limit_override=limit_override,
        config_dir=config_dir or _CONFIG_DIR,
        http_client=_make_client(dir_responses),
        bearer_token=bearer_token,
    )


def _run_hook(hook: PolicyHook, event: BeforeToolCallEvent) -> str | bool:
    """Invoke the hook and return the cancel_tool value (False means allowed)."""
    hook._before_tool_call(event)
    return event.cancel_tool


# ---------------------------------------------------------------------------
# real config-backed specs and fingerprints for tests that need to pass P6
# ---------------------------------------------------------------------------

def _real_spec(tool_name: str, agent_name: str) -> dict:
    cfg = _load_config(agent_name, _CONFIG_DIR)
    fp = cfg.get("fingerprints", {}).get(tool_name)
    if fp is None:
        # Fingerprints not yet written — build a spec whose hash we force-match below.
        return _make_tool_spec(tool_name, f"{tool_name} description")
    # Build a fake spec whose fingerprint equals the stored one.
    # We do this by using a deterministic spec and confirming the hash matches,
    # OR by bypassing fingerprint checks in tests that don't exercise P6.
    return None  # signal: fingerprints not yet computed, caller should set selected_tool=None


# ---------------------------------------------------------------------------
# apply_policy_change reset restores default limit
# ---------------------------------------------------------------------------

def test_apply_policy_change_reset_restores_default_limit():
    arm_c.apply_policy_change({"expense_limit": 300})
    arm_c.apply_policy_change({})
    # Replicate the limit logic from run(): no expense_limit in override → limit_override=None
    limit_override = None
    if "expense_limit" in arm_c._current_policy_override:
        cfg = _load_config("expense-assistant", _CONFIG_DIR)
        if "expense_limit" in cfg:
            limit_override = int(arm_c._current_policy_override["expense_limit"])
    hook = PolicyHook(
        agent_name="expense-assistant",
        user="alice",
        base_url="http://fake",
        limit_override=limit_override,
        config_dir=_CONFIG_DIR,
    )
    assert hook._limit == 500


# ---------------------------------------------------------------------------
# P7: unknown tool denied
# ---------------------------------------------------------------------------

def test_p7_unknown_tool_denied(tmp_path):
    log = str(tmp_path / "hook.jsonl")
    os.environ["HOOK_LOG"] = log
    h = _hook("expense-assistant", "alice", {})
    event = _make_event("export_all_expenses", {})
    result = _run_hook(h, event)
    assert result  # non-empty → denied
    lines = [json.loads(l) for l in pathlib.Path(log).read_text().splitlines() if l]
    assert lines[-1]["rule"] == "P7"
    assert lines[-1]["decision"] == "deny"


# ---------------------------------------------------------------------------
# P6: fingerprint mismatch denied
# ---------------------------------------------------------------------------

def test_p6_fingerprint_mismatch_denied(tmp_path):
    log = str(tmp_path / "hook.jsonl")
    os.environ["HOOK_LOG"] = log

    cfg = _load_config("expense-assistant", _CONFIG_DIR)
    stored_fps = cfg.get("fingerprints", {})
    if not stored_fps.get("submit_expense"):
        pytest.fail("fingerprints not written — run arms/c_hook/write_fingerprints.py")

    # Build a tool spec with a different description so the hash won't match.
    bad_spec = _make_tool_spec("submit_expense", "MODIFIED submit description")
    selected_tool = _make_selected_tool(bad_spec)
    h = _hook("expense-assistant", "alice", {})
    event = _make_event("submit_expense", {"claimant": "alice", "amount": 50.0, "description": "x"}, selected_tool)
    result = _run_hook(h, event)
    assert result
    lines = [json.loads(l) for l in pathlib.Path(log).read_text().splitlines() if l]
    assert lines[-1]["rule"] == "P6"


# ---------------------------------------------------------------------------
# P1: claimant must equal user
# ---------------------------------------------------------------------------

def test_p1_wrong_claimant_denied(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    h = _hook("expense-assistant", "alice", {})
    event = _make_event("submit_expense", {"claimant": "bob", "amount": 50.0, "description": "x"})
    result = _run_hook(h, event)
    assert result
    log = [json.loads(l) for l in pathlib.Path(str(tmp_path / "hook.jsonl")).read_text().splitlines() if l]
    assert log[-1]["rule"] == "P1"


# ---------------------------------------------------------------------------
# P2: over-limit without approval_ref
# ---------------------------------------------------------------------------

def test_p2_over_limit_no_ref_denied(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    h = _hook("expense-assistant", "alice", {})
    event = _make_event("submit_expense", {"claimant": "alice", "amount": 600.0, "description": "x"})
    result = _run_hook(h, event)
    assert result
    log = [json.loads(l) for l in pathlib.Path(str(tmp_path / "hook.jsonl")).read_text().splitlines() if l]
    assert log[-1]["rule"] == "P2"


def test_p2_over_limit_with_ref_allowed(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    h = _hook("expense-assistant", "alice", {})
    event = _make_event(
        "submit_expense",
        {"claimant": "alice", "amount": 600.0, "description": "x", "approval_ref": "mgr-approved"},
    )
    result = _run_hook(h, event)
    assert not result  # allowed


def test_p2_limit_override_respected(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    # Override limit to 300; £400 without ref should be denied.
    h = _hook("expense-assistant", "alice", {}, limit_override=300)
    event = _make_event("submit_expense", {"claimant": "alice", "amount": 400.0, "description": "x"})
    result = _run_hook(h, event)
    assert result
    log = [json.loads(l) for l in pathlib.Path(str(tmp_path / "hook.jsonl")).read_text().splitlines() if l]
    assert log[-1]["rule"] == "P2"


# ---------------------------------------------------------------------------
# P3: self-approval and not-manager
# ---------------------------------------------------------------------------

def test_p3_self_approve_denied(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    dir_resp = {
        "/directory/expenses/exp-001": {"claimant": "alice", "status": "pending"},
        "/directory/users/alice": {"role": "employee", "manager": "bob"},
    }
    h = _hook("expense-assistant", "alice", dir_resp)
    event = _make_event("approve_expense", {"expense_id": "exp-001"})
    result = _run_hook(h, event)
    assert result
    log = [json.loads(l) for l in pathlib.Path(str(tmp_path / "hook.jsonl")).read_text().splitlines() if l]
    assert log[-1]["rule"] == "P3"


def test_p3_not_manager_denied(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    dir_resp = {
        "/directory/expenses/exp-001": {"claimant": "alice", "status": "pending"},
        "/directory/users/alice": {"role": "employee", "manager": "bob"},
    }
    # carol is acting, but bob is alice's manager, so carol is denied
    h = _hook("expense-assistant", "carol", dir_resp)
    event = _make_event("approve_expense", {"expense_id": "exp-001"})
    result = _run_hook(h, event)
    assert result
    log = [json.loads(l) for l in pathlib.Path(str(tmp_path / "hook.jsonl")).read_text().splitlines() if l]
    assert log[-1]["rule"] == "P3"


def test_p3_manager_allowed(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    dir_resp = {
        "/directory/expenses/exp-001": {"claimant": "alice", "status": "pending"},
        "/directory/users/alice": {"role": "employee", "manager": "bob"},
    }
    h = _hook("expense-assistant", "bob", dir_resp)
    event = _make_event("approve_expense", {"expense_id": "exp-001"})
    result = _run_hook(h, event)
    assert not result


def test_p3_expense_not_found_denied(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    h = _hook("expense-assistant", "bob", {})  # empty dir → 404 for expense
    event = _make_event("approve_expense", {"expense_id": "exp-999"})
    result = _run_hook(h, event)
    assert result


# ---------------------------------------------------------------------------
# P4: delegation
# ---------------------------------------------------------------------------

_ACTIVE_DELEGATION = {
    "delegations": [
        {
            "id": "del-001",
            "delegator": "carol",
            "delegate": "dan",
            "scope": ["travel"],
            "active": True,
        },
    ],
    "sim_time": 1735689600.0,
}

_REVOKED_DELEGATION = {
    "delegations": [
        {
            "id": "del-003",
            "delegator": "carol",
            "delegate": "dan",
            "scope": ["travel"],
            "active": False,
        },
    ],
    "sim_time": 1735689600.0,
}


def test_p4_active_delegation_allowed(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    h = _hook("travel-assistant", "dan", {"/directory/delegations": _ACTIVE_DELEGATION})
    event = _make_event("book_travel", {"traveller": "carol", "details": "LHR-CDG"})
    result = _run_hook(h, event)
    assert not result


def test_p4_no_delegation_denied(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    h = _hook("travel-assistant", "dan", {"/directory/delegations": {
        "delegations": [], "sim_time": 1735689600.0,
    }})
    event = _make_event("book_travel", {"traveller": "carol", "details": "LHR-CDG"})
    result = _run_hook(h, event)
    assert result
    log = [json.loads(l) for l in pathlib.Path(str(tmp_path / "hook.jsonl")).read_text().splitlines() if l]
    assert log[-1]["rule"] == "P4"


def test_p4_denied_when_directory_returns_no_delegation(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    h = _hook("travel-assistant", "dan", {"/directory/delegations": {
        "delegations": [], "sim_time": 1830384000.0,
    }})
    event = _make_event("book_travel", {"traveller": "carol", "details": "LHR-CDG"})
    result = _run_hook(h, event)
    assert result
    log = [json.loads(l) for l in pathlib.Path(str(tmp_path / "hook.jsonl")).read_text().splitlines() if l]
    assert log[-1]["rule"] == "P4"


def test_p4_revoked_delegation_denied(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    h = _hook("travel-assistant", "dan", {"/directory/delegations": _REVOKED_DELEGATION})
    event = _make_event("book_travel", {"traveller": "carol", "details": "LHR-CDG"})
    result = _run_hook(h, event)
    assert result
    log = [json.loads(l) for l in pathlib.Path(str(tmp_path / "hook.jsonl")).read_text().splitlines() if l]
    assert log[-1]["rule"] == "P4"


def test_p4_same_user_no_delegation_needed(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    # dan books for himself; no delegation lookup expected
    h = _hook("travel-assistant", "dan", {})
    event = _make_event("book_travel", {"traveller": "dan", "details": "LHR-CDG"})
    result = _run_hook(h, event)
    assert not result


# ---------------------------------------------------------------------------
# P5: vendor approval
# ---------------------------------------------------------------------------

def test_p5_approved_vendor_allowed(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    dir_resp = {"/directory/vendors": ["acme-hotels", "fastair"]}
    h = _hook("payments-agent", "erin", dir_resp)
    # payments-agent acts_for=finance; add erin to user dir
    h._config["acts_for"] = "any"  # bypass role check for this test
    event = _make_event("pay_vendor", {"vendor": "acme-hotels", "amount": 100.0, "reference": "r"})
    result = _run_hook(h, event)
    assert not result


def test_p5_unapproved_vendor_denied(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    dir_resp = {"/directory/vendors": ["acme-hotels", "fastair"]}
    h = _hook("payments-agent", "erin", dir_resp)
    h._config["acts_for"] = "any"
    event = _make_event("pay_vendor", {"vendor": "rogue-vendor", "amount": 100.0, "reference": "r"})
    result = _run_hook(h, event)
    assert result
    log = [json.loads(l) for l in pathlib.Path(str(tmp_path / "hook.jsonl")).read_text().splitlines() if l]
    assert log[-1]["rule"] == "P5"


# ---------------------------------------------------------------------------
# payments-agent: acts_for finance (P7 via role check)
# ---------------------------------------------------------------------------

def test_payments_agent_non_finance_denied(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    dir_resp = {"/directory/users/alice": {"role": "employee", "manager": "bob"}}
    h = _hook("payments-agent", "alice", dir_resp)
    event = _make_event("pay_vendor", {"vendor": "acme-hotels", "amount": 100.0, "reference": "r"})
    result = _run_hook(h, event)
    assert result
    log = [json.loads(l) for l in pathlib.Path(str(tmp_path / "hook.jsonl")).read_text().splitlines() if l]
    assert log[-1]["rule"] == "P7"


def test_payments_agent_finance_passes_role_check(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    dir_resp = {
        "/directory/users/erin": {"role": "finance", "manager": None},
        "/directory/vendors": ["acme-hotels"],
    }
    h = _hook("payments-agent", "erin", dir_resp)
    event = _make_event("pay_vendor", {"vendor": "acme-hotels", "amount": 100.0, "reference": "r"})
    result = _run_hook(h, event)
    assert not result


# ---------------------------------------------------------------------------
# blank user denied
# ---------------------------------------------------------------------------

def test_blank_user_denied(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    h = _hook("expense-assistant", "", {})
    event = _make_event("submit_expense", {"claimant": "", "amount": 50.0, "description": "x"})
    result = _run_hook(h, event)
    assert result


# ---------------------------------------------------------------------------
# error wrapper: hook raises, logs error line, re-raises
# ---------------------------------------------------------------------------

def test_error_wrapper_logs_and_reraises(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    # Use a 503 response to trigger an exception in the hook.
    dir_resp = {"/directory/vendors": 503}
    h = _hook("payments-agent", "erin", dir_resp)
    h._config["acts_for"] = "any"
    event = _make_event("pay_vendor", {"vendor": "acme-hotels", "amount": 100.0, "reference": "r"})
    with pytest.raises(httpx.HTTPStatusError):
        _run_hook(h, event)
    log_path = tmp_path / "hook.jsonl"
    log = [json.loads(l) for l in log_path.read_text().splitlines() if l]
    assert log[-1]["decision"] == "error"


# ---------------------------------------------------------------------------
# S1 baseline: legitimate expense is allowed
# ---------------------------------------------------------------------------

def test_s1_legitimate_allowed(tmp_path):
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    h = _hook("expense-assistant", "alice", {})
    event = _make_event("submit_expense", {"claimant": "alice", "amount": 120.0, "description": "office"})
    result = _run_hook(h, event)
    assert not result
    log = [json.loads(l) for l in pathlib.Path(str(tmp_path / "hook.jsonl")).read_text().splitlines() if l]
    assert log[-1]["decision"] == "allow"


# ---------------------------------------------------------------------------
# integration: fingerprints match reviewed values in config files
# ---------------------------------------------------------------------------

def test_fingerprints_match_reviewed(tmp_path):
    """Start the clean domain server, load tools via MCPClient (as the agent does),
    and assert every tool's live fingerprint matches the stored reviewed fingerprint."""
    from strands.tools.mcp import MCPClient
    from runner._server import domain_server

    # Gather all reviewed fingerprints from the three config files.
    reviewed: dict[str, str] = {}
    for agent_name in ("expense-assistant", "travel-assistant", "payments-agent"):
        cfg = _load_config(agent_name, _CONFIG_DIR)
        reviewed.update(cfg.get("fingerprints", {}))

    if not reviewed:
        pytest.fail("fingerprints not written — run arms/c_hook/write_fingerprints.py")

    log = str(tmp_path / "fp_test.jsonl")
    with domain_server(log) as (port, base_url):
        mcp_url = f"{base_url}/mcp"
        with MCPClient(url=mcp_url) as client:
            tools = list(client.list_tools_sync())

    for tool in tools:
        if tool.tool_name in reviewed:
            live_fp = fingerprint(tool.tool_spec)
            assert live_fp == reviewed[tool.tool_name], (
                f"fingerprint mismatch for {tool.tool_name}: "
                f"live={live_fp!r} reviewed={reviewed[tool.tool_name]!r}"
            )


# ---------------------------------------------------------------------------
# Item 10: hook reads no clock; uses directory filtering; logs directory's
#           sim_time
# ---------------------------------------------------------------------------

def test_hook_module_imports_no_clock():
    """The hook module must not import or reference simclock."""
    import inspect
    import arms.c_hook.hook as hook_mod
    source = inspect.getsource(hook_mod)
    assert "simclock" not in source, (
        "hook.py must not reference simclock — delegation filtering belongs to the directory"
    )


def test_hook_refuses_booking_when_directory_filters_expired(tmp_path):
    """After the directory filters an expired delegation, the hook denies with P4."""
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    # Directory already filtered — delegation expired, so list is empty
    dir_resp = {
        "/directory/delegations": {
            "delegations": [],
            "sim_time": 1830384000.0,
        },
    }
    h = _hook("travel-assistant", "dan", dir_resp)
    event = _make_event("book_travel", {"traveller": "carol", "details": "LHR-CDG"})
    result = _run_hook(h, event)
    assert result  # denied
    log = [json.loads(l) for l in (tmp_path / "hook.jsonl").read_text().splitlines() if l]
    assert log[-1]["rule"] == "P4"


def test_hook_log_sim_time_equals_directory_sim_time(tmp_path):
    """The hook logs sim_time from the directory response, not from its own clock."""
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    dir_sim_time = 1735689900.0
    dir_resp = {
        "/directory/delegations": {
            "delegations": [
                {"id": "del-001", "delegator": "carol", "delegate": "dan",
                 "scope": ["travel"], "active": True},
            ],
            "sim_time": dir_sim_time,
        },
    }
    h = _hook("travel-assistant", "dan", dir_resp)
    event = _make_event("book_travel", {"traveller": "carol", "details": "LHR-CDG"})
    _run_hook(h, event)
    log = [json.loads(l) for l in (tmp_path / "hook.jsonl").read_text().splitlines() if l]
    assert log[-1]["sim_time"] == dir_sim_time


# ---------------------------------------------------------------------------
# Item 9 (hook side): delegation directory 503 raises, not P4
# ---------------------------------------------------------------------------

def test_hook_delegation_503_raises_not_p4(tmp_path):
    """A 503 from /directory/delegations raises an error, not a P4 denial."""
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    dir_resp = {"/directory/delegations": 503}
    h = _hook("travel-assistant", "dan", dir_resp)
    event = _make_event("book_travel", {"traveller": "carol", "details": "LHR-CDG"})
    with pytest.raises(httpx.HTTPStatusError):
        _run_hook(h, event)
    log = [json.loads(l) for l in (tmp_path / "hook.jsonl").read_text().splitlines() if l]
    assert log[-1]["decision"] == "error"
