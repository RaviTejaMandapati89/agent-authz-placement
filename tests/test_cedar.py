"""
Tests for arm D (Cedar boundary).

Unit tests use hand-built entities and cedarpy directly — no Bedrock, no server.
Integration tests start a subprocess server with ENFORCEMENT=cedar.
"""
import json
import pathlib
import tempfile
import time

import httpx
import pytest

import cedarpy

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_ARM_DIR   = _REPO_ROOT / "arms" / "d_boundary"

def _load_policy_text() -> str:
    return (_ARM_DIR / "policy.cedar").read_text(encoding="utf-8")


_SCHEMA       = json.loads((_ARM_DIR / "schema.json").read_text(encoding="utf-8"))
_AGENTS       = json.loads((_ARM_DIR / "agents.json").read_text(encoding="utf-8"))
_REVIEWED     = json.loads((_ARM_DIR / "reviewed_tools.json").read_text(encoding="utf-8"))

_POLICY_TEXT = _load_policy_text()
_POLICY_SET  = cedarpy.PolicySet.from_str(_POLICY_TEXT)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _entities(user: str, role: str, agent: str) -> list[dict]:
    agent_cfg = _AGENTS.get(agent, {"acts_for": "any", "allowed_tools": []})
    return [
        {
            "uid":     {"type": "ExpApp::User",  "id": user},
            "attrs":   {"username": user, "role": role},
            "parents": [],
        },
        {
            "uid":   {"type": "ExpApp::Agent", "id": agent},
            "attrs": {
                "acts_for":      agent_cfg["acts_for"],
                "allowed_tools": list(agent_cfg["allowed_tools"]),
            },
            "parents": [],
        },
    ]


def _req(user: str, agent: str, ctx: dict) -> dict:
    return {
        "principal": {"type": "ExpApp::User",  "id": user},
        "action":    "ExpApp::Action::\"call_tool\"",
        "resource":  {"type": "ExpApp::Agent", "id": agent},
        "context":   ctx,
    }


def _eval(user: str, role: str, agent: str, ctx: dict) -> cedarpy.AuthzResult:
    return cedarpy.is_authorized(
        request=_req(user, agent, ctx),
        policies=_POLICY_SET,
        entities=_entities(user, role, agent),
    )


def _rule(result: cedarpy.AuthzResult) -> str | None:
    annotations = result.diagnostics.id_annotations_by_reason
    return next(iter(annotations.values()), None) if annotations else None


def _base_ctx(action: str, **extra) -> dict:
    return {"action_name": action, "tool_reviewed": True, **extra}


# ---------------------------------------------------------------------------
# schema validation
# ---------------------------------------------------------------------------

def test_schema_validates():
    result = cedarpy.validate_policies(_POLICY_TEXT, json.dumps(_SCHEMA))
    assert result.validation_passed, result.errors


# ---------------------------------------------------------------------------
# P7: default deny — unlisted tool
# ---------------------------------------------------------------------------

def test_p7_unlisted_tool_denied():
    r = _eval("alice", "employee", "expense-assistant",
              _base_ctx("export_all_expenses"))
    assert not r.allowed
    # no permit fired → no annotations in reasons
    assert r.diagnostics.id_annotations_by_reason == {}


def test_p7_unlisted_tool_reason_mapped_to_no_permit():
    """PEP maps empty reasons to P7 'no permit'; verify the raw result here."""
    r = _eval("alice", "employee", "expense-assistant",
              _base_ctx("export_all_expenses"))
    assert not r.allowed
    assert r.diagnostics.reasons == []


# ---------------------------------------------------------------------------
# P7: blank user forbidden
# ---------------------------------------------------------------------------

def test_p7_blank_user_denied():
    r = _eval("", "", "expense-assistant",
              _base_ctx("submit_expense", claimant="", amount_pence=100,
                        has_approval_ref=False))
    assert not r.allowed
    assert _rule(r) == "P7"


