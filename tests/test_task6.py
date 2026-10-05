"""Tests for task 6: non-agent channel POST /app/expenses.

Spec summary (CLAUDE.md "Current task"):
1. POST /app/expenses lives only in gateway mode (inside GATEWAY markers).
2. USER token (no act claim), aud=expenses-app, scope expenses:submit required.
3. Calls /central/decide for the common rules; executes only on allow.
4. Logs with channel="app" and no agent; agent-path logs channel="agent".
5. Runner supports sending the same request via app and agent in one scenario.

Acceptance from CLAUDE.md:
- After limit 500→300: 400 expense via app refused in approaches 4, 5, 6.
- Via agent: ALLOWED in approach 4 (own copy), refused in approach 5 (live),
  allowed in approach 6 before next publication, refused after.
- App refuses agent token, wrong audience, missing expenses:submit.

ALL tests must FAIL against the current code.
Naming: test_t6_<spec_item>_<description>
"""
import json
import pathlib

import httpx
import pytest
import pytest_asyncio

from domain import tokens, simclock
from domain.grants import AGENT_GRANTS as GATEWAY_GRANTS
from runner.run import _AGENT_SCOPES

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_APP_AUD = "expenses-app"
_APP_SCOPE = "expenses:submit"

TOOL_SCOPES: dict[str, str] = {
    "read_receipt":    "expenses:read",
    "submit_expense":  "expenses:submit",
    "approve_expense": "expenses:approve",
    "book_travel":     "travel:book",
    "pay_vendor":      "payments:pay",
}

_REPO = pathlib.Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fingerprints() -> dict[str, str]:
    from domain.gateway import _compute_fingerprint
    from domain.server import mcp
    return {
        name: _compute_fingerprint(tool.name, tool.description, tool.parameters)
        for name, tool in mcp._tool_manager._tools.items()
    }


async def _issue_app_token(ctrl: httpx.AsyncClient, sub: str,
                            scopes: list[str] | None = None,
                            lifetime: int = 300) -> str:
    """Issue a USER token for the expenses-app audience (no act claim)."""
    if scopes is None:
        scopes = [_APP_SCOPE]
    r = await ctrl.post("/control/identity/user-token", json={
        "sub": sub, "aud": _APP_AUD, "scope": scopes, "lifetime": lifetime,
    })
    r.raise_for_status()
    return r.json()["access_token"]


async def _issue_agent_bearer(ctrl: httpx.AsyncClient, user: str, agent: str,
                               scopes: list[str] | None = None,
                               lifetime: int = 7200) -> str:
    """Issue a full on-behalf-of token for the gateway MCP endpoint."""
    if scopes is None:
        scopes = _AGENT_SCOPES[agent]
    r = await ctrl.post("/control/identity/user-token", json={
        "sub": user, "aud": agent, "scope": scopes, "lifetime": lifetime,
    })
    r.raise_for_status()
    user_tok = r.json()["access_token"]
    r = await ctrl.post("/control/identity/agent-token", json={
        "sub": agent, "scope": scopes, "lifetime": lifetime,
    })
    r.raise_for_status()
    agent_tok = r.json()["access_token"]
    r = await ctrl.post("/identity/exchange", json={
        "subject_token": user_tok, "actor_token": agent_tok,
    })
    r.raise_for_status()
    return r.json()["access_token"]


async def _post_app_expense(client: httpx.AsyncClient, token: str,
                             claimant: str, amount: float,
                             description: str = "test expense") -> httpx.Response:
    return await client.post(
        "/app/expenses",
        json={"claimant": claimant, "amount": amount, "description": description},
        headers={"Authorization": f"Bearer {token}"},
    )


