"""
Tests for task 4: shared scope map (domain/scopes.py) and approach-2 scope check.

Tests marked FAILS are expected to fail against current code; they define
what must change.  Tests marked PASSES confirm existing behaviour.

Conflicts with existing tests are listed in a comment block at the bottom.
"""
import inspect
import json
import os
import pathlib
from unittest.mock import MagicMock, patch

import pytest
import yaml

import arms.a_guides.agent as arm_a
import arms.c_hook.agent as arm_c
from arms.c_hook.hook import PolicyHook, _load_config
from domain import tokens

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_CONFIG_DIR = _REPO_ROOT / "arms" / "c_hook" / "config"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _make_event(tool_name: str, input_args: dict, selected_tool=None):
    from strands.hooks.events import BeforeToolCallEvent
    return BeforeToolCallEvent(
        agent=MagicMock(),
        selected_tool=selected_tool,
        tool_use={"toolUseId": "t-001", "name": tool_name, "input": input_args},
        invocation_state={},
    )


def _make_client(responses: dict):
    import httpx
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
                resp = MagicMock(spec=httpx.Response)
                resp.status_code = 200
                resp.raise_for_status.return_value = None
                resp.json.return_value = body
                return resp
        resp = MagicMock(spec=httpx.Response)
        resp.status_code = 404
        resp.raise_for_status.side_effect = None
        return resp

    client.get.side_effect = fake_get
    return client


def _run_hook(hook: PolicyHook, event) -> str | bool:
    hook._before_tool_call(event)
    return event.cancel_tool


def _bearer_with_scopes(scopes: list[str]) -> str:
    """Issue a real on-behalf-of token for alice/expense-assistant with given scopes."""
    user_token = tokens.issue_user_token(
        sub="alice", aud="expense-assistant", scope=scopes, lifetime=3600,
    )
    agent_token = tokens.issue_agent_token(
        sub="expense-assistant", aud="mcp-server", scope=scopes, lifetime=3600,
    )
    return tokens.exchange(user_token, agent_token)


def _hook_with_bearer(
    agent_name: str,
    user: str,
    dir_responses: dict,
    bearer_token: str,
    limit_override: int | None = None,
    config_dir: pathlib.Path | None = None,
) -> PolicyHook:
    """Create a PolicyHook with bearer_token (new parameter required by task 4)."""
    return PolicyHook(
        agent_name=agent_name,
        user=user,
        base_url="http://fake",
        run_id="test-run",
        limit_override=limit_override,
        config_dir=config_dir or _CONFIG_DIR,
        http_client=_make_client(dir_responses),
        bearer_token=bearer_token,  # new parameter — FAILS until task 4 is built
    )


# ===========================================================================
# 1. Shared scope map: domain/scopes.py
# ===========================================================================

def test_scope_map_module_exists():
    """domain.scopes must export TOOL_SCOPE_MAP.
    FAILS: module does not exist yet."""
    from domain.scopes import TOOL_SCOPE_MAP  # noqa: F401
    assert isinstance(TOOL_SCOPE_MAP, dict)


def test_scope_map_has_all_six_tools():
    """TOOL_SCOPE_MAP must contain exactly the six tool→scope entries.
    FAILS: module does not exist yet."""
    from domain.scopes import TOOL_SCOPE_MAP
    assert TOOL_SCOPE_MAP == {
        "read_receipt":       "expenses:read",
        "submit_expense":     "expenses:submit",
        "approve_expense":    "expenses:approve",
        "book_travel":        "travel:book",
        "pay_vendor":         "payments:pay",
        "ask_payments_agent": "agents:payments",
    }


def test_gateway_imports_from_domain_scopes():
    """domain.gateway must reference domain.scopes so there is one copy of the map.
    FAILS: gateway currently receives the map as a passed-in argument."""
    import domain.gateway
    source = inspect.getsource(domain.gateway)
    assert "domain.scopes" in source or "from domain import scopes" in source, (
        "domain.gateway still receives its scope map as a constructor argument "
        "instead of importing from domain.scopes"
    )