# ---------------------------------------------------------------------------
# P7: acts_for gate (payments-agent requires finance role)
# ---------------------------------------------------------------------------

def test_p7_non_finance_denied():
    r = _eval("alice", "employee", "payments-agent",
              _base_ctx("pay_vendor", vendor="acme-hotels", vendor_approved=True))
    assert not r.allowed
    # permit didn't fire because role != "finance"
    assert r.diagnostics.reasons == []


def test_p7_finance_allowed():
    r = _eval("erin", "finance", "payments-agent",
              _base_ctx("pay_vendor", vendor="acme-hotels", vendor_approved=True))
    assert r.allowed
    assert _rule(r) == "P7"


# ---------------------------------------------------------------------------
# P6: tool_reviewed = False
# ---------------------------------------------------------------------------

def test_p6_unreviewed_tool_denied():
    ctx = {"action_name": "submit_expense", "tool_reviewed": False,
           "claimant": "alice", "amount_pence": 100, "has_approval_ref": False}
    r = _eval("alice", "employee", "expense-assistant", ctx)
    assert not r.allowed
    assert _rule(r) == "P6"


# ---------------------------------------------------------------------------
# P1: claimant must equal user
# ---------------------------------------------------------------------------

def test_p1_wrong_claimant_denied():
    r = _eval("alice", "employee", "expense-assistant",
              _base_ctx("submit_expense", claimant="bob",
                        amount_pence=100, has_approval_ref=False))
    assert not r.allowed
    assert _rule(r) == "P1"


def test_p1_correct_claimant_allowed():
    r = _eval("alice", "employee", "expense-assistant",
              _base_ctx("submit_expense", claimant="alice",
                        amount_pence=100, has_approval_ref=False))
    assert r.allowed


# ---------------------------------------------------------------------------
# P2: expense limit (50000 pence = £500)
# ---------------------------------------------------------------------------

def test_p2_over_limit_pence_no_ref_denied():
    r = _eval("alice", "employee", "expense-assistant",
              _base_ctx("submit_expense", claimant="alice",
                        amount_pence=60000, has_approval_ref=False))
    assert not r.allowed
    assert _rule(r) == "P2"


def test_p2_over_limit_with_ref_allowed():
    r = _eval("alice", "employee", "expense-assistant",
              _base_ctx("submit_expense", claimant="alice",
                        amount_pence=60000, has_approval_ref=True))
    assert r.allowed


def test_p2_at_limit_no_ref_allowed():
    r = _eval("alice", "employee", "expense-assistant",
              _base_ctx("submit_expense", claimant="alice",
                        amount_pence=50000, has_approval_ref=False))
    assert r.allowed


def test_p2_amount_pence_rounding():
    """500.10 should round to 50010 pence, which is over the limit."""
    from arms.d_boundary.pep import fingerprint as _fp  # noqa: F401 — ensure module importable
    pence = round(500.10 * 100)
    assert pence == 50010
    r = _eval("alice", "employee", "expense-assistant",
              _base_ctx("submit_expense", claimant="alice",
                        amount_pence=pence, has_approval_ref=False))
    assert not r.allowed
    assert _rule(r) == "P2"


def test_p2_limit_change_enforced():
    """S7: replace 50000 with 30000 in policy text; £400 = 40000 pence now denied."""
    fresh_text = (_ARM_DIR / "policy.cedar").read_text(encoding="utf-8")
    modified = fresh_text.replace("50000", "30000")
    ps = cedarpy.PolicySet.from_str(modified)
    r = cedarpy.is_authorized(
        request=_req("alice", "expense-assistant",
                     _base_ctx("submit_expense", claimant="alice",
                               amount_pence=40000, has_approval_ref=False)),
        policies=ps,
        entities=_entities("alice", "employee", "expense-assistant"),
    )
    assert not r.allowed
    assert _rule(r) == "P2"


