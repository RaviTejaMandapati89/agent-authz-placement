"""Tests for approach 6 (task 2d).

Every test maps to a numbered spec item in CLAUDE.md.  All tests must FAIL
until the implementation is added; none may be skipped or weakened.

Naming convention: test_a6_<spec_item>_<description>
"""
import importlib
import json
import pathlib

import httpx
import pytest
import pytest_asyncio

from domain import simclock, tokens
from domain.simclock import _EPOCH as _SIM_EPOCH
from domain.grants import AGENT_GRANTS as GATEWAY_GRANTS

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

_REPO            = pathlib.Path(__file__).parent.parent
_A5_POLICIES_DIR = _REPO / "arms" / "shared" / "policies"
_CENTRAL_POLICY  = _REPO / "domain" / "central_policy.cedar"
_SHARED_FP       = _REPO / "domain" / "fingerprints.json"
_USE_CASES       = ["expenses", "travel", "payments"]

TOOL_SCOPES: dict[str, str] = {
    "read_receipt":    "expenses:read",
    "submit_expense":  "expenses:submit",
    "approve_expense": "expenses:approve",
    "book_travel":     "travel:book",
    "pay_vendor":      "payments:pay",
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode_unverified(token: str) -> dict:
    import jwt as _jwt
    return _jwt.decode(token, options={"verify_signature": False})


async def _issue_user_token(ctrl, sub: str, aud: str,
                             scopes=None, lifetime: int = 300) -> str:
    if scopes is None:
        scopes = sorted(tokens.FIXED_SCOPES)
    r = await ctrl.post("/control/identity/user-token", json={
        "sub": sub, "aud": aud, "scope": scopes, "lifetime": lifetime,
    })
    r.raise_for_status()
    return r.json()["access_token"]


async def _issue_agent_token(ctrl, sub: str, scopes=None,
                              lifetime: int = 300) -> str:
    if scopes is None:
        scopes = sorted(tokens.FIXED_SCOPES)
    r = await ctrl.post("/control/identity/agent-token", json={
        "sub": sub, "scope": scopes, "lifetime": lifetime,
    })
    r.raise_for_status()
    return r.json()["access_token"]


async def _exchange(ctrl, subject_token: str, actor_token: str,
                    audience: str | None = None) -> str:
    body: dict = {"subject_token": subject_token, "actor_token": actor_token}
    if audience:
        body["audience"] = audience
    r = await ctrl.post("/identity/exchange", json=body)
    r.raise_for_status()
    return r.json()["access_token"]


async def _make_bearer(ctrl, user: str, agent: str,
                       scopes=None, lifetime: int = 7200) -> str:
    if scopes is None:
        scopes = sorted(tokens.FIXED_SCOPES)
    user_tok  = await _issue_user_token(ctrl, user, agent, scopes, lifetime)
    agent_tok = await _issue_agent_token(ctrl, agent, scopes, lifetime)
    return await _exchange(ctrl, user_tok, agent_tok)


def _last_decision(log_path: pathlib.Path) -> dict:
    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    decisions = [l for l in lines if l.get("type") == "decision"]
    assert decisions, "no decision lines in log"
    return decisions[-1]


def _all_decisions(log_path: pathlib.Path) -> list[dict]:
    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    return [l for l in lines if l.get("type") == "decision"]


def _fingerprints_for_a6() -> dict[str, str]:
    from domain.gateway import _compute_fingerprint
    from domain.server import mcp
    return {
        name: _compute_fingerprint(tool.name, tool.description, tool.parameters)
        for name, tool in mcp._tool_manager._tools.items()
    }


# ---------------------------------------------------------------------------
# Gateway session helper
# ---------------------------------------------------------------------------

class _GwSession:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._session_id: str | None = None
        self._n = 0

    async def initialize(self) -> None:
        self._n += 1
        r = await self._post({"jsonrpc": "2.0", "id": self._n,
                               "method": "initialize",
                               "params": {"protocolVersion": "2025-11-25",
                                          "capabilities": {},
                                          "clientInfo": {"name": "a6-test", "version": "0"}}})
        self._session_id = r.headers.get("mcp-session-id")

    async def call_tool(self, name: str, arguments: dict) -> dict:
        self._n += 1
        r = await self._post({"jsonrpc": "2.0", "id": self._n,
                               "method": "tools/call",
                               "params": {"name": name, "arguments": arguments}})
        return r.json().get("result", r.json())

    async def _post(self, payload: dict) -> httpx.Response:
        hdrs: dict = {"Content-Type": "application/json",
                      "Accept": "application/json, text/event-stream",
                      "MCP-Protocol-Version": "2025-11-25"}
        if self._session_id:
            hdrs["mcp-session-id"] = self._session_id
        r = await self._client.post("/gateway/mcp", json=payload, headers=hdrs)
        r.raise_for_status()
        return r


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def a6_gw_app(running_app, log_path):
    """Install the gateway with the approach6 plugin."""
    from arms.approach6 import plugin as _a6_plugin
    from domain import server as _srv
    from domain.gateway import install_gateway, remove_gateway

    _a6_plugin.reset()
    _srv.register_revocation_listener(_a6_plugin.push_revocation)
    _srv.register_reset_callback(_a6_plugin.reset)

    install_gateway(
        grants=GATEWAY_GRANTS,
        fingerprints=_fingerprints_for_a6(),
        scope_map=TOOL_SCOPES,
        policy_plugin=_a6_plugin.evaluate,
    )
    yield running_app
    remove_gateway()
    _srv.deregister_revocation_listener(_a6_plugin.push_revocation)
    _srv.deregister_reset_callback(_a6_plugin.reset)


@pytest_asyncio.fixture
async def a6_ctrl(a6_gw_app) -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as client:
        yield client


async def _a6_session(app, bearer: str) -> _GwSession:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    )
    session = _GwSession(client)
    await session.initialize()
    return session


# ===========================================================================
# Spec item 1 — Shared use-case policies
# ===========================================================================

def test_a6_i1_use_case_policies_shared_with_approach5():
    """approach6 reads use-case policies from the same directory as approach5 (not a copy)."""
    from arms.approach6 import plugin as _a6_plugin
    assert _a6_plugin._POLICIES_DIR.resolve() == _A5_POLICIES_DIR.resolve(), (
        f"approach6 plugin must use the same policies directory as approach5; "
        f"got {_a6_plugin._POLICIES_DIR}"
    )


def test_a6_i1_use_case_files_exist():
    """All three use-case policy files are accessible from approach6."""
    from arms.approach6 import plugin as _a6_plugin
    for name in _USE_CASES:
        p = _a6_plugin._POLICIES_DIR / f"{name}.cedar"
        assert p.exists(), f"approach6 must be able to read {name}.cedar at {p}"


def test_a6_i1_payments_policy_has_p7():
    """approach6's payments use-case policy has P7 (finance-only forbid)."""
    import re
    from arms.approach6 import plugin as _a6_plugin
    text = (_a6_plugin._POLICIES_DIR / "payments.cedar").read_text()
    ids = set(re.findall(r'@id\("([^"]+)"\)', text))
    assert "P7" in ids, f"payments.cedar must have P7; found: {ids}"


def test_a6_i1_shared_policies_dir_exists():
    """arms/shared/policies/ must exist after approved change A."""
    _SHARED = _REPO / "arms" / "shared" / "policies"
    assert _SHARED.is_dir(), (
        f"arms/shared/policies/ must exist (approved change A — moved from "
        f"arms/approach5/policies); not found at {_SHARED}"
    )


def test_a6_i1_approach5_policies_dir_removed():
    """arms/approach5/policies must not exist — policies moved to arms/shared/policies."""
    _OLD = _REPO / "arms" / "approach5" / "policies"
    assert not _OLD.exists(), (
        f"arms/approach5/policies must not exist after approved change A; "
        f"still found at {_OLD}"
    )