def test_hook_imports_from_domain_scopes():
    """arms.c_hook.hook must reference domain.scopes for the tool→scope lookup.
    FAILS: hook has no scope-checking logic yet."""
    import arms.c_hook.hook
    source = inspect.getsource(arms.c_hook.hook)
    assert "domain.scopes" in source or "from domain import scopes" in source, (
        "arms.c_hook.hook does not yet import domain.scopes"
    )


# ===========================================================================
# 2. Approach 2 scope check in PolicyHook
# ===========================================================================

def test_hook_missing_bearer_token_refused(tmp_path):
    """PolicyHook with an empty bearer_token refuses any tool call with rule SCOPE.
    FAILS: PolicyHook has no bearer_token parameter."""
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    h = _hook_with_bearer("expense-assistant", "alice", {}, bearer_token="")
    event = _make_event("submit_expense", {"claimant": "alice", "amount": 50.0})
    result = _run_hook(h, event)
    assert result, "expected denial (missing bearer token)"
    log = [json.loads(l) for l in (tmp_path / "hook.jsonl").read_text().splitlines() if l]
    assert log[-1]["rule"] == "SCOPE", f"expected SCOPE, got {log[-1]['rule']!r}"


def test_hook_unreadable_bearer_token_refused(tmp_path):
    """PolicyHook with a malformed bearer token refuses with rule SCOPE.
    FAILS: PolicyHook has no bearer_token parameter."""
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    h = _hook_with_bearer(
        "expense-assistant", "alice", {}, bearer_token="not.a.valid.jwt",
    )
    event = _make_event("submit_expense", {"claimant": "alice", "amount": 50.0})
    result = _run_hook(h, event)
    assert result, "expected denial (unreadable bearer token)"
    log = [json.loads(l) for l in (tmp_path / "hook.jsonl").read_text().splitlines() if l]
    assert log[-1]["rule"] == "SCOPE", f"expected SCOPE, got {log[-1]['rule']!r}"


# --- Acceptance test (named in spec) ---

def test_acceptance_approve_expense_refused_when_scope_lacks_expenses_approve(tmp_path):
    """
    Acceptance: approach 2 refuses approve_expense with rule SCOPE when the bearer
    token lacks expenses:approve, even though the agent's config allows that tool.
    FAILS: PolicyHook has no bearer_token parameter and no SCOPE check.
    """
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")

    scopes_without_approve = sorted(
        s for s in tokens.FIXED_SCOPES if s != "expenses:approve"
    )
    bearer = _bearer_with_scopes(scopes_without_approve)

    # Pre-condition: expense-assistant's committed config does allow approve_expense.
    cfg = _load_config("expense-assistant", _CONFIG_DIR)
    assert "approve_expense" in cfg.get("allowed_tools", []), (
        "test pre-condition: approve_expense must be in expense-assistant allowed_tools"
    )

    dir_resp = {
        "/directory/expenses/exp-001": {"claimant": "charlie", "status": "pending"},
        "/directory/users/charlie": {"role": "employee", "manager": "alice"},
    }
    h = _hook_with_bearer("expense-assistant", "alice", dir_resp, bearer_token=bearer)
    event = _make_event("approve_expense", {"expense_id": "exp-001"})
    result = _run_hook(h, event)

    assert result, "expected SCOPE denial"
    log = [json.loads(l) for l in (tmp_path / "hook.jsonl").read_text().splitlines() if l]
    assert log[-1]["rule"] == "SCOPE", (
        f"expected SCOPE, got {log[-1]['rule']!r}; "
        "approach 2 must refuse based on bearer token scope, not just config"
    )


def test_hook_scope_check_valid_scope_allows_tool(tmp_path):
    """With a bearer token that carries expenses:approve, approve_expense is allowed.
    FAILS: PolicyHook has no bearer_token parameter."""
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    bearer = _bearer_with_scopes(sorted(tokens.FIXED_SCOPES))
    dir_resp = {
        "/directory/expenses/exp-001": {"claimant": "charlie", "status": "pending"},
        "/directory/users/charlie": {"role": "employee", "manager": "alice"},
    }
    h = _hook_with_bearer("expense-assistant", "alice", dir_resp, bearer_token=bearer)
    event = _make_event("approve_expense", {"expense_id": "exp-001"})
    result = _run_hook(h, event)
    assert not result, "expected allow (token has expenses:approve)"