def test_p2_literal_count_assertion():
    """apply_in_memory_limit raises if the literal does not appear exactly once."""
    from arms.d_boundary.pep import apply_in_memory_limit, reset_policy, _load_state

    # _load_state() initialises module-level policy text without touching
    # domain.server.authorise, so the shared in-process server is unaffected.
    _load_state()

    # First change succeeds (50000 → 40000).
    apply_in_memory_limit(400)

    # Second change must fail: 50000 is gone, replaced by 40000.
    with pytest.raises(AssertionError, match="expected exactly 1"):
        apply_in_memory_limit(300)

    reset_policy()  # restore for other tests


# ---------------------------------------------------------------------------
# P3: approve_expense
# ---------------------------------------------------------------------------

def test_p3_self_approve_denied():
    r = _eval("alice", "employee", "expense-assistant",
              _base_ctx("approve_expense",
                        expense_claimant="alice",
                        expense_claimant_manager="bob"))
    assert not r.allowed
    assert _rule(r) == "P3"


def test_p3_not_manager_denied():
    r = _eval("carol", "executive", "expense-assistant",
              _base_ctx("approve_expense",
                        expense_claimant="alice",
                        expense_claimant_manager="bob"))
    assert not r.allowed
    assert _rule(r) == "P3"


def test_p3_manager_allowed():
    r = _eval("bob", "manager", "expense-assistant",
              _base_ctx("approve_expense",
                        expense_claimant="alice",
                        expense_claimant_manager="bob"))
    assert r.allowed


def test_p3_missing_expense_data_allows():
    """Unknown expense ID → PEP omits expense_claimant/manager → Cedar allows.
    Verifies the PEP does not raise; Cedar is the decision authority."""
    # Simulate: context has no expense_claimant or expense_claimant_manager.
    r = _eval("alice", "employee", "expense-assistant",
              _base_ctx("approve_expense"))
    # P3 guards both use 'context has expense_claimant', so neither fires.
    # P7 permit fires (tool in allowed_tools, acts_for=any).
    assert r.allowed


# ---------------------------------------------------------------------------
# P4: book_travel — delegation
# ---------------------------------------------------------------------------

def test_p4_active_delegation_allowed():
    r = _eval("dan", "assistant", "travel-assistant",
              _base_ctx("book_travel", traveller="carol",
                        travel_delegators=["carol"]))
    assert r.allowed


def test_p4_no_delegation_denied():
    r = _eval("dan", "assistant", "travel-assistant",
              _base_ctx("book_travel", traveller="carol",
                        travel_delegators=[]))
    assert not r.allowed
    assert _rule(r) == "P4"


def test_p4_expired_delegation_denied():
    # Expired → PEP excludes delegator from travel_delegators set.
    r = _eval("dan", "assistant", "travel-assistant",
              _base_ctx("book_travel", traveller="carol",
                        travel_delegators=[]))
    assert not r.allowed
    assert _rule(r) == "P4"


def test_p4_revoked_delegation_denied():
    r = _eval("dan", "assistant", "travel-assistant",
              _base_ctx("book_travel", traveller="carol",
                        travel_delegators=[]))
    assert not r.allowed
    assert _rule(r) == "P4"


def test_p4_self_booking_allowed():
    r = _eval("dan", "assistant", "travel-assistant",
              _base_ctx("book_travel", traveller="dan",
                        travel_delegators=[]))
    assert r.allowed


# ---------------------------------------------------------------------------
# P5: pay_vendor
# ---------------------------------------------------------------------------

def test_p5_approved_vendor_allowed():
    r = _eval("erin", "finance", "payments-agent",
              _base_ctx("pay_vendor", vendor="acme-hotels", vendor_approved=True))
    assert r.allowed


def test_p5_unapproved_vendor_denied():
    r = _eval("erin", "finance", "payments-agent",
              _base_ctx("pay_vendor", vendor="rogue-vendor", vendor_approved=False))
    assert not r.allowed
    assert _rule(r) == "P5"


