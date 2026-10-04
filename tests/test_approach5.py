"""Tests for approach 5 (task 2c).

Every test maps to a numbered spec item in CLAUDE.md.  All tests must FAIL
until the implementation is added; none may be skipped or weakened.

Naming convention: test_a5_<spec_item>_<description>
"""
import json
import pathlib
import re
import time

import httpx
import pytest
import pytest_asyncio

from domain import simclock, tokens
from domain.grants import AGENT_GRANTS as GATEWAY_GRANTS

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

_REPO              = pathlib.Path(__file__).parent.parent
_A5_POLICIES_DIR   = _REPO / "arms" / "shared" / "policies"
_CENTRAL_POLICY    = _REPO / "domain" / "central_policy.cedar"
_SHARED_FP         = _REPO / "domain" / "fingerprints.json"
_OLD_A4_FP         = _REPO / "arms" / "approach4" / "fingerprints.json"
_USE_CASES         = ["expenses", "travel", "payments"]

TOOL_SCOPES: dict[str, str] = {
    "read_receipt":   "expenses:read",
    "submit_expense": "expenses:submit",
    "approve_expense":"expenses:approve",
    "book_travel":    "travel:book",
    "pay_vendor":     "payments:pay",
}

# ---------------------------------------------------------------------------
# Helpers — token issuing (mirrors test_approach4.py)
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
                       scopes=None, lifetime: int = 300) -> str:
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


# Compute fingerprints using the already-existing gateway helper so the
# fixture works even before approach5 is written.
def _fingerprints_for_a5() -> dict[str, str]:
    from domain.gateway import _compute_fingerprint
    from domain.server import mcp
    return {
        name: _compute_fingerprint(tool.name, tool.description, tool.parameters)
        for name, tool in mcp._tool_manager._tools.items()
    }


# ---------------------------------------------------------------------------
# Helpers — gateway session (mirrors test_approach4.py _GwSession)
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
                                          "clientInfo": {"name": "a5-test", "version": "0"}}})
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
# Fixtures — approach5 gateway
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def a5_gw_app(running_app, log_path):
    """Install the gateway with the approach5 plugin and yield the ASGI app.

    The plugin must call /central/decide over HTTP (not a direct function call).
    To avoid a real TCP socket in tests and to avoid deadlocking the event loop,
    we inject httpx.ASGITransport(app=running_app) into the plugin module.
    httpx's ASGITransport invokes the app as a coroutine — it suspends the
    calling coroutine and lets the event loop dispatch the /central/decide
    handler, so there is no blocking and no deadlock.
    """
    from arms.approach5 import plugin as _a5_plugin
    from domain.gateway import install_gateway, remove_gateway

    # Wire the plugin to call /central/decide through the in-process ASGI app.
    _a5_plugin._central_transport = httpx.ASGITransport(app=running_app)

    install_gateway(
        grants=GATEWAY_GRANTS,
        fingerprints=_fingerprints_for_a5(),
        scope_map=TOOL_SCOPES,
        policy_plugin=_a5_plugin.evaluate,
    )
    yield running_app
    remove_gateway()
    _a5_plugin._central_transport = None


@pytest_asyncio.fixture
async def a5_ctrl(a5_gw_app) -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as client:
        yield client


async def _a5_session(app, bearer: str) -> _GwSession:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    )
    session = _GwSession(client)
    await session.initialize()
    return session


# ===========================================================================
# Spec item 1 — Shared gateway data (fingerprints in domain/)
# ===========================================================================

def test_a5_i1_shared_fingerprints_file_exists():
    """domain/fingerprints.json exists in a shared location (not inside arms/approach4)."""
    assert _SHARED_FP.exists(), (
        f"domain/fingerprints.json must exist at {_SHARED_FP}; "
        "it is the shared reviewed-fingerprint record for approaches 4, 5 and 6"
    )


def test_a5_i1_shared_fingerprints_covers_all_five_tools():
    """domain/fingerprints.json contains an entry for every one of the five registered tools."""
    assert _SHARED_FP.exists(), "prerequisite: domain/fingerprints.json must exist"
    data = json.loads(_SHARED_FP.read_text(encoding="utf-8"))
    expected_tools = {"read_receipt", "submit_expense", "approve_expense",
                      "book_travel", "pay_vendor"}
    missing = expected_tools - set(data.keys())
    assert not missing, (
        f"domain/fingerprints.json is missing entries for: {missing}"
    )


def test_a5_i1_approach4_fingerprints_json_does_not_exist():
    """arms/approach4/fingerprints.json has been MOVED to domain/fingerprints.json.
    The old path must not exist — it is not a copy."""
    assert not _OLD_A4_FP.exists(), (
        f"arms/approach4/fingerprints.json must be deleted after moving to "
        f"domain/fingerprints.json; the file still exists at {_OLD_A4_FP}"
    )