def test_a6_i1_approach5_plugin_loads_from_shared():
    """approach5 plugin must load use-case policies from arms/shared/policies."""
    _SHARED = _REPO / "arms" / "shared" / "policies"
    from arms.approach5 import plugin as _a5_plugin
    assert _a5_plugin._POLICIES_DIR.resolve() == _SHARED.resolve(), (
        f"approach5 plugin must use arms/shared/policies; "
        f"got {_a5_plugin._POLICIES_DIR}"
    )


# ===========================================================================
# Spec item 2 — Published copy of central policy
# ===========================================================================

async def test_a6_i2_initial_copy_exists_at_start_version_1_at_time_0(
    a6_ctrl, a6_gw_app, log_path,
):
    """A copy exists at server start: version 1, at simulated time 0 (the first
    schedule boundary), before any decision is made."""
    from arms.approach6 import plugin as _a6_plugin
    assert simclock.now() == _SIM_EPOCH, "the clock must still be at simulated time 0"
    assert not log_path.exists() or log_path.read_text().strip() == "", (
        "no decision may have been made yet"
    )
    assert _a6_plugin._snapshot is not None, "a copy must exist at start, before any decision"
    assert _a6_plugin._snapshot_version == 1, (
        f"the start copy must be version 1; got {_a6_plugin._snapshot_version}"
    )
    assert _a6_plugin._snapshot["version"] == 1


async def test_a6_i2_snapshot_contains_expense_limit_and_vendors(
    a6_ctrl, a6_gw_app, log_path,
):
    """The snapshot contains expense_limit and approved_vendors from live state."""
    bearer = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        await session.call_tool("submit_expense",
                                {"claimant": "alice", "amount": 50.0, "description": "snap"})

    from arms.approach6 import plugin as _a6_plugin
    snap = _a6_plugin._snapshot
    assert snap is not None
    assert "expense_limit" in snap, "snapshot must contain expense_limit"
    assert "approved_vendors" in snap, "snapshot must contain approved_vendors"
    assert snap["expense_limit"] == 500, f"initial limit must be 500; got {snap['expense_limit']}"


async def test_a6_i2_limit_not_in_snapshot_until_next_publication(
    a6_ctrl, a6_gw_app, log_path,
):
    """Lowering the live expense limit is NOT seen by approach6 until the next publication."""
    # Trigger initial snapshot (limit=500)
    bearer = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        # £200 < £500 → allowed
        r1 = await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 200.0, "description": "before limit change"},
        )
    assert r1.get("isError") is not True, f"initial £200 should be allowed; got: {r1}"

    # Lower limit to £100 in live state
    await a6_ctrl.post("/control/set-limit", json={"limit": 100})

    # Call again BEFORE next publication — still uses old snapshot limit=500
    bearer2 = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer2}"},
    ) as client:
        session2 = _GwSession(client)
        await session2.initialize()
        r2 = await session2.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 200.0, "description": "after limit change before pub"},
        )
    assert r2.get("isError") is not True, (
        "£200 expense must still be allowed: new limit=100 not yet published; "
        f"got: {r2}"
    )


# ===========================================================================
# Spec item 3 — Person facts from token
# ===========================================================================

async def test_a6_i3_role_comes_from_token_not_live_state(
    a6_ctrl, a6_gw_app, log_path,
):
    """P7 (finance-only) is evaluated against the token role, not live state.
    alice (employee) via payments-agent should be denied by P7 even if we do not
    call /directory/users at all."""
    bearer = await _make_bearer(a6_ctrl, "alice", "payments-agent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "pay_vendor",
            {"vendor": "acme-hotels", "amount": 100.0, "reference": "ref-r"},
        )
    assert result.get("isError") is True, f"alice (employee) via payments-agent must be denied; got: {result}"
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P7", f"denial must be P7 (from token role); got: {d['rule']!r}"


async def test_a6_i3_delegation_from_token_when_not_revoked(
    a6_ctrl, a6_gw_app, log_path,
):
    """dan can book travel for carol using the delegation in the token (no revocation)."""
    bearer = await _make_bearer(a6_ctrl, "dan", "travel-assistant")
    # Trigger snapshot
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "book_travel",
            {"traveller": "carol", "details": "Paris, 2027-03-01"},
        )
    assert result.get("isError") is not True, (
        f"dan should be allowed to book for carol (delegation in token); got: {result}"
    )


async def test_a6_i3_approach6_uses_token_not_live_state_for_delegation(
    a6_ctrl, a6_gw_app, log_path,
):
    """approach6 reads delegation from token claims, unlike approach5.

    Issue token for dan while carol's delegation is active.
    Revoke the delegation in live state via /control/revoke-delegation.
    For approach6, since the revocation event hasn't reached 1-second delivery yet,
    the token's delegation claim is still honoured (same-tick call, t < revoke_time+1).
    This is the opposite of approach5.
    """
    bearer = await _make_bearer(a6_ctrl, "dan", "travel-assistant")

    # Trigger snapshot (ensure plugin is initialised)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session0 = _GwSession(client)
        await session0.initialize()
        r0 = await session0.call_tool("book_travel", {"traveller": "carol", "details": "pre"})
    assert r0.get("isError") is not True

    # Revoke delegation in live state at current sim_time T0
    r = await a6_ctrl.post("/control/revoke-delegation", json={"delegation_id": "del-001"})
    r.raise_for_status()

    # Immediately call with SAME token (clock still at T0, event delivery at T0+1)
    # approach6 uses token claim → still ALLOWED (event not delivered yet)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session1 = _GwSession(client)
        await session1.initialize()
        result = await session1.call_tool(
            "book_travel",
            {"traveller": "carol", "details": "Rome, 2027-05-01"},
        )

    assert result.get("isError") is not True, (
        "approach6 must honour token delegation before revocation event is delivered "
        "(same tick, < 1 second). "
        f"Got: {result}"
    )


# ===========================================================================
# Spec item 4 — Revocation events (1-second delivery window)
# ===========================================================================

async def test_a6_i4_revocation_event_registered_on_revoke(
    a6_ctrl, a6_gw_app,
):
    """After POST /control/revoke-delegation, the approach6 plugin has a pending event."""
    from arms.approach6 import plugin as _a6_plugin
    assert len(_a6_plugin._pending_events) == 0, "no events before revocation"

    await a6_ctrl.post("/control/revoke-delegation", json={"delegation_id": "del-001"})

    assert len(_a6_plugin._pending_events) == 1, (
        f"one pending event after revocation; got: {_a6_plugin._pending_events}"
    )
    event = _a6_plugin._pending_events[0]
    assert event.get("delegator") == "carol", f"event must identify delegator; got: {event}"
    assert event.get("delegate") == "dan", f"event must identify delegate; got: {event}"


async def test_a6_i4_revocation_event_delivery_time_is_one_second_later(
    a6_ctrl, a6_gw_app,
):
    """The revocation event delivery time is exactly 1 simulated second after revocation."""
    from arms.approach6 import plugin as _a6_plugin
    sim_time_before = simclock.now()
    await a6_ctrl.post("/control/revoke-delegation", json={"delegation_id": "del-001"})
    event = _a6_plugin._pending_events[0]
    expected_delivery = sim_time_before + 1.0
    assert abs(event["delivery_time"] - expected_delivery) < 0.001, (
        f"delivery_time must be revocation_time + 1.0; "
        f"got {event['delivery_time']}, expected {expected_delivery}"
    )


# ===========================================================================
# Spec item 5 — No per-decision call to /central/decide
# ===========================================================================

async def test_a6_i5_no_central_decide_call_per_decision(
    a6_ctrl, a6_gw_app, log_path,
):
    """approach6 never calls /central/decide. The counter stays 0 throughout."""
    r0 = await a6_ctrl.get("/control/central-decide-count")
    assert r0.json()["count"] == 0, "counter must be 0 after reset"

    bearer = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 50.0, "description": "no central call"},
        )

    r1 = await a6_ctrl.get("/control/central-decide-count")
    assert r1.json()["count"] == 0, (
        f"approach6 must not call /central/decide; counter must stay 0; "
        f"got {r1.json()['count']}"
    )