def _decisions(log_path: pathlib.Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [
        json.loads(l) for l in log_path.read_text().splitlines()
        if l.strip() and json.loads(l).get("type") == "decision"
    ]


def _last_decision(log_path: pathlib.Path) -> dict:
    decs = _decisions(log_path)
    assert decs, "no decision lines in log"
    return decs[-1]


class _GwSession:
    """Minimal gateway MCP session for acceptance tests."""

    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client
        self._session_id: str | None = None
        self._n = 0

    async def initialize(self) -> None:
        self._n += 1
        r = await self._post({
            "jsonrpc": "2.0", "id": self._n,
            "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "t6-test", "version": "0"}},
        })
        self._session_id = r.headers.get("mcp-session-id")

    async def call_tool(self, name: str, arguments: dict) -> dict:
        self._n += 1
        r = await self._post({
            "jsonrpc": "2.0", "id": self._n,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        })
        return r.json().get("result", r.json())

    async def _post(self, payload: dict) -> httpx.Response:
        hdrs = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-11-25",
        }
        if self._session_id:
            hdrs["mcp-session-id"] = self._session_id
        r = await self._client.post("/gateway/mcp", json=payload, headers=hdrs)
        r.raise_for_status()
        return r


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _allow_plugin(claims, agent_chain, tool, arguments):
    return {"decision": "allow", "rule": None, "reason": "test-allow",
            "central_called": False, "central_duration_ms": 0}


@pytest_asyncio.fixture(autouse=True)
async def _app_central_transport(running_app):
    """As the approach 5 fixtures do: route the app channel's call to
    /central/decide through the in-process ASGI app instead of the network."""
    from domain import server as _srv
    _srv._app_central_transport = httpx.ASGITransport(app=running_app)
    yield
    _srv._app_central_transport = None


@pytest_asyncio.fixture
async def gw_app(running_app, log_path):
    """Generic allow-all gateway for spec items 1–4 (approach-agnostic)."""
    from domain.gateway import install_gateway, remove_gateway

    install_gateway(
        grants=GATEWAY_GRANTS,
        fingerprints=_fingerprints(),
        scope_map=TOOL_SCOPES,
        policy_plugin=_allow_plugin,
    )
    yield running_app
    remove_gateway()


@pytest_asyncio.fixture
async def gw_ctrl(gw_app) -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as client:
        yield client


@pytest_asyncio.fixture
async def a4_gw_app(running_app, log_path):
    """Gateway with approach 4 plugin for acceptance tests."""
    from arms.approach4 import plugin as _a4
    from domain.gateway import install_gateway, remove_gateway

    install_gateway(
        grants=GATEWAY_GRANTS,
        fingerprints=_fingerprints(),
        scope_map=TOOL_SCOPES,
        policy_plugin=_a4.evaluate,
    )
    yield running_app
    remove_gateway()


@pytest_asyncio.fixture
async def a4_ctrl(a4_gw_app) -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as client:
        yield client


@pytest_asyncio.fixture
async def a5_gw_app(running_app, log_path):
    """Gateway with approach 5 plugin wired to the in-process ASGI transport."""
    from arms.approach5 import plugin as _a5
    from domain.gateway import install_gateway, remove_gateway

    _a5._central_transport = httpx.ASGITransport(app=running_app)
    install_gateway(
        grants=GATEWAY_GRANTS,
        fingerprints=_fingerprints(),
        scope_map=TOOL_SCOPES,
        policy_plugin=_a5.evaluate,
    )
    yield running_app
    remove_gateway()
    _a5._central_transport = None


@pytest_asyncio.fixture
async def a5_ctrl(a5_gw_app) -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as client:
        yield client


@pytest_asyncio.fixture
async def a6_gw_app(running_app, log_path):
    """Gateway with approach 6 plugin, plus revocation/reset registration."""
    from arms.approach6 import plugin as _a6
    from domain import server as _srv
    from domain.gateway import install_gateway, remove_gateway

    _a6.reset()
    _srv.register_revocation_listener(_a6.push_revocation)
    _srv.register_reset_callback(_a6.reset)

    install_gateway(
        grants=GATEWAY_GRANTS,
        fingerprints=_fingerprints(),
        scope_map=TOOL_SCOPES,
        policy_plugin=_a6.evaluate,
    )
    yield running_app
    remove_gateway()
    _srv.deregister_revocation_listener(_a6.push_revocation)
    _srv.deregister_reset_callback(_a6.reset)


@pytest_asyncio.fixture
async def a6_ctrl(a6_gw_app) -> httpx.AsyncClient:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as client:
        yield client