def test_a5_i1_no_python_source_references_old_fingerprints_path():
    """No Python source file in the repo references arms/approach4/fingerprints.json.
    This covers both code and scripts (e.g. write_fingerprints.py must be updated)."""
    needle = "arms/approach4/fingerprints.json"
    offenders: list[str] = []
    for py_file in _REPO.rglob("*.py"):
        # Skip venv and cache directories
        parts = py_file.relative_to(_REPO).parts
        if any(p in (".venv", "__pycache__", ".pytest_cache") for p in parts):
            continue
        # Exclude this file: its needle variable contains the string by necessity
        if py_file == _REPO / "tests" / "test_approach5.py":
            continue
        if needle in py_file.read_text(encoding="utf-8"):
            offenders.append(str(py_file.relative_to(_REPO)))
    assert not offenders, (
        f"The following files still reference {needle!r} and must be updated: "
        f"{offenders}"
    )


def test_a5_i1_gateway_server_startup_loads_from_domain_fingerprints():
    """domain/server.py's gateway startup block loads fingerprints from
    domain/fingerprints.json, not from the old arms/approach4 path and not
    by computing them inline."""
    srv_src = (_REPO / "domain" / "server.py").read_text(encoding="utf-8")
    # Must reference the shared file
    assert "domain/fingerprints.json" in srv_src or "domain.fingerprints" in srv_src, (
        "domain/server.py must load fingerprints from domain/fingerprints.json "
        "(or a domain.fingerprints module) in the gateway startup block"
    )
    # Must NOT reference the old approach4 path
    assert "arms/approach4/fingerprints" not in srv_src, (
        "domain/server.py must not reference arms/approach4/fingerprints "
        "after the move"
    )


def test_a5_i1_domain_fingerprints_values_match_current_tool_definitions():
    """The SHA-256 values in domain/fingerprints.json match the fingerprints
    computed from the current tool definitions.  The file must be kept in sync
    when tools are updated."""
    assert _SHARED_FP.exists(), "prerequisite: domain/fingerprints.json must exist"
    from domain.gateway import _compute_fingerprint
    from domain.server import mcp

    stored = json.loads(_SHARED_FP.read_text(encoding="utf-8"))
    mismatches: list[str] = []
    for name, tool in mcp._tool_manager._tools.items():
        expected = _compute_fingerprint(tool.name, tool.description, tool.parameters)
        actual = stored.get(name)
        if actual != expected:
            mismatches.append(
                f"{name}: stored={actual!r}, computed={expected!r}"
            )
    assert not mismatches, (
        "domain/fingerprints.json is out of sync with current tool definitions:\n"
        + "\n".join(mismatches)
    )


# ===========================================================================
# Spec item 2 — Central policy file (P1–P5 in domain/)
# ===========================================================================

def test_a5_i2_central_policy_file_exists():
    """domain/central_policy.cedar exists at a shared path, not under any arm directory."""
    assert _CENTRAL_POLICY.exists(), (
        f"domain/central_policy.cedar must exist at {_CENTRAL_POLICY}"
    )


def _extract_rule_ids(text: str) -> set[str]:
    return set(re.findall(r'@id\("([^"]+)"\)', text))


def test_a5_i2_central_policy_contains_p1_through_p5():
    """Central policy has @id annotations for P1, P2, P3, P4, and P5."""
    assert _CENTRAL_POLICY.exists(), "prerequisite: domain/central_policy.cedar must exist"
    text = _CENTRAL_POLICY.read_text(encoding="utf-8")
    ids  = _extract_rule_ids(text)
    missing = {"P1", "P2", "P3", "P4", "P5"} - ids
    assert not missing, (
        f"central policy is missing rule IDs: {missing}. Found: {ids}"
    )


def test_a5_i2_central_policy_has_no_p7():
    """P7 (finance-only, payments) is NOT in the central policy; it lives in the use-case file."""
    assert _CENTRAL_POLICY.exists(), "prerequisite: domain/central_policy.cedar must exist"
    text = _CENTRAL_POLICY.read_text(encoding="utf-8")
    ids  = _extract_rule_ids(text)
    assert "P7" not in ids, (
        "P7 must not appear in the central policy; it belongs in the payments use-case file"
    )


def test_a5_i2_central_policy_not_under_approach4():
    """The central policy file path is under domain/, not under arms/approach4/."""
    parts = _CENTRAL_POLICY.parts
    assert "approach4" not in parts, (
        f"Central policy must not be under arms/approach4; found: {_CENTRAL_POLICY}"
    )


# ===========================================================================
# Spec item 3 — Central decision service (live facts, own route)
# ===========================================================================

async def test_a5_i3_central_service_route_allows_valid_call(a5_ctrl):
    """POST /central/decide returns a well-formed JSON decision for a valid request."""
    r = await a5_ctrl.post("/central/decide", json={
        "user": "alice",
        "tool": "submit_expense",
        "arguments": {"claimant": "alice", "amount": 100.0},
    })
    assert r.status_code == 200, (
        f"/central/decide must return 200 for a valid request; got {r.status_code}: {r.text}"
    )
    body = r.json()
    assert "decision" in body, f"/central/decide response must have 'decision' field: {body}"
    assert body["decision"] in ("allow", "deny"), (
        f"decision must be 'allow' or 'deny', got: {body['decision']}"
    )