async def test_a6_i5_central_called_false_in_decision_log(
    a6_ctrl, a6_gw_app, log_path,
):
    """Every approach6 decision log line has central_called=False."""
    bearer = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 100.0, "description": "central_called check"},
        )
    d = _last_decision(log_path)
    assert "central_called" in d, f"decision line must have central_called field; got: {d}"
    assert d["central_called"] is False, (
        f"approach6 must have central_called=False; got: {d['central_called']!r}"
    )


async def test_a6_i5_decision_log_has_copy_version(
    a6_ctrl, a6_gw_app, log_path,
):
    """Decision log lines for approach6 include the version of the published copy used."""
    bearer = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 100.0, "description": "copy version"},
        )
    d = _last_decision(log_path)
    assert "central_copy_version" in d, (
        f"decision line must have central_copy_version field; got keys: {list(d.keys())}"
    )
    assert isinstance(d["central_copy_version"], int), (
        f"central_copy_version must be an int; got: {d['central_copy_version']!r}"
    )
    assert d["central_copy_version"] >= 1, (
        f"central_copy_version must be >= 1 after first publication; got: {d['central_copy_version']}"
    )


async def test_a6_i5_decision_log_has_revocation_applied(
    a6_ctrl, a6_gw_app, log_path,
):
    """Decision log lines for approach6 include the revocation_applied flag."""
    bearer = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 100.0, "description": "revocation_applied check"},
        )
    d = _last_decision(log_path)
    assert "revocation_applied" in d, (
        f"decision line must have revocation_applied field; got keys: {list(d.keys())}"
    )
    assert isinstance(d["revocation_applied"], bool), (
        f"revocation_applied must be a bool; got: {d['revocation_applied']!r}"
    )


# ===========================================================================
# Spec item 6 — Central source down: keep deciding from last copy
# ===========================================================================

async def test_a6_i6_central_down_still_allows_from_last_copy(
    a6_ctrl, a6_gw_app, log_path,
):
    """With central source down, a legitimate call is ALLOWED from the last copy.
    Unlike approach5, approach6 does NOT return CENTRAL_UNAVAILABLE when central is down."""
    # Step 1: trigger initial snapshot while central is up
    bearer = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        r0 = await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 100.0, "description": "before down"},
        )
    assert r0.get("isError") is not True

    # Step 2: take central down
    await a6_ctrl.post("/control/directory-down", json={"down": True})

    # Step 3: legitimate call — should still be ALLOWED from last copy
    bearer2 = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer2}"},
    ) as client:
        session2 = _GwSession(client)
        await session2.initialize()
        result = await session2.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 100.0, "description": "central down"},
        )

    assert result.get("isError") is not True, (
        "approach6: with central down, a legitimate call must still be ALLOWED "
        f"from the last copy; got: {result}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "allow", (
        f"approach6 must allow from last copy when central is down; got: {d}"
    )
    assert d.get("central_called") is False, (
        f"central_called must be False even when central is down; got: {d}"
    )


async def test_a6_i6_central_down_at_start_no_copy_fails_closed(
    a6_ctrl, a6_gw_app, log_path,
):
    """Central source down at server start: no copy is ever published, so
    person-level decisions fail closed with CENTRAL_UNAVAILABLE."""
    from arms.approach6 import plugin as _a6_plugin

    await a6_ctrl.post("/control/directory-down", json={"down": True})
    _a6_plugin.reset()  # server start with the source already down
    assert _a6_plugin._snapshot is None, "no copy at start when the source is down"

    bearer = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 50.0, "description": "no copy"},
        )

    assert result.get("isError") is True, (
        f"With no copy and central down, must return error; got: {result}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "CENTRAL_UNAVAILABLE"


# ===========================================================================
# Spec item 8 — Combining rules (same as approach 5)
# ===========================================================================

async def test_a6_i8_central_refusal_wins_over_use_case_permit(
    a6_ctrl, a6_gw_app, log_path,
):
    """Central P1 overrides a use-case permit: alice submitting for bob is denied (P1)."""
    bearer = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "submit_expense",
            {"claimant": "bob", "amount": 50.0, "description": "P1 test"},
        )
    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P1", f"central P1 must override; got: {d['rule']!r}"


async def test_a6_i8_use_case_refusal_wins_when_central_permits(
    a6_ctrl, a6_gw_app, log_path,
):
    """P7 (use-case) refuses alice via payments-agent when central permits."""
    bearer = await _make_bearer(a6_ctrl, "alice", "payments-agent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "pay_vendor",
            {"vendor": "acme-hotels", "amount": 50.0, "reference": "ref-uc"},
        )
    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P7", f"use-case P7 must refuse; got: {d['rule']!r}"


async def test_a6_i8_log_central_rule_when_both_refuse(
    a6_ctrl, a6_gw_app, log_path,
):
    """When both central (P5) and use-case (P7) refuse, log the central rule (P5)."""
    bearer = await _make_bearer(a6_ctrl, "alice", "payments-agent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "pay_vendor",
            {"vendor": "rogue-vendor", "amount": 50.0, "reference": "ref-both"},
        )
    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P5", (
        f"when both refuse, central rule (P5) must be logged; got: {d['rule']!r}"
    )


# ===========================================================================
# Spec item 9 — Runner
# ===========================================================================

def test_a6_i9_runner_starts_gateway_with_approach6_plugin(tmp_path):
    """The runner's domain_server with GATEWAY_PLUGIN=approach6 starts and serves /gateway/mcp."""
    from runner._server import domain_server

    decision_log = str(tmp_path / "decisions.jsonl")
    with domain_server(
        decision_log,
        env_extra={"GATEWAY": "true", "GATEWAY_PLUGIN": "approach6"},
    ) as (port, base_url):
        resp = httpx.post(
            f"{base_url}/gateway/mcp",
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test-a6-runner", "version": "0"},
                },
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert resp.status_code == 200, (
        f"approach6 runner server must serve /gateway/mcp; "
        f"got HTTP {resp.status_code}: {resp.text[:400]}"
    )


# ===========================================================================
# Acceptance tests
# ===========================================================================