# ===========================================================================
# Spec item 1 — Route placement: only in gateway mode, inside GATEWAY markers
# ===========================================================================

async def test_t6_s1_app_expenses_present_in_gateway_mode(gw_ctrl, gw_app, log_path):
    """POST /app/expenses must respond (not 404) when the gateway is active."""
    tok = await _issue_app_token(gw_ctrl, "alice")
    r = await _post_app_expense(gw_ctrl, tok, "alice", 100.0)
    assert r.status_code != 404, (
        "POST /app/expenses must exist in gateway mode; "
        f"got HTTP 404 — route not implemented or not registered"
    )


def test_t6_s1_route_handler_in_gateway_markers():
    """The /app/expenses handler must appear in domain/server.py inside a
    # --- GATEWAY BEGIN/END --- block."""
    src = (_REPO / "domain" / "server.py").read_text(encoding="utf-8")
    assert "/app/expenses" in src, (
        "domain/server.py must define the /app/expenses route handler"
    )
    # It must be inside the GATEWAY markers (not outside).
    gw_begin = src.find("# --- GATEWAY BEGIN ---")
    gw_end   = src.rfind("# --- GATEWAY END ---")
    route_pos = src.find("/app/expenses")
    assert gw_begin != -1 and gw_end != -1 and gw_begin < route_pos < gw_end, (
        "/app/expenses must live inside a GATEWAY BEGIN/END block in domain/server.py"
    )


# ===========================================================================
# Spec item 2 — Token: USER token, aud=expenses-app, scope expenses:submit
# ===========================================================================

async def test_t6_s2_valid_user_token_passes_identity(gw_ctrl, gw_app, log_path):
    """A USER token (no act claim) with aud=expenses-app and expenses:submit passes
    the IDENTITY check and reaches the policy step."""
    tok = await _issue_app_token(gw_ctrl, "alice", scopes=[_APP_SCOPE])
    r = await _post_app_expense(gw_ctrl, tok, "alice", 100.0)
    # The route must at least log a decision and not refuse at IDENTITY
    d = _last_decision(log_path)
    assert d.get("rule") != "IDENTITY", (
        f"valid user token must not be refused at IDENTITY; got rule={d.get('rule')!r}"
    )


async def test_t6_s2_agent_token_refused_identity(gw_ctrl, gw_app, log_path):
    """A token with an act claim (on-behalf-of / agent token) must be refused
    with rule=IDENTITY — the app channel accepts only pure USER tokens."""
    # Build an on-behalf-of token (which has an act claim)
    r1 = await gw_ctrl.post("/control/identity/user-token", json={
        "sub": "alice", "aud": "expense-assistant",
        "scope": [_APP_SCOPE], "lifetime": 300,
    })
    r1.raise_for_status()
    r2 = await gw_ctrl.post("/control/identity/agent-token", json={
        "sub": "expense-assistant", "scope": [_APP_SCOPE], "lifetime": 300,
    })
    r2.raise_for_status()
    r3 = await gw_ctrl.post("/identity/exchange", json={
        "subject_token": r1.json()["access_token"],
        "actor_token":   r2.json()["access_token"],
    })
    r3.raise_for_status()
    obo_token = r3.json()["access_token"]  # has act claim

    await _post_app_expense(gw_ctrl, obo_token, "alice", 100.0)

    d = _last_decision(log_path)
    assert d["decision"] == "deny", (
        f"agent token must be refused by /app/expenses; got {d['decision']!r}"
    )
    assert d["rule"] == "IDENTITY", (
        f"agent token rejection must use rule=IDENTITY; got {d['rule']!r}"
    )


async def test_t6_s2_wrong_audience_refused_identity(gw_ctrl, gw_app, log_path):
    """A USER token with aud != expenses-app must be refused with rule=IDENTITY."""
    r = await gw_ctrl.post("/control/identity/user-token", json={
        "sub": "alice", "aud": "other-app",
        "scope": [_APP_SCOPE], "lifetime": 300,
    })
    r.raise_for_status()
    bad_tok = r.json()["access_token"]

    await _post_app_expense(gw_ctrl, bad_tok, "alice", 100.0)

    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "IDENTITY", (
        f"wrong-audience token must be refused with rule=IDENTITY; got {d['rule']!r}"
    )