# ---------------------------------------------------------------------------
# 2a. Check order: allowed_tools (P7) → P6 → SCOPE → policy rules
# ---------------------------------------------------------------------------

def test_scope_order_p7_fires_before_scope(tmp_path):
    """A tool absent from allowed_tools gets P7, not SCOPE (P7 is checked first).
    FAILS: PolicyHook has no bearer_token parameter."""
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    # Token deliberately missing expenses:approve — but P7 must fire first.
    scopes_without_approve = sorted(
        s for s in tokens.FIXED_SCOPES if s != "expenses:approve"
    )
    bearer = _bearer_with_scopes(scopes_without_approve)
    h = _hook_with_bearer("expense-assistant", "alice", {}, bearer_token=bearer)
    event = _make_event("export_all_expenses", {})  # not in allowed_tools
    result = _run_hook(h, event)
    assert result
    log = [json.loads(l) for l in (tmp_path / "hook.jsonl").read_text().splitlines() if l]
    assert log[-1]["rule"] == "P7", f"expected P7, got {log[-1]['rule']!r}"


def test_scope_order_p6_fires_before_scope(tmp_path):
    """A fingerprint mismatch is denied with P6, not SCOPE (P6 is checked before SCOPE).
    FAILS: PolicyHook has no bearer_token parameter."""
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    cfg = _load_config("expense-assistant", _CONFIG_DIR)
    if not cfg.get("fingerprints", {}).get("submit_expense"):
        pytest.skip("fingerprints not yet written — run write_fingerprints.py first")

    # Token missing expenses:submit — P6 must still fire before SCOPE.
    scopes_without_submit = sorted(
        s for s in tokens.FIXED_SCOPES if s != "expenses:submit"
    )
    bearer = _bearer_with_scopes(scopes_without_submit)
    bad_spec = _make_tool_spec("submit_expense", "TAMPERED description")
    selected_tool = _make_selected_tool(bad_spec)

    h = _hook_with_bearer("expense-assistant", "alice", {}, bearer_token=bearer)
    event = _make_event(
        "submit_expense",
        {"claimant": "alice", "amount": 50.0, "description": "x"},
        selected_tool,
    )
    result = _run_hook(h, event)
    assert result
    log = [json.loads(l) for l in (tmp_path / "hook.jsonl").read_text().splitlines() if l]
    assert log[-1]["rule"] == "P6", f"expected P6, got {log[-1]['rule']!r}"


def test_scope_order_policy_rule_fires_after_scope(tmp_path):
    """When scope is present a policy rule violation still fires (SCOPE is not the last word).
    FAILS: PolicyHook has no bearer_token parameter."""
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")
    bearer = _bearer_with_scopes(sorted(tokens.FIXED_SCOPES))
    dir_resp = {
        "/directory/expenses/exp-001": {"claimant": "alice", "status": "pending"},
        "/directory/users/alice": {"role": "employee", "manager": "bob"},
    }
    h = _hook_with_bearer("expense-assistant", "alice", dir_resp, bearer_token=bearer)
    # alice approving her own expense — scope passes, P3 (self-approval) fires.
    event = _make_event("approve_expense", {"expense_id": "exp-001"})
    result = _run_hook(h, event)
    assert result
    log = [json.loads(l) for l in (tmp_path / "hook.jsonl").read_text().splitlines() if l]
    assert log[-1]["rule"] == "P3", (
        f"expected P3 (scope present, policy rule should fire), got {log[-1]['rule']!r}"
    )


# ===========================================================================
# 3. S13: per-run config directory accessible via arm_c.run()
# ===========================================================================

def test_arm_c_run_accepts_config_dir_parameter():
    """arm_c.run() must accept a config_dir keyword parameter so the runner can
    point the hook at a per-run copy of the config directory (S13).
    FAILS: arm_c.run() has no config_dir parameter."""
    sig = inspect.signature(arm_c.run)
    assert "config_dir" in sig.parameters, (
        "arm_c.run() does not have a config_dir parameter; "
        "S13 requires the runner to pass a per-run config directory to PolicyHook"
    )