async def test_a6_acc_publication_window_limit_not_applied_until_publication(
    a6_ctrl, a6_gw_app, log_path,
):
    """ACCEPTANCE (S7/S10 window): A centrally-lowered expense limit is NOT applied
    until the simulated clock passes the next publication time, then IS applied.
    """
    # Trigger first snapshot (limit=500)
    bearer = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        r0 = await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 200.0, "description": "init snap"},
        )
    assert r0.get("isError") is not True, f"precondition: £200 < limit=500 → allowed; got: {r0}"

    # Lower limit to £100 centrally
    await a6_ctrl.post("/control/set-limit", json={"limit": 100})

    # S7: Before next publication — new limit NOT applied
    bearer2 = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer2}"},
    ) as client:
        session2 = _GwSession(client)
        await session2.initialize()
        r1 = await session2.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 200.0, "description": "before pub"},
        )
    assert r1.get("isError") is not True, (
        "ACCEPTANCE S7: limit=100 not yet published; £200 must still be allowed "
        f"(snapshot still has limit=500); got: {r1}"
    )

    # Advance clock past publication interval (15 simulated minutes)
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 900})

    # S10: After publication — new limit IS applied
    bearer3 = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer3}"},
    ) as client:
        session3 = _GwSession(client)
        await session3.initialize()
        r2 = await session3.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 200.0, "description": "after pub"},
        )
    assert r2.get("isError") is True, (
        "ACCEPTANCE S10: limit=100 now published; £200 must be denied (P2); "
        f"got: {r2}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P2", (
        f"ACCEPTANCE: rule must be P2 (expense limit); got: {d['rule']!r}"
    )


async def test_a6_acc_revocation_window_honoured_before_one_second_denied_after(
    a6_ctrl, a6_gw_app, log_path,
):
    """ACCEPTANCE (S5 window): A revoked delegation is still honoured less than
    1 simulated second after revocation (event not yet delivered), and refused
    with the SAME token once the clock passes 1 second (event delivered).
    """
    bearer = await _make_bearer(a6_ctrl, "dan", "travel-assistant", lifetime=7200)
    claims = _decode_unverified(bearer)
    assert any(
        d.get("delegator") == "carol"
        for d in claims.get("delegations_received", [])
    ), "Precondition: token must carry carol→dan travel delegation"

    # Trigger snapshot so plugin is initialised
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session0 = _GwSession(client)
        await session0.initialize()
        r0 = await session0.call_tool("book_travel",
                                       {"traveller": "carol", "details": "pre-revoke"})
    assert r0.get("isError") is not True, f"precondition: booking allowed; got: {r0}"

    # Revoke at current sim_time T0 → event delivery at T0+1
    r = await a6_ctrl.post("/control/revoke-delegation", json={"delegation_id": "del-001"})
    r.raise_for_status()

    # At T0 (no advance): event not yet delivered → ALLOWED
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session1 = _GwSession(client)
        await session1.initialize()
        r1 = await session1.call_tool("book_travel",
                                       {"traveller": "carol", "details": "at T0"})
    assert r1.get("isError") is not True, (
        "ACCEPTANCE S5: delegation must still be honoured at T0 (event delivery at T0+1); "
        f"got: {r1}"
    )

    # Advance to T0+0.9 → still before delivery
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 0.9})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session2 = _GwSession(client)
        await session2.initialize()
        r2 = await session2.call_tool("book_travel",
                                       {"traveller": "carol", "details": "at T0+0.9"})
    assert r2.get("isError") is not True, (
        "ACCEPTANCE S5: delegation still valid at T0+0.9 (< 1 second); "
        f"got: {r2}"
    )

    # Advance to T0+1.0 → event delivered
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 0.1})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session3 = _GwSession(client)
        await session3.initialize()
        r3 = await session3.call_tool("book_travel",
                                       {"traveller": "carol", "details": "at T0+1.0"})
    assert r3.get("isError") is True, (
        "ACCEPTANCE S5: delegation must be refused once clock reaches T0+1.0 "
        f"(event delivered); got: {r3}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P4", f"denial must be P4; got: {d['rule']!r}"
    assert d.get("revocation_applied") is True, (
        f"revocation_applied must be True; got: {d}"
    )


async def test_a6_acc_central_down_allowed_from_last_copy(
    a6_ctrl, a6_gw_app, log_path,
):
    """ACCEPTANCE (S8): With the central source down, a legitimate call is ALLOWED
    from the last copy (approach6 keeps deciding, unlike approach5).
    """
    # Establish a valid snapshot
    bearer = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        r0 = await session.call_tool("submit_expense",
                                      {"claimant": "alice", "amount": 50.0, "description": "init"})
    assert r0.get("isError") is not True

    # Take central down
    await a6_ctrl.post("/control/directory-down", json={"down": True})

    # Legitimate call → ALLOWED from last copy (not CENTRAL_UNAVAILABLE)
    bearer2 = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer2}"},
    ) as client:
        session2 = _GwSession(client)
        await session2.initialize()
        result = await session2.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 50.0, "description": "during outage"},
        )

    assert result.get("isError") is not True, (
        "ACCEPTANCE S8: approach6 must allow from last copy when central is down; "
        f"got: {result}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "allow", (
        f"decision must be allow (not CENTRAL_UNAVAILABLE); got: {d}"
    )