async def test_a5_i3_central_service_uses_live_delegation(a5_ctrl):
    """After revoking carol→dan delegation, /central/decide denies dan booking for carol
    immediately — no token needed, it reads live state."""
    r_before = await a5_ctrl.post("/central/decide", json={
        "user": "dan",
        "tool": "book_travel",
        "arguments": {"traveller": "carol", "details": "Paris, 2027-03-01"},
    })
    assert r_before.status_code == 200
    assert r_before.json()["decision"] == "allow", (
        "Precondition: dan booking for carol should be allowed before revocation"
    )

    await a5_ctrl.post("/control/revoke-delegation",
                       json={"delegation_id": "del-001"})

    r_after = await a5_ctrl.post("/central/decide", json={
        "user": "dan",
        "tool": "book_travel",
        "arguments": {"traveller": "carol", "details": "Paris, 2027-03-01"},
    })
    assert r_after.status_code == 200
    body = r_after.json()
    assert body["decision"] == "deny", (
        "After revoking delegation, /central/decide must deny dan booking for carol "
        f"immediately; got: {body}"
    )
    assert body.get("rule") == "P4", (
        f"Rule must be P4 (delegation check), got: {body.get('rule')}"
    )


async def test_a5_i3_central_service_uses_live_vendors(a5_ctrl):
    """Central service denies an unapproved vendor (reads live vendor list)."""
    r = await a5_ctrl.post("/central/decide", json={
        "user": "erin",
        "tool": "pay_vendor",
        "arguments": {"vendor": "rogue-vendor", "amount": 100.0},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "deny", (
        f"/central/decide must deny rogue-vendor (not on approved list); got: {body}"
    )
    assert body.get("rule") == "P5", (
        f"Rule must be P5, got: {body.get('rule')}"
    )


async def test_a5_i3_central_service_uses_live_expense_limit(a5_ctrl):
    """After lowering the live expense limit to £100, a £200 expense without
    approval_ref is denied by the central service."""
    await a5_ctrl.post("/control/set-limit", json={"limit": 100})

    r = await a5_ctrl.post("/central/decide", json={
        "user": "alice",
        "tool": "submit_expense",
        "arguments": {"claimant": "alice", "amount": 200.0},
    })
    assert r.status_code == 200
    body = r.json()
    assert body["decision"] == "deny", (
        "With live limit=£100, a £200 expense without approval_ref must be denied; "
        f"got: {body}"
    )
    assert body.get("rule") == "P2", f"Rule must be P2, got: {body.get('rule')}"


# --- Central service location (shared module, not under arms/approach5/) ---

def test_a5_i3_central_service_module_in_domain_exists():
    """domain/central_service.py exists.  It is shared by approaches 5 and 6
    (and any future non-agent channel) and must live next to the central policy,
    not inside any arm directory."""
    p = _REPO / "domain" / "central_service.py"
    assert p.exists(), (
        f"domain/central_service.py must exist at {p}; "
        "the central evaluator is a shared module, not approach5-specific"
    )


def test_a5_i3_no_central_service_file_under_approach5():
    """No file named central_service.py exists anywhere under arms/approach5/.
    The central evaluator must not be duplicated or shadowed inside the arm."""
    duplicates = list((_REPO / "arms" / "approach5").rglob("central_service.py"))
    assert not duplicates, (
        f"central_service.py must not exist under arms/approach5/; "
        f"found: {[str(p.relative_to(_REPO)) for p in duplicates]}"
    )


def test_a5_i3_approach5_plugin_does_not_import_central_service():
    """arms/approach5/plugin.py must NOT import or reference domain.central_service.
    The plugin reaches the central service exclusively through POST /central/decide;
    it has no knowledge of the module behind the route.  The route-counter test
    (test_a5_i5_plugin_calls_central_via_http_route_one_call_per_decision) is the
    proof that the HTTP route is used."""
    plugin_path = _REPO / "arms" / "approach5" / "plugin.py"
    assert plugin_path.exists(), f"prerequisite: {plugin_path} must exist"
    src = plugin_path.read_text(encoding="utf-8")
    for forbidden in ("domain.central_service", "domain/central_service",
                      "from domain import central_service",
                      "import central_service"):
        assert forbidden not in src, (
            f"arms/approach5/plugin.py must not reference {forbidden!r}; "
            "the plugin must only know the HTTP route /central/decide, "
            "not the module that implements it"
        )


def test_a5_i3_no_approach5_file_reads_central_policy_cedar():
    """No Python file under arms/approach5/ reads or loads central_policy.cedar.
    The central Cedar policy is evaluated exclusively by domain/central_service.py."""
    offenders: list[str] = []
    a5_dir = _REPO / "arms" / "approach5"
    if a5_dir.exists():
        for py_file in a5_dir.rglob("*.py"):
            src = py_file.read_text(encoding="utf-8")
            if "central_policy.cedar" in src or "central_policy" in src:
                offenders.append(str(py_file.relative_to(_REPO)))
    assert not offenders, (
        f"The following approach5 files reference central_policy.cedar, "
        f"which must only be loaded by domain/central_service.py: {offenders}"
    )


# ===========================================================================
# Spec item 4 — Use-case policy files (ONLY use-case rules)
# ===========================================================================

def test_a5_i4_expenses_policy_file_exists():
    """arms/approach5/policies/expenses.cedar exists."""
    p = _A5_POLICIES_DIR / "expenses.cedar"
    assert p.exists(), f"expected expenses.cedar at {p}"


def test_a5_i4_travel_policy_file_exists():
    """arms/approach5/policies/travel.cedar exists."""
    p = _A5_POLICIES_DIR / "travel.cedar"
    assert p.exists(), f"expected travel.cedar at {p}"


def test_a5_i4_payments_policy_file_exists():
    """arms/approach5/policies/payments.cedar exists."""
    p = _A5_POLICIES_DIR / "payments.cedar"
    assert p.exists(), f"expected payments.cedar at {p}"


def test_a5_i4_use_case_files_contain_no_p1_through_p5():
    """None of the approach5 use-case Cedar files contains @id P1, P2, P3, P4, or P5.
    Those rules live exclusively in the central policy."""
    forbidden = {"P1", "P2", "P3", "P4", "P5"}
    for name in _USE_CASES:
        path = _A5_POLICIES_DIR / f"{name}.cedar"
        assert path.exists(), f"prerequisite: {path} must exist"
        ids = _extract_rule_ids(path.read_text(encoding="utf-8"))
        overlap = ids & forbidden
        assert not overlap, (
            f"{name}.cedar must not contain central rules {overlap}; "
            "P1–P5 must only appear in domain/central_policy.cedar"
        )


def test_a5_i4_payments_use_case_has_p7():
    """arms/approach5/policies/payments.cedar has @id("P7") — the finance-only forbid."""
    path = _A5_POLICIES_DIR / "payments.cedar"
    assert path.exists(), f"prerequisite: {path} must exist"
    ids = _extract_rule_ids(path.read_text(encoding="utf-8"))
    assert "P7" in ids, (
        f"payments.cedar must contain @id('P7') (finance-only forbid); found: {ids}"
    )


def test_a5_i4_use_case_files_no_include_or_import():
    """None of the approach5 use-case files uses an include or import directive."""
    for name in _USE_CASES:
        path = _A5_POLICIES_DIR / f"{name}.cedar"
        assert path.exists(), f"prerequisite: {path} must exist"
        text = path.read_text(encoding="utf-8")
        for keyword in ("include", "import", "use"):
            if re.search(rf'^\s*{keyword}\s+["\']', text, re.MULTILINE | re.IGNORECASE):
                pytest.fail(
                    f"{name}.cedar must not use '{keyword}' — each file is self-contained"
                )


# ===========================================================================
# Spec item 5 — Plugin (uses central service; ignores token person claims)
# ===========================================================================

async def test_a5_i5_plugin_calls_central_service_on_every_decision(
    a5_ctrl, a5_gw_app, log_path,
):
    """Every decision that reaches the approach5 plugin has central_called=True in the log."""
    bearer = await _make_bearer(a5_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        # Legitimate call — reaches the plugin
        await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 100.0, "description": "test"},
        )

    d = _last_decision(log_path)
    assert "central_called" in d, (
        f"decision log line must have 'central_called' field; got: {d}"
    )
    assert d["central_called"] is True, (
        f"approach5: central_called must be True when plugin is reached; got: {d}"
    )


async def test_a5_i5_plugin_calls_central_via_http_route_one_call_per_decision(
    a5_ctrl, a5_gw_app, log_path,
):
    """The approach5 plugin calls /central/decide over its HTTP route, not as a
    direct Python function call.  Each plugin-level decision increments the
    route's call counter (stored in state.central_decide_calls) by exactly one.

    The counter is read via GET /control/central-decide-count.  It is reset to
    zero on /control/reset (which runs before every test).

    This test also verifies that a gateway-core denial (GRANT) does NOT call
    the route — the counter stays at zero for calls blocked before the plugin."""

    # Baseline: reset already ran; counter must be 0
    r0 = await a5_ctrl.get("/control/central-decide-count")
    assert r0.status_code == 200, (
        f"GET /control/central-decide-count must exist; got {r0.status_code}"
    )
    assert r0.json()["count"] == 0, (
        f"counter must be 0 after reset; got {r0.json()}"
    )

    # One plugin-level decision (alice submits her own expense → allowed)
    bearer = await _make_bearer(a5_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 50.0, "description": "counter test"},
        )

    r1 = await a5_ctrl.get("/control/central-decide-count")
    assert r1.json()["count"] == 1, (
        f"plugin must call /central/decide exactly once per decision; "
        f"got count={r1.json()['count']}"
    )

    # A gateway-core denial (GRANT — expense-assistant cannot call pay_vendor)
    # must NOT reach the plugin, so counter stays at 1
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session2 = _GwSession(client)
        await session2.initialize()
        await session2.call_tool(
            "pay_vendor",
            {"vendor": "acme-hotels", "amount": 10.0, "reference": "gc-ref"},
        )

    r2 = await a5_ctrl.get("/control/central-decide-count")
    assert r2.json()["count"] == 1, (
        f"counter must not increase for a GRANT denial (plugin never called); "
        f"got count={r2.json()['count']}"
    )