# ---------------------------------------------------------------------------
# integration: pdp-down refused and logged
# ---------------------------------------------------------------------------

def _mcp_call(base_url: str, tool: str, args: dict, user: str, agent: str) -> dict:
    common = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
        "X-User": user,
        "X-Agent": agent,
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        r = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        }, headers=common)
        r.raise_for_status()
        sid = r.headers.get("mcp-session-id")
        hdrs = {**common, **({"mcp-session-id": sid} if sid else {})}
        r2 = client.post("/mcp", json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": tool, "arguments": args},
        }, headers=hdrs)
        r2.raise_for_status()
        return r2.json()


def _read_decisions(log_path: str, run_type: str = "decision") -> list[dict]:
    p = pathlib.Path(log_path)
    if not p.exists():
        return []
    return [
        json.loads(line)
        for line in p.read_text(encoding="utf-8").splitlines()
        if line.strip() and json.loads(line).get("type") == run_type
    ]


@pytest.mark.integration
def test_pdp_down_refused_and_logged():
    from runner._server import domain_server
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
        log = tmp.name
    with domain_server(log, env_extra={"ENFORCEMENT": "cedar"}) as (_, base_url):
        httpx.post(f"{base_url}/control/pdp-down", json={"down": True}).raise_for_status()
        resp = _mcp_call(base_url, "read_receipt", {"receipt_id": "rcpt-001"},
                         user="alice", agent="expense-assistant")
        # MCP returns an error result when the tool raises
        body = resp.get("result", resp)
        assert body.get("isError") or "error" in resp

    decisions = _read_decisions(log, "decision")
    assert decisions, "no decision log entries written"
    last = decisions[-1]
    assert last["decision"] == "error"

    outcomes = _read_decisions(log, "outcome")
    assert outcomes
    last_out = outcomes[-1]
    assert last_out["executed"] is False


@pytest.mark.integration
def test_p6_all_reviewed_tools_pass():
    """Every tool in reviewed_tools.json must be free of P6 denial when called
    with valid arguments in arm D mode."""
    from runner._server import domain_server

    # Valid call arguments per tool; these pass all rules so only P6 could fire.
    valid_calls = [
        ("read_receipt",   {"receipt_id": "rcpt-001"},
         "alice", "expense-assistant"),
        ("submit_expense", {"claimant": "alice", "amount": 120.0,
                            "description": "office supplies", "receipt_id": "rcpt-001"},
         "alice", "expense-assistant"),
        ("approve_expense", {"expense_id": "exp-001"},
         "bob", "expense-assistant"),
        ("book_travel",    {"traveller": "dan", "details": "LHR-CDG"},
         "dan", "travel-assistant"),
        ("pay_vendor",     {"vendor": "acme-hotels", "amount": 100.0,
                            "reference": "ref-001"},
         "erin", "payments-agent"),
    ]

    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
        log = tmp.name

    with domain_server(log, env_extra={"ENFORCEMENT": "cedar"}) as (_, base_url):
        for tool, args, user, agent in valid_calls:
            _mcp_call(base_url, tool, args, user=user, agent=agent)

    decisions = _read_decisions(log, "decision")
    p6_denials = [d for d in decisions if d.get("rule") == "P6"]
    assert p6_denials == [], f"unexpected P6 denials: {p6_denials}"


@pytest.mark.integration
def test_p3_alice_approves_own_denied():
    """Direct MCP call: alice approving her own expense is refused under P3."""
    from runner._server import domain_server
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
        log = tmp.name
    with domain_server(log, env_extra={"ENFORCEMENT": "cedar"}) as (_, base_url):
        _mcp_call(base_url, "approve_expense", {"expense_id": "exp-001"},
                  user="alice", agent="expense-assistant")

    decisions = _read_decisions(log, "decision")
    assert decisions
    last = decisions[-1]
    assert last["decision"] == "deny"
    assert last["rule"] == "P3"
    assert last["user"] == "alice"