async def test_a6_acc_central_down_revocation_takes_effect_after_recovery(
    a6_ctrl, a6_gw_app, log_path,
):
    """ACCEPTANCE (S8): A revocation made during a central source outage takes effect
    only after the source recovers — the event is queued and delivered on recovery.

    Steps:
    1. Establish snapshot, issue dan's token.
    2. Take central down.
    3. Revoke carol's delegation during outage (event would fire at T+1).
    4. Advance clock to T+5 (past event delivery time) — still down.
    5. Call with dan's token → ALLOWED (central down, event not delivered).
    6. Restore central.
    7. Call with same token → DENIED (event delivered on recovery).
    """
    bearer = await _make_bearer(a6_ctrl, "dan", "travel-assistant", lifetime=7200)

    # Step 1: trigger snapshot
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session0 = _GwSession(client)
        await session0.initialize()
        r0 = await session0.call_tool("book_travel",
                                       {"traveller": "carol", "details": "init"})
    assert r0.get("isError") is not True, f"precondition: booking allowed; got: {r0}"

    # Step 2: take central down
    await a6_ctrl.post("/control/directory-down", json={"down": True})

    # Step 3: revoke during outage → event at T0+1
    r = await a6_ctrl.post("/control/revoke-delegation", json={"delegation_id": "del-001"})
    r.raise_for_status()

    # Step 4: advance clock to T0+5 (past event delivery time T0+1)
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 5})

    # Step 5: call with same token → ALLOWED (central down, event deferred)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session1 = _GwSession(client)
        await session1.initialize()
        r1 = await session1.call_tool("book_travel",
                                       {"traveller": "carol", "details": "during outage T+5"})
    assert r1.get("isError") is not True, (
        "ACCEPTANCE S8: revocation during outage must NOT take effect while central is down; "
        f"got: {r1}"
    )

    # Step 6: restore central
    await a6_ctrl.post("/control/directory-down", json={"down": False})

    # Step 7: call again → event now delivered → DENIED
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session2 = _GwSession(client)
        await session2.initialize()
        r2 = await session2.call_tool("book_travel",
                                       {"traveller": "carol", "details": "after recovery"})
    assert r2.get("isError") is True, (
        "ACCEPTANCE S8: revocation must take effect after central recovers; "
        f"got: {r2}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P4", f"denial must be P4; got: {d['rule']!r}"


async def test_a6_acc_central_decide_counter_unchanged(
    a6_ctrl, a6_gw_app, log_path,
):
    """ACCEPTANCE: The /central/decide counter does not change during approach6 decisions."""
    r0 = await a6_ctrl.get("/control/central-decide-count")
    count_before = r0.json()["count"]

    bearer = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        # Multiple decisions
        await session.call_tool("submit_expense",
                                 {"claimant": "alice", "amount": 50.0, "description": "d1"})
        await session.call_tool("submit_expense",
                                 {"claimant": "alice", "amount": 100.0, "description": "d2"})
        await session.call_tool("read_receipt", {"receipt_id": "rcpt-001"})

    r1 = await a6_ctrl.get("/control/central-decide-count")
    count_after = r1.json()["count"]
    assert count_after == count_before, (
        "ACCEPTANCE: /central/decide counter must not change during approach6 decisions; "
        f"was {count_before}, now {count_after}"
    )


# ===========================================================================
# Required acceptance tests (exact names from spec E)
# ===========================================================================

async def test_a6_acc_limit_window(a6_ctrl, a6_gw_app, log_path):
    """ACCEPTANCE: a centrally-lowered expense limit is NOT applied until the clock
    passes the next publication time (S7), then IS applied (S10).

    Fails if removed: the publication-window guarantee would be unverified.
    """
    # Step 1: initial snapshot at EPOCH (limit=500)
    bearer0 = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer0}"},
    ) as client:
        s0 = _GwSession(client)
        await s0.initialize()
        r0 = await s0.call_tool("submit_expense",
                                {"claimant": "alice", "amount": 200.0, "description": "init"})
    assert r0.get("isError") is not True, f"S7 precondition: £200 < 500 allowed; got: {r0}"

    # Lower limit to 100 (central source, not yet published)
    await a6_ctrl.post("/control/set-limit", json={"limit": 100})

    # S7: decision BEFORE next publication — old limit=500 still in snapshot
    bearer1 = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer1}"},
    ) as client:
        s1 = _GwSession(client)
        await s1.initialize()
        r1 = await s1.call_tool("submit_expense",
                                {"claimant": "alice", "amount": 200.0, "description": "S7"})
    assert r1.get("isError") is not True, (
        "ACCEPTANCE S7: limit=100 not yet published; £200 must still be allowed; "
        f"got: {r1}"
    )

    # Advance clock past the next publication boundary (15 simulated minutes = 900 s)
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 900})

    # S10: decision AFTER publication — new limit=100 now in snapshot
    bearer2 = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer2}"},
    ) as client:
        s2 = _GwSession(client)
        await s2.initialize()
        r2 = await s2.call_tool("submit_expense",
                                {"claimant": "alice", "amount": 200.0, "description": "S10"})
    assert r2.get("isError") is True, (
        "ACCEPTANCE S10: limit=100 now published; £200 must be denied; "
        f"got: {r2}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P2", f"ACCEPTANCE S10: rule must be P2; got: {d['rule']!r}"


async def test_a6_acc_revocation_window(a6_ctrl, a6_gw_app, log_path):
    """ACCEPTANCE: a revoked delegation is still honoured less than 1 simulated second
    after revocation (event not yet delivered), and refused with the SAME token once
    the clock passes 1 second (S5 window).

    Fails if removed: the 1-second revocation-delivery guarantee would be unverified.
    """
    bearer = await _make_bearer(a6_ctrl, "dan", "travel-assistant", lifetime=7200)
    claims = _decode_unverified(bearer)
    assert any(d.get("delegator") == "carol" for d in claims.get("delegations_received", [])), \
        "Precondition: token must carry carol→dan delegation"

    # Prime snapshot
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        s0 = _GwSession(client)
        await s0.initialize()
        r0 = await s0.call_tool("book_travel", {"traveller": "carol", "details": "pre"})
    assert r0.get("isError") is not True, f"precondition: booking allowed; got: {r0}"

    # Revoke at T0 — event delivery at T0+1
    (await a6_ctrl.post("/control/revoke-delegation", json={"delegation_id": "del-001"})).raise_for_status()

    # At T0+0.5 (<1 s): event NOT yet delivered — ALLOWED
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 0.5})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        s1 = _GwSession(client)
        await s1.initialize()
        r1 = await s1.call_tool("book_travel", {"traveller": "carol", "details": "T0+0.5"})
    assert r1.get("isError") is not True, (
        "ACCEPTANCE S5: delegation must still be honoured at T0+0.5 (<1 s); "
        f"got: {r1}"
    )

    # At T0+1.0: event delivered — DENIED
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 0.5})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        s2 = _GwSession(client)
        await s2.initialize()
        r2 = await s2.call_tool("book_travel", {"traveller": "carol", "details": "T0+1.0"})
    assert r2.get("isError") is True, (
        "ACCEPTANCE S5: delegation must be refused at T0+1.0 (event delivered); "
        f"got: {r2}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P4", f"ACCEPTANCE S5: rule must be P4; got: {d['rule']!r}"
    assert d.get("revocation_applied") is True


async def test_a6_acc_central_down_last_copy_and_revocation(a6_ctrl, a6_gw_app, log_path):
    """ACCEPTANCE (S8): combined central-down test.

    Part A — with the central source down, a legitimate call is ALLOWED from the
    last copy (approach6 keeps deciding, unlike approach5).

    Part B — a revocation made during the outage takes effect only after the source
    recovers (the event is buffered and delivered on recovery).

    Fails if removed: the availability guarantee and buffered-revocation guarantee
    under central-source outages would both be unverified.
    """
    bearer = await _make_bearer(a6_ctrl, "dan", "travel-assistant", lifetime=7200)

    # Prime snapshot
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        s0 = _GwSession(client)
        await s0.initialize()
        r0 = await s0.call_tool("book_travel", {"traveller": "carol", "details": "init"})
    assert r0.get("isError") is not True, f"precondition: booking allowed; got: {r0}"

    # Take central DOWN
    await a6_ctrl.post("/control/directory-down", json={"down": True})

    # Part A: legitimate alice call must still be ALLOWED from last copy
    bearer_alice = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer_alice}"},
    ) as client:
        sa = _GwSession(client)
        await sa.initialize()
        ra = await sa.call_tool("submit_expense",
                               {"claimant": "alice", "amount": 50.0, "description": "central down"})
    assert ra.get("isError") is not True, (
        "ACCEPTANCE S8 part A: with central down, legitimate call must be ALLOWED "
        f"from last copy; got: {ra}"
    )
    da = _last_decision(log_path)
    assert da["decision"] == "allow"
    assert da.get("central_called") is False

    # Part B: revoke during outage → event at T0+1
    (await a6_ctrl.post("/control/revoke-delegation", json={"delegation_id": "del-001"})).raise_for_status()

    # Advance past delivery time (T0+5) — still DOWN
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 5})

    # Still allowed (central down, event not delivered)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        sb = _GwSession(client)
        await sb.initialize()
        rb = await sb.call_tool("book_travel",
                               {"traveller": "carol", "details": "during outage T+5"})
    assert rb.get("isError") is not True, (
        "ACCEPTANCE S8 part B: revocation during outage must NOT take effect while "
        f"central is down; got: {rb}"
    )

    # Restore central — event now delivered on recovery
    await a6_ctrl.post("/control/directory-down", json={"down": False})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        sc = _GwSession(client)
        await sc.initialize()
        rc = await sc.call_tool("book_travel",
                               {"traveller": "carol", "details": "after recovery"})
    assert rc.get("isError") is True, (
        "ACCEPTANCE S8 part B: revocation must take effect after central recovers; "
        f"got: {rc}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P4", f"ACCEPTANCE S8 part B: rule must be P4; got: {d['rule']!r}"


async def test_a6_acc_no_central_decide_calls(a6_ctrl, a6_gw_app, log_path):
    """ACCEPTANCE: the /central/decide counter does not change at all during
    approach6 decisions, regardless of the number or type of tool calls.

    Fails if removed: the no-per-decision-call guarantee would be unverified.
    """
    r0 = await a6_ctrl.get("/control/central-decide-count")
    count_before = r0.json()["count"]

    bearer = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        await session.call_tool("submit_expense",
                               {"claimant": "alice", "amount": 50.0, "description": "d1"})
        await session.call_tool("submit_expense",
                               {"claimant": "alice", "amount": 100.0, "description": "d2"})
        await session.call_tool("read_receipt", {"receipt_id": "rcpt-001"})
        # Denied call (P1) must also not increment the counter
        await session.call_tool("submit_expense",
                               {"claimant": "bob", "amount": 50.0, "description": "d3-denied"})

    r1 = await a6_ctrl.get("/control/central-decide-count")
    count_after = r1.json()["count"]
    assert count_after == count_before, (
        "ACCEPTANCE: /central/decide counter must not change during approach6; "
        f"was {count_before}, now {count_after}"
    )


# ===========================================================================
# Required spec-item tests (exact names from spec E)
# ===========================================================================

async def test_a6_i2_change_after_publication_not_in_current_copy(
    a6_ctrl, a6_gw_app, log_path,
):
    """Spec item 2: the first publication fires at EPOCH+900 capturing limit=500; the
    limit is then lowered to 100; a decision at EPOCH+1000 still uses the OLD limit
    (not yet published); after the second publication at EPOCH+1800 the new limit IS
    applied.

    Fails if removed: the guarantee that changes between publications are invisible
    to the gateway until the next publication would be unverified.
    """
    # Initial decision at EPOCH → initial snapshot (limit=500)
    bearer0 = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer0}"},
    ) as client:
        s0 = _GwSession(client)
        await s0.initialize()
        await s0.call_tool("submit_expense",
                           {"claimant": "alice", "amount": 50.0, "description": "prime"})

    # Clock → EPOCH+900: first regular publication fires (snapshot: limit=500)
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 900})

    # Clock → EPOCH+950: lower limit to 100 (between publications)
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 50})
    await a6_ctrl.post("/control/set-limit", json={"limit": 100})

    # Clock → EPOCH+1000: still between publications
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 50})

    # Decision at EPOCH+1000: snapshot from EPOCH+900 has limit=500 → £200 ALLOWED
    bearer1 = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer1}"},
    ) as client:
        s1 = _GwSession(client)
        await s1.initialize()
        r1 = await s1.call_tool("submit_expense",
                                {"claimant": "alice", "amount": 200.0,
                                 "description": "at EPOCH+1000"})
    assert r1.get("isError") is not True, (
        "At EPOCH+1000: new limit=100 not yet published; "
        f"£200 must be allowed (snapshot still has limit=500); got: {r1}"
    )

    # Clock → EPOCH+1800: second publication fires (snapshot: limit=100)
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 800})

    # Decision after EPOCH+1800: snapshot has limit=100 → £200 DENIED (P2)
    bearer2 = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer2}"},
    ) as client:
        s2 = _GwSession(client)
        await s2.initialize()
        r2 = await s2.call_tool("submit_expense",
                                {"claimant": "alice", "amount": 200.0,
                                 "description": "after EPOCH+1800"})
    assert r2.get("isError") is True, (
        "After EPOCH+1800: new limit=100 now published; "
        f"£200 must be denied (P2); got: {r2}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P2", f"rule must be P2; got: {d['rule']!r}"


async def test_a6_i2_publication_schedule_is_fixed_not_install_relative(
    a6_ctrl, a6_gw_app, log_path,
):
    """Spec item 2: versions advance on fixed boundaries at EPOCH+900*k from time 0,
    independent of install time, with the start copy as version 1.

    The plugin is reset (re-installed) at EPOCH+450: its start copy is version 1
    and the next boundary is still EPOCH+900, not EPOCH+1350.  Crossing EPOCH+900
    makes version 2; crossing EPOCH+1800 makes version 3.

    Fails if removed: the fixed-schedule guarantee would be unverified.
    """
    from arms.approach6 import plugin as _a6_plugin

    await a6_ctrl.post("/control/clock/advance", json={"seconds": 450})
    _a6_plugin.reset()

    assert _a6_plugin._snapshot_version == 1, "the start copy is version 1"
    assert _a6_plugin._next_pub_at == _SIM_EPOCH + 900.0, (
        "After reset at EPOCH+450 the schedule must remain anchored to the simulated "
        f"epoch: _next_pub_at must be {_SIM_EPOCH + 900.0}; "
        f"got {_a6_plugin._next_pub_at!r}"
    )

    await a6_ctrl.post("/control/clock/advance", json={"seconds": 451})   # EPOCH+901
    assert _a6_plugin._snapshot_version == 2, (
        f"the EPOCH+900 boundary must give version 2; got {_a6_plugin._snapshot_version}"
    )

    await a6_ctrl.post("/control/clock/advance", json={"seconds": 900})   # EPOCH+1801
    assert _a6_plugin._snapshot_version == 3, (
        f"the EPOCH+1800 boundary must give version 3; got {_a6_plugin._snapshot_version}"
    )
    from domain import central_publisher as _publisher
    assert _publisher._next_pub_at == _SIM_EPOCH + 2700.0

    bearer = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 50.0, "description": "fixed-schedule check"},
        )
    assert result.get("isError") is not True, f"Got: {result}"
    d = _last_decision(log_path)
    assert d.get("central_copy_version") == 3, (
        f"the decision must use version 3; got: {d.get('central_copy_version')!r}"
    )