async def test_a5_i5_plugin_ignores_token_person_claims_uses_live_state(
    a5_ctrl, a5_gw_app, log_path,
):
    """Approach 5 reads P4 from live state, not the token's delegations_received.

    Issue a token for dan while carol's delegation is active (token carries the claim).
    Revoke the delegation.  Call with the SAME token → DENIED because the central
    service reads live state, not the token.  This is the opposite of approach 4.
    """
    # Step 1: issue token while delegation is active
    bearer = await _make_bearer(a5_ctrl, "dan", "travel-assistant", lifetime=300)
    claims = _decode_unverified(bearer)
    assert any(
        d.get("delegator") == "carol"
        for d in claims.get("delegations_received", [])
    ), "Precondition: token must carry carol→dan delegation"

    # Step 2: revoke the delegation in live state
    r = await a5_ctrl.post("/control/revoke-delegation",
                            json={"delegation_id": "del-001"})
    r.raise_for_status()

    # Step 3: same token → DENIED (live state has no delegation)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "book_travel",
            {"traveller": "carol", "details": "Rome, 2027-05-01"},
        )

    assert result.get("isError") is True, (
        "approach5 must deny booking with a token that carries an active delegation "
        "claim if the live delegation has been revoked.  "
        f"Got: {result}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P4", (
        "denial must be P4 (delegation check via live state)"
    )


# ===========================================================================
# Spec item 6 — Combining rules
# ===========================================================================

async def test_a5_i6_central_refusal_wins_over_use_case_permit(
    a5_ctrl, a5_gw_app, log_path,
):
    """S12: expenses use-case policy has no P1 rule (it lives in central), so the
    use-case policy alone would permit alice submitting for bob.  The central policy
    has P1, which forbids it.  Result: deny, rule=P1 (from central)."""
    import cedarpy

    # Verify directly that the approach5 expenses use-case policy alone would
    # evaluate to ALLOW for a claimant mismatch (P1 is not in the use-case file).
    uc_text = (_A5_POLICIES_DIR / "expenses.cedar").read_text(encoding="utf-8")
    uc_policy = cedarpy.PolicySet.from_str(uc_text)
    request = {
        "principal": {"type": "User", "id": "alice"},
        "action":    {"type": "Action", "id": "submit_expense"},
        "resource":  {"type": "Resource", "id": "r"},
        "context":   {"action_name": "submit_expense", "claimant": "bob",
                      "amount_pence": 10000, "has_approval_ref": False},
    }
    entities = [{"uid": {"type": "User", "id": "alice"},
                 "attrs": {"username": "alice", "role": "employee"},
                 "parents": []}]
    uc_result = cedarpy.is_authorized(
        request=request, policies=uc_policy, entities=entities,
    )
    assert uc_result.allowed, (
        "S12 precondition: the approach5 expenses use-case policy must ALLOW a "
        "claimant mismatch (P1 is not there); got: denied"
    )

    # Now run through the full approach5 gateway — central P1 must refuse.
    bearer = await _make_bearer(a5_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "submit_expense",
            {"claimant": "bob", "amount": 100.0, "description": "S12 test"},
        )

    assert result.get("isError") is True, (
        f"S12: the combined evaluation must deny even though use-case permits; got: {result}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P1", (
        f"S12: central P1 must be the logged rule; got: {d['rule']!r}"
    )


async def test_a5_i6_use_case_refusal_wins_when_central_permits(
    a5_ctrl, a5_gw_app, log_path,
):
    """Use-case P7 refuses alice (employee) via payments-agent calling an approved vendor.
    Central policy has no rule that would refuse this combination, so central permits.
    Use-case refusal still wins: result is deny, rule=P7."""
    bearer = await _make_bearer(a5_ctrl, "alice", "payments-agent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "pay_vendor",
            {"vendor": "acme-hotels", "amount": 100.0, "reference": "ref-uc"},
        )

    assert result.get("isError") is True, (
        f"use-case P7 must refuse alice via payments-agent; got: {result}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P7", (
        f"when only use-case refuses, rule must be P7; got: {d['rule']!r}"
    )


async def test_a5_i6_log_central_rule_when_both_refuse(
    a5_ctrl, a5_gw_app, log_path,
):
    """When both use-case (P7) and central (P5) refuse, the logged rule is P5 (central).

    alice (employee, not finance) via payments-agent pays rogue-vendor:
      - Use-case P7 fires (alice.role != 'finance').
      - Central P5 fires (rogue-vendor not on approved list).
    Both refuse.  The logged rule must be P5 (the central rule takes precedence)."""
    bearer = await _make_bearer(a5_ctrl, "alice", "payments-agent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "pay_vendor",
            {"vendor": "rogue-vendor", "amount": 100.0, "reference": "ref-both"},
        )

    assert result.get("isError") is True, (
        f"Both P7 and P5 must refuse; got: {result}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P5", (
        f"when both sides refuse, central rule (P5) must be logged; "
        f"got: {d['rule']!r}"
    )


# ===========================================================================
# Spec item 7 — Central source down
# ===========================================================================

async def test_a5_i7_central_down_refuses_with_central_unavailable(
    a5_ctrl, a5_gw_app, log_path,
):
    """With directory_down=True, every decision needing the central service
    is refused with rule=CENTRAL_UNAVAILABLE and a clear reason."""
    await a5_ctrl.post("/control/directory-down", json={"down": True})

    bearer = await _make_bearer(a5_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 100.0, "description": "central down test"},
        )

    assert result.get("isError") is True, (
        f"central source down must refuse the call; got: {result}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "CENTRAL_UNAVAILABLE", (
        f"rule must be CENTRAL_UNAVAILABLE when central is down; got: {d['rule']!r}"
    )
    assert d.get("reason"), "reason must be a non-empty string explaining the outage"


async def test_a5_i7_directory_routes_and_central_down_together(
    a5_ctrl, a5_gw_app, log_path,
):
    """The same directory_down switch takes down both:
    (a) directory routes → 503, and
    (b) central decisions → CENTRAL_UNAVAILABLE.
    These represent the same enterprise source."""
    await a5_ctrl.post("/control/directory-down", json={"down": True})

    # (a) directory routes must 503
    r = await a5_ctrl.get("/directory/users/alice")
    assert r.status_code == 503, (
        f"directory route must 503 when central is down; got {r.status_code}"
    )

    # (b) central decisions must refuse with CENTRAL_UNAVAILABLE
    bearer = await _make_bearer(a5_ctrl, "erin", "payments-agent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "pay_vendor",
            {"vendor": "acme-hotels", "amount": 100.0, "reference": "ref-down"},
        )
    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["rule"] == "CENTRAL_UNAVAILABLE", (
        f"central decision must refuse when directory_down=True; got: {d['rule']!r}"
    )


async def test_a5_i7_identity_check_still_runs_when_central_down(
    a5_gw_app, log_path,
):
    """With central down, a request with no Bearer token is denied by IDENTITY,
    not CENTRAL_UNAVAILABLE.  Gateway-core checks run before the plugin."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as ctrl:
        await ctrl.post("/control/directory-down", json={"down": True})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},  # no Authorization header
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 100.0, "description": "no token"},
        )

    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["rule"] == "IDENTITY", (
        f"IDENTITY check must run even when central is down; got: {d['rule']!r}"
    )


async def test_a5_i7_grant_check_still_runs_when_central_down(
    a5_ctrl, a5_gw_app, log_path,
):
    """With central down, a request for a tool not granted to the agent is still
    denied by GRANT.  Gateway-core checks run before the plugin."""
    await a5_ctrl.post("/control/directory-down", json={"down": True})

    # expense-assistant does not have pay_vendor in its grants
    bearer = await _make_bearer(a5_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "pay_vendor",
            {"vendor": "acme-hotels", "amount": 100.0, "reference": "ref-grant"},
        )

    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["rule"] == "GRANT", (
        f"GRANT check must run even when central is down; got: {d['rule']!r}"
    )


async def test_a5_i7_token_issuing_unaffected_when_central_down(
    a5_ctrl, log_path,
):
    """Token-issuing endpoints work while the central source is down.
    Issuing is harness-only and must not depend on the enterprise policy source."""
    await a5_ctrl.post("/control/directory-down", json={"down": True})

    tok = await _issue_user_token(a5_ctrl, "alice", "expense-assistant")
    assert tok, "user token issuance must succeed even when central is down"

    tok2 = await _issue_agent_token(a5_ctrl, "expense-assistant")
    assert tok2, "agent token issuance must succeed even when central is down"


async def test_a5_i7_server_does_not_crash_when_central_down(
    a5_ctrl, a5_gw_app, log_path,
):
    """After a CENTRAL_UNAVAILABLE refusal, the server stays alive and processes
    further requests normally (no crash)."""
    await a5_ctrl.post("/control/directory-down", json={"down": True})

    bearer = await _make_bearer(a5_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        # This will be refused with CENTRAL_UNAVAILABLE
        await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 50.0, "description": "down"},
        )

    # Restore central and verify a subsequent call works
    await a5_ctrl.post("/control/directory-down", json={"down": False})

    bearer2 = await _make_bearer(a5_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer2}"},
    ) as client:
        session2 = _GwSession(client)
        await session2.initialize()
        result2 = await session2.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 50.0, "description": "after restore"},
        )

    assert result2.get("isError") is not True, (
        f"server must process requests normally after central is restored; got: {result2}"
    )


# ===========================================================================
# Spec item 8 — Measurements (central_called, central_duration_ms)
# ===========================================================================

async def test_a5_i8_decision_line_has_central_called_field(
    a5_ctrl, a5_gw_app, log_path,
):
    """Every decision line written by the approach5 gateway has a 'central_called' field."""
    bearer = await _make_bearer(a5_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 100.0, "description": "measurement test"},
        )

    d = _last_decision(log_path)
    assert "central_called" in d, (
        f"decision log line must have 'central_called'; got keys: {list(d.keys())}"
    )
    assert isinstance(d["central_called"], bool), (
        f"central_called must be a boolean; got: {d['central_called']!r}"
    )


async def test_a5_i8_decision_line_has_central_duration_ms(
    a5_ctrl, a5_gw_app, log_path,
):
    """Every decision line has 'central_duration_ms' (real milliseconds, ≥ 0)."""
    bearer = await _make_bearer(a5_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 100.0, "description": "timing test"},
        )

    d = _last_decision(log_path)
    assert "central_duration_ms" in d, (
        f"decision log line must have 'central_duration_ms'; got keys: {list(d.keys())}"
    )
    assert isinstance(d["central_duration_ms"], (int, float)), (
        f"central_duration_ms must be numeric; got: {d['central_duration_ms']!r}"
    )
    assert d["central_duration_ms"] >= 0, (
        f"central_duration_ms must be ≥ 0; got: {d['central_duration_ms']}"
    )


async def test_a5_i8_central_called_true_for_plugin_decisions(
    a5_ctrl, a5_gw_app, log_path,
):
    """Decisions that reach the approach5 plugin (past gateway-core checks) have
    central_called=True and central_duration_ms > 0."""
    bearer = await _make_bearer(a5_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 100.0, "description": "plugin reached"},
        )

    d = _last_decision(log_path)
    assert d.get("central_called") is True, (
        f"central_called must be True when the plugin was reached; got: {d}"
    )
    assert d.get("central_duration_ms", -1) >= 0, (
        f"central_duration_ms must be ≥ 0 when central was called; got: {d}"
    )


async def test_a5_i8_central_called_false_for_gateway_core_deny(
    a5_gw_app, log_path,
):
    """Decisions refused by GRANT (gateway-core, before the plugin) have
    central_called=False — the plugin was never invoked."""
    # expense-assistant does not have pay_vendor; a GRANT denial occurs before plugin
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as ctrl:
        bearer = await _make_bearer(ctrl, "alice", "expense-assistant")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        await session.call_tool(
            "pay_vendor",
            {"vendor": "acme-hotels", "amount": 50.0, "reference": "ref-gc"},
        )

    d = _last_decision(log_path)
    assert d["rule"] == "GRANT", f"prerequisite: expect GRANT denial; got: {d['rule']!r}"
    assert d.get("central_called") is False, (
        f"central_called must be False for a gateway-core denial; got: {d}"
    )


# ===========================================================================
# Spec item 9 — Runner
# ===========================================================================

def test_a5_i9_runner_starts_gateway_with_approach5_plugin(tmp_path):
    """The runner's domain_server with GATEWAY_PLUGIN=approach5 starts successfully
    and serves /gateway/mcp."""
    from runner._server import domain_server

    decision_log = str(tmp_path / "decisions.jsonl")
    with domain_server(
        decision_log,
        env_extra={"GATEWAY": "true", "GATEWAY_PLUGIN": "approach5"},
    ) as (port, base_url):
        resp = httpx.post(
            f"{base_url}/gateway/mcp",
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test-a5-runner", "version": "0"},
                },
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert resp.status_code == 200, (
        f"approach5 runner server must serve /gateway/mcp; "
        f"got HTTP {resp.status_code}: {resp.text[:400]}"
    )


def test_a5_i9_runner_strips_test_plugin_vars(tmp_path, monkeypatch):
    """Even when _GATEWAY_TESTING and GATEWAY_TEST_PLUGIN are in the parent env,
    the runner strips them so the approach5 plugin is used."""
    monkeypatch.setenv("_GATEWAY_TESTING", "1")
    monkeypatch.setenv("GATEWAY_TEST_PLUGIN", "allow_all")

    from runner._server import domain_server

    decision_log = str(tmp_path / "decisions.jsonl")
    with domain_server(
        decision_log,
        env_extra={"GATEWAY": "true", "GATEWAY_PLUGIN": "approach5"},
    ) as (port, base_url):
        resp = httpx.post(
            f"{base_url}/gateway/mcp",
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0"},
                },
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert resp.status_code == 200


# ===========================================================================
# Acceptance tests
# ===========================================================================

async def test_a5_acc_revocation_immediate_same_token(
    a5_ctrl, a5_gw_app, log_path,
):
    """ACCEPTANCE: Revocation takes effect on the very next call with the SAME token.

    This is the OPPOSITE of approach 4's staleness test.  Approach 5 reads live state
    from the central service, so a revoked delegation is refused immediately even if
    the same token still carries the (now stale) delegation claim.

    Steps:
    1. Issue token for dan while carol's travel delegation is active.
    2. Revoke carol's delegation via /control/revoke-delegation.
    3. Call book_travel for carol with the SAME token → DENIED (P4 from live state).
    """
    # Step 1: issue token while delegation is active
    bearer = await _make_bearer(a5_ctrl, "dan", "travel-assistant", lifetime=300)
    claims = _decode_unverified(bearer)
    assert any(
        d.get("delegator") == "carol"
        for d in claims.get("delegations_received", [])
    ), "Precondition: token must carry carol→dan travel delegation"

    # Step 2: revoke
    r = await a5_ctrl.post("/control/revoke-delegation",
                            json={"delegation_id": "del-001"})
    r.raise_for_status()

    # Step 3: same token → DENIED immediately
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "book_travel",
            {"traveller": "carol", "details": "Rome, 2027-05-01"},
        )

    assert result.get("isError") is True, (
        "ACCEPTANCE: approach5 must deny the booking with the same token immediately "
        "after revocation.  The central service reads live state, not the token claim.  "
        f"Got: {result}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P4", (
        f"denial must be P4 (live delegation check); got: {d['rule']!r}"
    )


async def test_a5_acc_s12_use_case_permits_central_forbids_still_denied(
    a5_ctrl, a5_gw_app, log_path,
):
    """ACCEPTANCE S12: A use-case file that permits something the central policy forbids
    is still refused by the central rule.

    Scenario: alice submits an expense with claimant=bob via expense-assistant.
      - The approach5 expenses use-case policy has NO P1 rule → use-case evaluates ALLOW.
      - The central policy has P1 → central evaluates DENY with P1.
      - Combined result: DENY, rule=P1 (central wins).
    """
    import cedarpy

    # Confirm the use-case policy alone would allow this (P1 is absent from it)
    uc_text = (_A5_POLICIES_DIR / "expenses.cedar").read_text(encoding="utf-8")
    assert "P1" not in _extract_rule_ids(uc_text), (
        "S12 precondition: expenses.cedar must not contain P1"
    )
    uc_policy = cedarpy.PolicySet.from_str(uc_text)
    uc_request = {
        "principal": {"type": "User", "id": "alice"},
        "action":    {"type": "Action", "id": "submit_expense"},
        "resource":  {"type": "Resource", "id": "r"},
        "context":   {"action_name": "submit_expense", "claimant": "bob",
                      "amount_pence": 10000, "has_approval_ref": False},
    }
    uc_entities = [{"uid": {"type": "User", "id": "alice"},
                    "attrs": {"username": "alice", "role": "employee"},
                    "parents": []}]
    uc_result = cedarpy.is_authorized(
        request=uc_request, policies=uc_policy, entities=uc_entities,
    )
    assert uc_result.allowed, (
        "S12 precondition: use-case policy must ALLOW the P1 violation (P1 not there)"
    )

    # Full gateway run: combined evaluation must deny with P1
    bearer = await _make_bearer(a5_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "submit_expense",
            {"claimant": "bob", "amount": 100.0, "description": "S12 acceptance"},
        )

    assert result.get("isError") is True, (
        f"S12: must be denied even though use-case permits; got: {result}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P1", (
        f"S12: logged rule must be P1 (central); got: {d['rule']!r}"
    )


async def test_a5_acc_central_down_refused_and_server_stable(
    a5_ctrl, a5_gw_app, log_path,
):
    """ACCEPTANCE: With the central source down, a call is refused with
    CENTRAL_UNAVAILABLE and the server does not crash.

    Steps:
    1. Bring central source down.
    2. Make a valid (would-be-allowed) call → CENTRAL_UNAVAILABLE.
    3. Bring central source back up.
    4. Repeat the same call → allowed (proves server is still alive).
    """
    # Step 1: take central down
    await a5_ctrl.post("/control/directory-down", json={"down": True})

    # Step 2: refused with CENTRAL_UNAVAILABLE
    bearer = await _make_bearer(a5_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result_down = await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 50.0, "description": "central down"},
        )

    assert result_down.get("isError") is True, (
        f"ACCEPTANCE: must refuse when central is down; got: {result_down}"
    )
    d_down = _last_decision(log_path)
    assert d_down["rule"] == "CENTRAL_UNAVAILABLE", (
        f"rule must be CENTRAL_UNAVAILABLE; got: {d_down['rule']!r}"
    )

    # Step 3: restore central
    await a5_ctrl.post("/control/directory-down", json={"down": False})

    # Step 4: server is alive; same call succeeds
    bearer2 = await _make_bearer(a5_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer2}"},
    ) as client:
        session2 = _GwSession(client)
        await session2.initialize()
        result_up = await session2.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 50.0, "description": "after restore"},
        )

    assert result_up.get("isError") is not True, (
        f"ACCEPTANCE: server must be alive after CENTRAL_UNAVAILABLE refusal; "
        f"got: {result_up}"
    )