def test_s13_per_run_config_dir_adds_tool_without_touching_committed_files(tmp_path):
    """A per-run config_dir can extend an agent's allowed_tools; the tool must not be
    denied with P7.  Committed config files must remain unchanged.
    FAILS: PolicyHook has no bearer_token parameter."""
    os.environ["HOOK_LOG"] = str(tmp_path / "hook.jsonl")

    committed_cfg = _load_config("expense-assistant", _CONFIG_DIR)
    run_cfg = dict(committed_cfg)
    run_cfg["allowed_tools"] = list(committed_cfg.get("allowed_tools", [])) + [
        "read_all_expenses"
    ]

    run_config_dir = tmp_path / "run_config"
    run_config_dir.mkdir()
    (run_config_dir / "expense-assistant.yaml").write_text(yaml.dump(run_cfg))

    # Committed config must NOT contain the extra tool.
    assert "read_all_expenses" not in committed_cfg.get("allowed_tools", [])

    bearer = _bearer_with_scopes(sorted(tokens.FIXED_SCOPES))
    h = _hook_with_bearer(
        "expense-assistant", "alice", {},
        bearer_token=bearer,
        config_dir=run_config_dir,
    )
    event = _make_event("read_all_expenses", {})
    result = _run_hook(h, event)

    if result:
        log = [json.loads(l) for l in (tmp_path / "hook.jsonl").read_text().splitlines() if l]
        assert log[-1]["rule"] != "P7", (
            "tool added via per-run config_dir was P7-denied — S13 not satisfied"
        )


# ===========================================================================
# 4. Approach 1: sends only bearer token; prompt contains policy rules
# ===========================================================================

def test_arm_a_run_sends_only_bearer_token_as_authorization_header():
    """arm_a.run() passes exactly {'Authorization': 'Bearer <token>'} to MCPClient.
    PASSES against current code (confirms existing behaviour)."""
    mock_result = MagicMock()
    mock_result.__str__ = MagicMock(return_value="reply text")
    mock_result.metrics.accumulated_usage = {"inputTokens": 1, "outputTokens": 1}
    mock_agent = MagicMock(return_value=mock_result)

    mock_mcp = MagicMock()
    mock_mcp.__enter__ = MagicMock(return_value=mock_mcp)
    mock_mcp.__exit__ = MagicMock(return_value=False)
    mock_mcp.list_tools_sync.return_value = []

    mock_mcp_cls = MagicMock(return_value=mock_mcp)

    with (
        patch("arms.a_guides.agent.MCPClient", mock_mcp_cls),
        patch("arms.a_guides.agent.Agent", return_value=mock_agent),
        patch("arms.a_guides.agent.BedrockModel"),
    ):
        arm_a.run(
            agent_name="expense-assistant",
            user="alice",
            turns=["test"],
            mcp_url="http://localhost:9999/mcp",
            bearer_token="sentinel-token-42",
        )

    call_kwargs = mock_mcp_cls.call_args.kwargs
    headers = call_kwargs.get("headers", {})
    assert headers.get("Authorization") == "Bearer sentinel-token-42", (
        f"Authorization header: {headers.get('Authorization')!r}"
    )
    assert list(headers.keys()) == ["Authorization"], (
        f"MCPClient received unexpected extra headers: {list(headers.keys())}"
    )


def test_arm_a_prompt_contains_policy_rules():
    """arm_a builds a system prompt that includes all policy rules P1–P7.
    PASSES against current code (confirms existing behaviour)."""
    from arms.a_guides.agent import build_system_prompt
    prompt = build_system_prompt("expense-assistant")
    for rule in ("P1", "P2", "P3", "P4", "P5", "P6", "P7"):
        assert rule in prompt, f"rule {rule!r} missing from arm A system prompt"