async def test_a6_i5_multiple_publications_drained_in_one_advance(
    a6_ctrl, a6_gw_app, log_path,
):
    """Spec item 5: when the clock advances past multiple publication boundaries in
    one /control/clock/advance call, all pending publications are drained before the
    next decision is evaluated.

    Fails if removed: the guarantee that a single large clock advance drains all
    crossed publication boundaries would be unverified.
    """
    from arms.approach6 import plugin as _a6_plugin

    # Get initial snapshot at EPOCH
    bearer0 = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer0}"},
    ) as client:
        s0 = _GwSession(client)
        await s0.initialize()
        await s0.call_tool("submit_expense",
                           {"claimant": "alice", "amount": 50.0, "description": "prime"})
    version_after_init = _a6_plugin._snapshot_version
    assert version_after_init >= 1, "precondition: initial snapshot taken"

    # Lower limit to 100 so all three forthcoming publications capture the new value
    await a6_ctrl.post("/control/set-limit", json={"limit": 100})

    # ONE advance of 2700 s crosses three boundaries (EPOCH+900, EPOCH+1800, EPOCH+2700)
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 2700})

    # The decision must reflect all three publications
    bearer1 = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer1}"},
    ) as client:
        s1 = _GwSession(client)
        await s1.initialize()
        result = await s1.call_tool("submit_expense",
                                    {"claimant": "alice", "amount": 200.0,
                                     "description": "after 3 pubs"})

    # With limit=100 now published, £200 must be denied (P2)
    assert result.get("isError") is True, (
        "After 2700-s advance (3 publication boundaries), limit=100 must be in effect; "
        f"£200 must be denied (P2); got: {result}"
    )
    d = _last_decision(log_path)
    assert d["rule"] == "P2", f"rule must be P2; got: {d['rule']!r}"
    assert d.get("central_copy_version", 0) == version_after_init + 3, (
        f"Three publications must have fired (copy_version must be {version_after_init+3}); "
        f"got: {d.get('central_copy_version')!r}"
    )