async def test_t6_s2_missing_expenses_submit_scope_refused(gw_ctrl, gw_app, log_path):
    """A USER token for expenses-app but without expenses:submit must be refused
    with rule=SCOPE."""
    tok = await _issue_app_token(gw_ctrl, "alice", scopes=["expenses:read"])
    await _post_app_expense(gw_ctrl, tok, "alice", 100.0)

    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "SCOPE", (
        f"token without expenses:submit must be refused with rule=SCOPE; "
        f"got {d['rule']!r}"
    )


async def test_t6_s2_no_authorization_header_refused_identity(gw_app, log_path):
    """No Authorization header at all must be refused with rule=IDENTITY."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as client:
        await client.post(
            "/app/expenses",
            json={"claimant": "alice", "amount": 100.0, "description": "no token"},
        )

    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "IDENTITY", (
        f"missing Authorization must be refused with rule=IDENTITY; got {d['rule']!r}"
    )


# ===========================================================================
# Spec item 3 — Central service: always calls /central/decide; execute on allow
# ===========================================================================

async def test_t6_s3_app_increments_central_decide_counter(gw_ctrl, gw_app, log_path):
    """Each POST /app/expenses call increments the /central/decide call counter by 1.

    This verifies the route calls /central/decide (not a direct import of the
    central_service module or a Cedar file).
    """
    r0 = await gw_ctrl.get("/control/central-decide-count")
    assert r0.json()["count"] == 0, "counter must start at 0"

    tok = await _issue_app_token(gw_ctrl, "alice")
    await _post_app_expense(gw_ctrl, tok, "alice", 100.0)

    r1 = await gw_ctrl.get("/control/central-decide-count")
    assert r1.json()["count"] == 1, (
        f"/app/expenses must call /central/decide exactly once per request; "
        f"got count={r1.json()['count']}"
    )


async def test_t6_s3_app_executes_submission_when_central_allows(gw_ctrl, gw_app, log_path):
    """When central allows (limit=500, amount=100 ≤ limit), the expense appears
    in the ledger — submission was executed."""
    tok = await _issue_app_token(gw_ctrl, "alice")
    await _post_app_expense(gw_ctrl, tok, "alice", 100.0, description="ledger-test")

    r = await gw_ctrl.get("/control/ledger")
    r.raise_for_status()
    submitted = [e for e in r.json() if e.get("action_type") == "expense_submitted"]
    assert submitted, (
        "when central allows, /app/expenses must execute and record expense_submitted "
        "in the ledger"
    )


async def test_t6_s3_app_refuses_when_central_denies(gw_ctrl, gw_app, log_path):
    """After lowering the limit to 100, a 200 expense is refused by central (P2).
    The submission is NOT executed (not in ledger)."""
    await gw_ctrl.post("/control/set-limit", json={"limit": 100})

    tok = await _issue_app_token(gw_ctrl, "alice")
    await _post_app_expense(gw_ctrl, tok, "alice", 200.0)

    d = _last_decision(log_path)
    assert d["decision"] == "deny", (
        f"/app/expenses must refuse when central denies; got {d['decision']!r}"
    )
    assert d["rule"] == "P2", (
        f"central P2 must fire when amount > limit; got rule={d['rule']!r}"
    )

    r = await gw_ctrl.get("/control/ledger")
    submitted_200 = [
        e for e in r.json()
        if e.get("action_type") == "expense_submitted" and e.get("amount", 0) == 200.0
    ]
    assert not submitted_200, (
        "refused expense must not appear in the ledger"
    )


async def test_t6_s3_app_never_imports_central_service_module():
    """The /app/expenses route handler must not import domain.central_service directly.
    It reaches the central service exclusively through POST /central/decide."""
    srv_src = (_REPO / "domain" / "server.py").read_text(encoding="utf-8")
    # Locate the /app/expenses handler block
    app_pos = srv_src.find("/app/expenses")
    assert app_pos != -1, (
        "prerequisite: /app/expenses route must be in domain/server.py"
    )
    # Scan the 100 lines after the route registration for forbidden direct imports
    snippet = srv_src[app_pos: app_pos + 3000]
    for forbidden in ("central_service.decide(", "import central_service",
                      "from domain import central_service",
                      "from domain.central_service"):
        assert forbidden not in snippet, (
            f"/app/expenses handler must not call {forbidden!r} directly; "
            "use POST /central/decide instead"
        )


# ===========================================================================
# Spec item 4 — Logging: channel field in decision entries
# ===========================================================================

async def test_t6_s4_app_decision_has_channel_app(gw_ctrl, gw_app, log_path):
    """Decision lines from POST /app/expenses have channel='app'."""
    tok = await _issue_app_token(gw_ctrl, "alice")
    await _post_app_expense(gw_ctrl, tok, "alice", 100.0)

    d = _last_decision(log_path)
    assert "channel" in d, (
        f"app decision must have a 'channel' field; keys present: {sorted(d.keys())}"
    )
    assert d["channel"] == "app", (
        f"app decision channel must be 'app'; got {d['channel']!r}"
    )


async def test_t6_s4_app_decision_has_no_agent(gw_ctrl, gw_app, log_path):
    """Decision lines from POST /app/expenses must have agent=None (no agent in chain)."""
    tok = await _issue_app_token(gw_ctrl, "alice")
    await _post_app_expense(gw_ctrl, tok, "alice", 100.0)

    d = _last_decision(log_path)
    assert d.get("agent") is None, (
        f"app decision must have agent=None; got {d.get('agent')!r}"
    )


async def test_t6_s4_app_decision_logs_user_from_token(gw_ctrl, gw_app, log_path):
    """The 'user' field in the app decision is the sub claim of the token."""
    tok = await _issue_app_token(gw_ctrl, "alice")
    await _post_app_expense(gw_ctrl, tok, "alice", 100.0)

    d = _last_decision(log_path)
    assert d.get("user") == "alice", (
        f"app decision must log user='alice' (from token sub); got {d.get('user')!r}"
    )


async def test_t6_s4_agent_decision_has_channel_agent(gw_ctrl, gw_app, log_path):
    """Decision lines from /gateway/mcp (agent path) have channel='agent'."""
    bearer = await _issue_agent_bearer(gw_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 100.0, "description": "channel-agent-test"},
        )

    d = _last_decision(log_path)
    assert "channel" in d, (
        f"agent-path decision must have a 'channel' field; keys: {sorted(d.keys())}"
    )
    assert d["channel"] == "agent", (
        f"agent-path decision channel must be 'agent'; got {d['channel']!r}"
    )


# ===========================================================================
# Spec item 5 — Runner support for app+agent combined scenario
# ===========================================================================

def test_t6_s5_runner_source_has_app_expenses_support():
    """runner/run.py must include support for calling POST /app/expenses so the
    runner can drive a combined app+agent scenario after a central limit change."""
    src = (_REPO / "runner" / "run.py").read_text(encoding="utf-8")
    assert "/app/expenses" in src, (
        "runner/run.py must include logic for posting to /app/expenses; "
        "the runner must be able to exercise the app channel in a scenario "
        "alongside the agent channel"
    )


# ===========================================================================
# Acceptance tests: limit 500→300, app vs agent, all three approaches
# ===========================================================================

# ---------------------------------------------------------------------------
# Approach 4
# ---------------------------------------------------------------------------

async def test_t6_acc_a4_app_refuses_400_after_limit_300(
    a4_ctrl, a4_gw_app, log_path,
):
    """ACCEPTANCE / approach 4: After limit lowered to 300, a £400 expense via
    POST /app/expenses is refused immediately by central (P2).

    The app always calls /central/decide with live state, regardless of which
    approach plugin handles the agent path.
    """
    await a4_ctrl.post("/control/set-limit", json={"limit": 300})

    tok = await _issue_app_token(a4_ctrl, "alice", lifetime=7200)
    await _post_app_expense(a4_ctrl, tok, "alice", 400.0)

    d = _last_decision(log_path)
    assert d["decision"] == "deny", (
        "ACCEPTANCE/approach4: app must refuse 400 after limit=300; "
        f"got decision={d['decision']!r}"
    )
    assert d["rule"] == "P2", (
        f"ACCEPTANCE/approach4: app refusal must be P2; got {d['rule']!r}"
    )
    assert d.get("channel") == "app", (
        f"ACCEPTANCE/approach4: app decision must have channel='app'; "
        f"got {d.get('channel')!r}"
    )


async def test_t6_acc_a4_agent_allows_400_after_limit_300(
    a4_ctrl, a4_gw_app, log_path,
):
    """ACCEPTANCE / approach 4: After limit lowered to 300, the same £400 expense
    via the agent (/gateway/mcp) is ALLOWED.

    Approach 4 evaluates its own Cedar policy copy with a hard-coded £500 threshold
    (not the live central limit).  40000 pence < 50000 pence → pass P2.
    """
    await a4_ctrl.post("/control/set-limit", json={"limit": 300})

    bearer = await _issue_agent_bearer(a4_ctrl, "alice", "expense-assistant",
                                        lifetime=7200)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 400.0, "description": "a4-agent-test"},
        )

    d = _last_decision(log_path)
    assert d["decision"] == "allow", (
        "ACCEPTANCE/approach4: agent must ALLOW 400 after limit=300 — approach 4 "
        "uses its own Cedar policy copy with hard-coded £500 threshold, "
        f"not the live central limit; got {d['decision']!r}, rule={d.get('rule')!r}"
    )
    assert d.get("channel") == "agent", (
        f"agent decision must have channel='agent'; got {d.get('channel')!r}"
    )


# ---------------------------------------------------------------------------
# Approach 5
# ---------------------------------------------------------------------------

async def test_t6_acc_a5_app_refuses_400_after_limit_300(
    a5_ctrl, a5_gw_app, log_path,
):
    """ACCEPTANCE / approach 5: After limit lowered to 300, a £400 expense via
    POST /app/expenses is refused immediately (app calls /central/decide live)."""
    await a5_ctrl.post("/control/set-limit", json={"limit": 300})

    tok = await _issue_app_token(a5_ctrl, "alice", lifetime=7200)
    await _post_app_expense(a5_ctrl, tok, "alice", 400.0)

    d = _last_decision(log_path)
    assert d["decision"] == "deny", (
        f"ACCEPTANCE/approach5: app must refuse 400 after limit=300; "
        f"got {d['decision']!r}"
    )
    assert d["rule"] == "P2", (
        f"ACCEPTANCE/approach5: app refusal must be P2; got {d['rule']!r}"
    )


async def test_t6_acc_a5_agent_refuses_400_after_limit_300(
    a5_ctrl, a5_gw_app, log_path,
):
    """ACCEPTANCE / approach 5: After limit lowered to 300, the £400 expense via
    the agent is ALSO refused — approach 5 calls /central/decide with live state."""
    await a5_ctrl.post("/control/set-limit", json={"limit": 300})

    bearer = await _issue_agent_bearer(a5_ctrl, "alice", "expense-assistant",
                                        lifetime=7200)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a5_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 400.0, "description": "a5-agent-test"},
        )

    d = _last_decision(log_path)
    assert d["decision"] == "deny", (
        "ACCEPTANCE/approach5: agent must REFUSE 400 after limit=300 — "
        "approach 5 calls /central/decide with live state; "
        f"got {d['decision']!r}"
    )
    assert d["rule"] == "P2", (
        f"ACCEPTANCE/approach5: agent refusal must be P2; got {d['rule']!r}"
    )


# ---------------------------------------------------------------------------
# Approach 6
# ---------------------------------------------------------------------------

async def test_t6_acc_a6_app_refuses_400_after_limit_300(
    a6_ctrl, a6_gw_app, log_path,
):
    """ACCEPTANCE / approach 6: After limit lowered to 300, a £400 expense via
    POST /app/expenses is refused immediately.

    The app always calls /central/decide with live state regardless of approach.
    """
    await a6_ctrl.post("/control/set-limit", json={"limit": 300})

    tok = await _issue_app_token(a6_ctrl, "alice", lifetime=7200)
    await _post_app_expense(a6_ctrl, tok, "alice", 400.0)

    d = _last_decision(log_path)
    assert d["decision"] == "deny", (
        f"ACCEPTANCE/approach6: app must refuse 400 after limit=300; "
        f"got {d['decision']!r}"
    )
    assert d["rule"] == "P2", (
        f"ACCEPTANCE/approach6: app refusal must be P2; got {d['rule']!r}"
    )


async def test_t6_acc_a6_agent_allows_400_before_next_publication(
    a6_ctrl, a6_gw_app, log_path,
):
    """ACCEPTANCE / approach 6: After limit lowered to 300 but BEFORE the next
    publication boundary, the agent still ALLOWS the £400 expense.

    Approach 6 evaluates against the published snapshot (still limit=500).
    Step 1: Trigger initial snapshot via an agent call (while limit=500).
    Step 2: Lower limit to 300.
    Step 3: Agent call before 900-second publication boundary → ALLOWED.
    """
    # Step 1: trigger initial snapshot (limit=500)
    bearer_seed = await _issue_agent_bearer(a6_ctrl, "alice", "expense-assistant",
                                             lifetime=7200)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer_seed}"},
    ) as client:
        session_seed = _GwSession(client)
        await session_seed.initialize()
        # A small call to snapshot the current state (limit=500)
        await session_seed.call_tool("read_receipt", {"receipt_id": "rcpt-001"})

    # Step 2: lower limit — snapshot still has 500
    await a6_ctrl.post("/control/set-limit", json={"limit": 300})

    # Step 3: agent submits 400 before next publication boundary → ALLOWED
    bearer = await _issue_agent_bearer(a6_ctrl, "alice", "expense-assistant",
                                        lifetime=7200)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 400.0,
             "description": "a6-agent-before-pub"},
        )

    d = _last_decision(log_path)
    assert d["decision"] == "allow", (
        "ACCEPTANCE/approach6: agent must ALLOW 400 before next publication — "
        "the published snapshot still carries limit=500; "
        f"got {d['decision']!r}, rule={d.get('rule')!r}"
    )
    assert d.get("channel") == "agent"


async def test_t6_acc_a6_agent_refuses_400_after_publication(
    a6_ctrl, a6_gw_app, log_path,
):
    """ACCEPTANCE / approach 6: After limit lowered to 300 AND after the next
    publication boundary (900 s), the agent REFUSES the £400 expense.

    The new snapshot carries limit=300, so 40000 > 30000 → denied by P2.
    Step 1: Trigger initial snapshot via an agent call (while limit=500).
    Step 2: Lower limit to 300.
    Step 3: Advance simulated clock past the next 900-second publication boundary.
    Step 4: Agent call → REFUSED (snapshot now has limit=300).
    """
    from domain.central_publisher import _PUBLICATION_INTERVAL

    # Step 1: trigger initial snapshot
    bearer_seed = await _issue_agent_bearer(a6_ctrl, "alice", "expense-assistant",
                                             lifetime=7200)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer_seed}"},
    ) as client:
        session_seed = _GwSession(client)
        await session_seed.initialize()
        await session_seed.call_tool("read_receipt", {"receipt_id": "rcpt-001"})

    # Step 2: lower limit
    await a6_ctrl.post("/control/set-limit", json={"limit": 300})

    # Step 3: advance clock past the next publication boundary
    await a6_ctrl.post("/control/clock/advance", json={"seconds": _PUBLICATION_INTERVAL})

    # Step 4: agent call → REFUSED (snapshot updated to limit=300)
    bearer = await _issue_agent_bearer(a6_ctrl, "alice", "expense-assistant",
                                        lifetime=7200)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a6_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 400.0,
             "description": "a6-agent-after-pub"},
        )

    d = _last_decision(log_path)
    assert d["decision"] == "deny", (
        "ACCEPTANCE/approach6: agent must REFUSE 400 after publication with limit=300; "
        f"got {d['decision']!r}"
    )
    assert d["rule"] == "P2", (
        f"ACCEPTANCE/approach6: post-publication refusal must be P2; "
        f"got {d['rule']!r}"
    )
    assert d.get("channel") == "agent"