# ===========================================================================
# Conflicts with existing tests in test_hook.py
# ===========================================================================
#
# Adding a SCOPE check (after P6, before policy rules) that rejects when bearer_token
# is missing or has insufficient scope will BREAK the following tests in test_hook.py,
# because they create PolicyHook without a bearer_token and expect either an ALLOW or
# a denial with a rule that fires AFTER SCOPE in the check order:
#
# Tests expecting ALLOW (no bearer → SCOPE denial):
#   test_p2_over_limit_with_ref_allowed
#   test_p3_manager_allowed
#   test_p4_active_delegation_allowed
#   test_p4_same_user_no_delegation_needed
#   test_p5_approved_vendor_allowed
#   test_payments_agent_finance_passes_role_check
#   test_s1_legitimate_allowed
#
# Tests expecting a rule AFTER SCOPE (no bearer → SCOPE fires first, not the expected rule):
#   test_p1_wrong_claimant_denied           (expects P1)
#   test_p2_over_limit_no_ref_denied        (expects P2)
#   test_p2_limit_override_respected        (expects P2)
#   test_p3_self_approve_denied             (expects P3)
#   test_p3_not_manager_denied              (expects P3)
#   test_p3_expense_not_found_denied        (expects P3)
#   test_p4_no_delegation_denied            (expects P4)
#   test_p4_denied_when_directory_returns_no_delegation  (expects P4)
#   test_p4_revoked_delegation_denied       (expects P4)
#   test_p5_unapproved_vendor_denied        (expects P5)
#   test_hook_refuses_booking_when_directory_filters_expired  (expects P4)
#   test_hook_log_sim_time_equals_directory_sim_time  (expects a log entry from delegation check)
#   test_hook_delegation_503_raises_not_p4  (expects HTTPStatusError; SCOPE denies cleanly first)
#   test_error_wrapper_logs_and_reraises    (expects HTTPStatusError; SCOPE denies cleanly first)
#
# Total: 21 conflicts.
# These must be resolved before any code is built — see CLAUDE.md rule:
#   "If a test conflicts with the spec, stop and ask."


# ---------------------------------------------------------------------------
# 4b: approve_expense → expenses:approve appears only in domain/scopes.py
#     outside tests/ (FAILS against current code because domain/server.py
#     also defines it)
# ---------------------------------------------------------------------------

def test_approve_expense_scope_defined_only_in_scopes_py():
    """Repo-wide: 'approve_expense.*expenses:approve' must appear only in
    domain/scopes.py outside the tests/ directory."""
    import re
    import pathlib

    repo_root = pathlib.Path(__file__).parent.parent
    pattern = re.compile(r"approve_expense.*expenses:approve|expenses:approve.*approve_expense")
    found_in = []

    kit_snapshot = repo_root / "arms" / "approach3" / "kit_snapshot"

    for path in repo_root.rglob("*.py"):
        # skip tests/ entirely
        try:
            path.relative_to(repo_root / "tests")
            continue
        except ValueError:
            pass
        # skip the frozen kit snapshot archive (copied into kits, never executed)
        try:
            path.relative_to(kit_snapshot)
            continue
        except ValueError:
            pass

        text = path.read_text(encoding="utf-8")
        if pattern.search(text):
            found_in.append(str(path.relative_to(repo_root)))

    assert found_in == ["domain/scopes.py"], (
        f"Pairing 'approve_expense: expenses:approve' found outside tests/ in: {found_in!r}; "
        "expected only domain/scopes.py"
    )


def test_kit_snapshot_never_imported_at_runtime():
    """No code outside the kit builder may import from or read kit_snapshot/."""
    import pathlib
    import re

    repo_root = pathlib.Path(__file__).parent.parent
    kit_snapshot_re = re.compile(r"kit_snapshot")
    allowed = {
        "arms/approach3/make_kit.py",
        # gen_conformance.py reads spec_input.md from the frozen snapshot to judge
        # generations against the spec they were generated from (verification script,
        # not runtime code).
        "scripts/gen_conformance.py",
    }

    violators = []
    for path in repo_root.rglob("*.py"):
        rel = str(path.relative_to(repo_root))
        if rel in allowed:
            continue
        # skip tests/ — tests may reference kit_snapshot for verification
        try:
            path.relative_to(repo_root / "tests")
            continue
        except ValueError:
            pass
        # skip the snapshot itself
        try:
            path.relative_to(repo_root / "arms" / "approach3" / "kit_snapshot")
            continue
        except ValueError:
            pass
        if "__pycache__" in rel:
            continue
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), 1):
            if kit_snapshot_re.search(line) and not line.lstrip().startswith("#"):
                violators.append(f"{rel}:{i}: {line.strip()}")

    assert not violators, (
        "kit_snapshot/ is a frozen archive — it is copied into kits, never "
        "executed or imported at run time. Found references outside the kit "
        "builder and tests:\n" + "\n".join(violators)
    )