async def test_a6_i3_state_level_revocation_produces_event_after_1s(
    a6_ctrl, a6_gw_app, log_path,
):
    """Spec item 3 / approved change C: State.revoke_delegation(id) marks the
    delegation inactive in live state AND emits an event whose delivery_time is
    exactly sim_time + 1.0 seconds.

    Also verifies the effect through the gateway: after the state-level revocation
    at T0, the SAME long-lived token is still allowed at T0+0.5 (event not yet
    delivered) and refused with P4 at T0+1.0 (event delivered).

    Fails if removed: the state-level revocation mechanism (approved change C) and
    its event-delivery-time guarantee would be unverified.
    """
    from arms.approach6 import plugin as _a6_plugin
    from domain import simclock
    from domain.state import state

    # domain/state.State must expose revoke_delegation per approved change C
    assert hasattr(state, "revoke_delegation"), (
        "State must have revoke_delegation(id) method (approved change C)"
    )

    # Issue dan's long-lived token while the delegation is still active
    bearer = await _make_bearer(a6_ctrl, "dan", "travel-assistant", lifetime=7200)
    claims = _decode_unverified(bearer)
    assert any(d.get("delegator") == "carol" for d in claims.get("delegations_received", [])), \
        "Precondition: token must carry carol→dan travel delegation"

    # Prime snapshot (approach6 needs a snapshot before it can make decisions)
    bearer_prime = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer_prime}"},
    ) as client:
        s_prime = _GwSession(client)
        await s_prime.initialize()
        await s_prime.call_tool("submit_expense",
                               {"claimant": "alice", "amount": 50.0, "description": "prime"})

    # Booking allowed before revocation (sanity check)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        s_pre = _GwSession(client)
        await s_pre.initialize()
        r_pre = await s_pre.call_tool("book_travel",
                                     {"traveller": "carol", "details": "pre-revoke"})
    assert r_pre.get("isError") is not True, (
        f"Precondition: booking allowed before revocation; got: {r_pre}"
    )

    # Revoke via state directly (no HTTP route) at T0
    sim_before = simclock.now()
    state.revoke_delegation("del-001")

    # --- existing assertions ---

    # Delegation must be inactive in live state
    del_entry = next((d for d in state.delegations if d["id"] == "del-001"), None)
    assert del_entry is not None
    assert del_entry.get("active") is False, (
        "revoke_delegation must mark the delegation inactive in state"
    )

    # Plugin must have received exactly one pending event
    assert len(_a6_plugin._pending_events) == 1, (
        f"revoke_delegation must emit exactly one event; "
        f"got {len(_a6_plugin._pending_events)}: {_a6_plugin._pending_events}"
    )
    event = _a6_plugin._pending_events[0]
    assert event.get("delegator") == "carol", (
        f"event must identify delegator='carol'; got: {event}"
    )
    assert event.get("delegate") == "dan", (
        f"event must identify delegate='dan'; got: {event}"
    )
    expected = sim_before + 1.0
    assert abs(event["delivery_time"] - expected) < 0.001, (
        f"delivery_time must be sim_time+1.0 = {expected}; "
        f"got {event['delivery_time']}"
    )

    # --- gateway-level assertions: same token, 1-second delivery window ---

    # At T0+0.5 (< 1 s): event NOT yet delivered → booking ALLOWED
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 0.5})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        s_half = _GwSession(client)
        await s_half.initialize()
        r_half = await s_half.call_tool("book_travel",
                                       {"traveller": "carol", "details": "T0+0.5"})
    assert r_half.get("isError") is not True, (
        "SAME token must still allow booking at T0+0.5 (event delivery is T0+1.0); "
        f"got: {r_half}"
    )

    # At T0+1.0: event delivered → booking DENIED with P4
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 0.5})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        s_full = _GwSession(client)
        await s_full.initialize()
        r_full = await s_full.call_tool("book_travel",
                                       {"traveller": "carol", "details": "T0+1.0"})
    assert r_full.get("isError") is True, (
        "SAME token must be refused at T0+1.0 (event delivered, revocation applied); "
        f"got: {r_full}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P4", f"denial must be P4; got: {d['rule']!r}"
    assert d.get("revocation_applied") is True, (
        f"revocation_applied must be True; got: {d}"
    )


async def test_a6_i3_role_from_token_not_live_state(a6_ctrl, a6_gw_app, log_path):
    """Spec item 3: approach6 reads the principal's role from the token claims, not
    from live state.  Even if alice's live-state role is changed to 'finance' after
    token issuance, the P7 deny is based on the token's 'employee' role.

    Fails if removed: the guarantee that people-facts come from the token (not live
    state) would be unverified for role-based rules.
    """
    from domain.state import state

    # Issue a token while alice has role='employee' (from state)
    bearer = await _make_bearer(a6_ctrl, "alice", "payments-agent")
    claims = _decode_unverified(bearer)
    assert claims.get("role") == "employee", (
        f"Precondition: token must carry role='employee'; got {claims.get('role')!r}"
    )

    # Promote alice in live state (after token issuance)
    state.users["alice"]["role"] = "finance"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "pay_vendor",
            {"vendor": "acme-hotels", "amount": 50.0, "reference": "ref-role"},
        )

    # approach6 must use token role (employee) → P7 deny
    # approach5 (live state) would now see 'finance' → allow
    assert result.get("isError") is True, (
        "approach6 must use token role 'employee' and deny via P7 even if "
        "live state now says 'finance'; "
        f"got: {result}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P7", (
        f"denial must be P7 (from token role 'employee'); got: {d['rule']!r}"
    )


async def test_a6_i6_snapshot_taken_on_schedule_during_outage_latest_delivered_on_recovery(
    a6_ctrl, a6_gw_app, log_path,
):
    """Spec item 6 / approved change B: while the central source is down, snapshots
    are still taken at their scheduled boundaries and buffered internally.  On
    recovery, only the LATEST buffered snapshot is delivered (latest wins).

    This is verified by: making the state at EPOCH+2700 (limit=50) different from
    the state at recovery time (limit=10).  The correct implementation delivers
    limit=50; the wrong one would use the recovery-time state (limit=10).

    Fails if removed: the buffered-publication guarantee and 'latest wins' semantics
    under central-source outages would be unverified.
    """
    from arms.approach6 import plugin as _a6_plugin

    # --- Step 1: establish initial snapshot at EPOCH ---
    bearer = await _make_bearer(a6_ctrl, "alice", "expense-assistant", lifetime=7200)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        s0 = _GwSession(client)
        await s0.initialize()
        r0 = await s0.call_tool("submit_expense",
                               {"claimant": "alice", "amount": 50.0, "description": "init"})
    assert r0.get("isError") is not True, f"precondition: init allowed; got: {r0}"

    # --- Step 2: EPOCH+900 — first regular publication (limit=500, central UP) ---
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 900})

    # --- Step 3: take central DOWN ---
    await a6_ctrl.post("/control/directory-down", json={"down": True})

    # --- Step 4: lower limit to 200 (during outage, before EPOCH+1800 boundary) ---
    await a6_ctrl.post("/control/set-limit", json={"limit": 200})

    # --- Step 5: EPOCH+1800 — second pub boundary crossed during outage ---
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 900})

    # --- Step 6: lower limit to 50 (during outage, before EPOCH+2700 boundary) ---
    await a6_ctrl.post("/control/set-limit", json={"limit": 50})

    # --- Step 7: EPOCH+2700 — third pub boundary crossed during outage ---
    await a6_ctrl.post("/control/clock/advance", json={"seconds": 900})

    # Snapshot must NOT have been updated yet (central is still down)
    snap_during_outage = _a6_plugin._snapshot
    assert snap_during_outage is not None
    assert snap_during_outage["expense_limit"] == 500, (
        "During outage, snapshot must NOT be updated; expected limit=500 (last copy "
        f"from before outage); got: {snap_during_outage['expense_limit']}"
    )

    # --- Step 8: change limit AGAIN after last pub boundary (before recovery) ---
    # This distinguishes "snapshot at EPOCH+2700" (limit=50) from "snapshot at recovery" (limit=10)
    await a6_ctrl.post("/control/set-limit", json={"limit": 10})

    # --- Step 9: restore central ---
    await a6_ctrl.post("/control/directory-down", json={"down": False})

    # --- Step 10: decision — must use the EPOCH+2700 snapshot (limit=50, not 10) ---
    # £30 < limit=50 → ALLOWED (correct: latest buffered snapshot from EPOCH+2700)
    # £30 > limit=10 → DENIED  (wrong: recovery-time snapshot)
    bearer2 = await _make_bearer(a6_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer2}"},
    ) as client:
        s2 = _GwSession(client)
        await s2.initialize()
        result = await s2.call_tool("submit_expense",
                                   {"claimant": "alice", "amount": 30.0,
                                    "description": "post-recovery"})

    assert result.get("isError") is not True, (
        "After recovery, latest buffered snapshot (from EPOCH+2700, limit=50) must apply; "
        "£30 < limit=50 must be ALLOWED. "
        "Wrong implementation would use recovery-time state (limit=10) and deny. "
        f"Got: {result}"
    )
    assert _a6_plugin._snapshot["expense_limit"] == 50, (
        "After recovery, plugin snapshot must reflect the EPOCH+2700 publication "
        f"(limit=50, not recovery-time limit=10); "
        f"got: {_a6_plugin._snapshot['expense_limit']}"
    )


async def test_a6_guard_plugin_reads_no_live_state_for_people_facts(
    a6_ctrl, a6_gw_app, log_path,
):
    """Guard: during every approach6 policy evaluation, the plugin must NOT access
    state.users, state.delegations, state.vendors, or state.expense_limit.  All
    people-facts come from token claims; central-policy data comes from the snapshot.

    A _StateProxy wraps the state singleton and raises AssertionError on any access
    to those four attributes.  The proxy is active only during each evaluate() call.

    Tests deny cases (P1, P2, P3, P4, P5, P7) and four allow cases:
      • alice's own small expense
      • dan booking for carol with the delegation in his token
      • erin paying an approved vendor
      • bob approving alice's expense

    Fails if removed: someone could add live-state access for people-facts without
    any test catching it.
    """
    import domain.state as _state_mod
    from arms.approach6 import plugin as _a6_plugin
    from domain import simclock
    from domain.simclock import _EPOCH as _SIM_EPOCH

    # --- _StateProxy: raises on the four forbidden attributes ---
    _BLOCKED = frozenset({"users", "delegations", "vendors", "expense_limit"})

    class _StateProxy:
        def __init__(self, real):
            object.__setattr__(self, "_real", real)

        def __getattr__(self, name):
            if name in _BLOCKED:
                raise AssertionError(
                    f"approach6 plugin must not read state.{name} during decisions"
                )
            return getattr(object.__getattribute__(self, "_real"), name)

        def __setattr__(self, name, value):
            setattr(object.__getattribute__(self, "_real"), name, value)

    # --- prime the snapshot ---
    # Set the snapshot directly to avoid triggering the slow initial-snapshot loop.
    # This isolates the guard from publication mechanics, testing only the decision path.
    _a6_plugin._snapshot = {
        "expense_limit": 500,
        "approved_vendors": ["acme-hotels", "fastair", "reliable-cabs"],
        "version": 1,
    }
    _a6_plugin._snapshot_version = 1
    # Place the next publication boundary far in the future so _process_pending()
    # does not try to take a new snapshot during the guarded calls below.
    _a6_plugin._next_pub_at = _SIM_EPOCH + 900_000_000

    # --- guarded call: proxy is active only during evaluate() ---
    def guarded(claims, agent_chain, tool, args):
        real = _state_mod.state
        _state_mod.state = _StateProxy(real)
        try:
            return _a6_plugin.evaluate(claims, agent_chain, tool, args)
        finally:
            _state_mod.state = real

    # Shared claim skeletons (token-level, as the gateway would pass them)
    _alice = {"sub": "alice", "role": "employee",
               "delegations_received": [], "reports_to": []}
    _dan_no_deleg = {"sub": "dan", "role": "assistant",
                     "delegations_received": [], "reports_to": []}
    _dan_with_deleg = {
        "sub": "dan", "role": "assistant", "reports_to": [],
        "delegations_received": [{
            "delegator": "carol", "scope": ["travel"],
            "expires": "2027-12-31T23:59:59Z",
        }],
    }
    _erin = {"sub": "erin", "role": "finance",
              "delegations_received": [], "reports_to": []}
    _bob = {"sub": "bob", "role": "manager",
             "delegations_received": [], "reports_to": ["alice"]}

    # --- deny cases ---

    # P1: alice submitting for bob (wrong claimant)
    r = guarded(_alice, ["expense-assistant"], "submit_expense",
                {"claimant": "bob", "amount": 50.0, "description": "P1"})
    assert r["decision"] == "deny" and r["rule"] == "P1", (
        f"P1 deny: expected deny/P1; got {r}"
    )

    # P2: alice submitting expense above snapshot limit (£600 > £500)
    r = guarded(_alice, ["expense-assistant"], "submit_expense",
                {"claimant": "alice", "amount": 600.0, "description": "P2"})
    assert r["decision"] == "deny" and r["rule"] == "P2", (
        f"P2 deny: expected deny/P2; got {r}"
    )

    # P3: alice approving her own expense (exp-001 claimant = alice)
    r = guarded(_alice, ["expense-assistant"], "approve_expense",
                {"expense_id": "exp-001"})
    assert r["decision"] == "deny" and r["rule"] == "P3", (
        f"P3 deny: expected deny/P3; got {r}"
    )

    # P4: dan booking for carol WITHOUT delegation in token
    r = guarded(_dan_no_deleg, ["travel-assistant"], "book_travel",
                {"traveller": "carol", "details": "P4 test"})
    assert r["decision"] == "deny" and r["rule"] == "P4", (
        f"P4 deny: expected deny/P4; got {r}"
    )

    # P5: erin paying a non-approved vendor
    r = guarded(_erin, ["payments-agent"], "pay_vendor",
                {"vendor": "rogue-vendor", "amount": 50.0, "reference": "P5"})
    assert r["decision"] == "deny" and r["rule"] == "P5", (
        f"P5 deny: expected deny/P5; got {r}"
    )

    # P7: alice via payments-agent (role=employee, not finance)
    r = guarded(_alice, ["payments-agent"], "pay_vendor",
                {"vendor": "acme-hotels", "amount": 50.0, "reference": "P7"})
    assert r["decision"] == "deny" and r["rule"] == "P7", (
        f"P7 deny: expected deny/P7; got {r}"
    )

    # --- allow cases ---

    # 1. alice's own small expense (£50 < limit=500, claimant=self)
    r = guarded(_alice, ["expense-assistant"], "submit_expense",
                {"claimant": "alice", "amount": 50.0, "description": "own expense"})
    assert r["decision"] == "allow", (
        f"Allow: alice's own small expense; got {r}"
    )

    # 2. dan booking for carol WITH delegation in token
    r = guarded(_dan_with_deleg, ["travel-assistant"], "book_travel",
                {"traveller": "carol", "details": "Rome, 2027-05-01"})
    assert r["decision"] == "allow", (
        f"Allow: dan books for carol via token delegation; got {r}"
    )

    # 3. erin paying an approved vendor
    r = guarded(_erin, ["payments-agent"], "pay_vendor",
                {"vendor": "acme-hotels", "amount": 100.0, "reference": "erin-pay"})
    assert r["decision"] == "allow", (
        f"Allow: erin (finance) pays approved vendor; got {r}"
    )

    # 4. bob approving alice's expense (bob is alice's manager, reports_to=["alice"])
    r = guarded(_bob, ["expense-assistant"], "approve_expense",
                {"expense_id": "exp-001"})
    assert r["decision"] == "allow", (
        f"Allow: bob approves alice's expense (manager); got {r}"
    )


# ===========================================================================
# Approved change B2 — snapshot-taking belongs in domain/central_publisher.py
# ===========================================================================

def test_a6_b2_central_publisher_module_exists():
    """Approved change B2: domain/central_publisher.py must exist and be the module
    that the clock control routes (or the clock itself) call when a publication
    boundary is crossed — i.e. it must expose process_clock_advance and on_recovery,
    and the clock advance route in server.py must call central_publisher, not the
    plugin directly.

    Fails right now because domain/central_publisher.py does not yet exist.
    """
    import importlib
    import importlib.util

    _PUBLISHER_PATH = _REPO / "domain" / "central_publisher.py"
    assert _PUBLISHER_PATH.exists(), (
        "domain/central_publisher.py must exist (approved change B2); "
        f"not found at {_PUBLISHER_PATH}"
    )

    # Must be importable
    spec = importlib.util.spec_from_file_location("domain.central_publisher", _PUBLISHER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert hasattr(mod, "process_clock_advance"), (
        "domain/central_publisher.py must expose process_clock_advance(now)"
    )
    assert hasattr(mod, "on_recovery"), (
        "domain/central_publisher.py must expose on_recovery(now)"
    )

    # The server's clock-advance route must invoke central_publisher.process_clock_advance,
    # not the plugin directly.
    server_src = (_REPO / "domain" / "server.py").read_text()
    assert "central_publisher" in server_src, (
        "domain/server.py must import/call central_publisher when advancing the clock "
        "(approved change B2); the snapshot-taking must no longer be driven by the plugin"
    )


def test_a6_b2_plugin_source_has_no_live_state_reads():
    """Approved change B2: arms/approach6/plugin.py must contain no reference to
    state.users, state.delegations, state.vendors, or state.expense_limit anywhere
    in the file — not only during decisions, but at module level too.

    The snapshot-taking (which reads those attributes) must have been moved to
    domain/central_publisher.py; the plugin only receives delivered snapshots.

    Fails right now because plugin.py still contains _capture_snapshot() which reads
    state.expense_limit and state.vendors.
    """
    plugin_src = (_REPO / "arms" / "approach6" / "plugin.py").read_text()

    forbidden = [
        "state.users",
        "state.delegations",
        "state.vendors",
        "state.expense_limit",
    ]
    violations = [f for f in forbidden if f in plugin_src]
    assert not violations, (
        "arms/approach6/plugin.py must not reference live state for people-facts or "
        "central-policy data (approved change B2). "
        f"Found forbidden references: {violations}"
    )
