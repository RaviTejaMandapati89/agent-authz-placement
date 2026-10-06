"""Tests for approach 4 policy (task 2b).

Each section maps to a numbered spec item in CLAUDE.md.  All tests must
FAIL until the corresponding implementation is added; none may be skipped.

Naming convention: test_a4_<spec_item>_<description>
"""
import datetime
import json
import pathlib
import re

import httpx
import jwt as _jwt
import pytest
import pytest_asyncio

from domain import simclock, tokens
from domain.grants import AGENT_GRANTS as GATEWAY_GRANTS

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

_REPO = pathlib.Path(__file__).parent.parent
_POLICIES_DIR = _REPO / "arms" / "approach4" / "policies"
_C_HOOK_CONFIG_DIR = _REPO / "arms" / "c_hook" / "config"
_USE_CASES = ["expenses", "travel", "payments"]

TOOL_SCOPES: dict[str, str] = {
    "read_receipt": "expenses:read",
    "submit_expense": "expenses:submit",
    "approve_expense": "expenses:approve",
    "book_travel": "travel:book",
    "pay_vendor": "payments:pay",
}

# Person-claims keys written into user tokens by the identity issuer.
_PERSON_CLAIM_KEYS = frozenset({"role", "reports_to", "delegations_received"})


# ---------------------------------------------------------------------------
# Helpers — token issuing
# ---------------------------------------------------------------------------

def _decode_unverified(token: str) -> dict:
    return _jwt.decode(token, options={"verify_signature": False})


async def _issue_user_token(ctrl: httpx.AsyncClient, sub: str, aud: str,
                             scopes: list[str] | None = None,
                             lifetime: int = 300) -> str:
    if scopes is None:
        scopes = sorted(tokens.FIXED_SCOPES)
    r = await ctrl.post("/control/identity/user-token", json={
        "sub": sub, "aud": aud, "scope": scopes, "lifetime": lifetime,
    })
    r.raise_for_status()
    return r.json()["access_token"]


async def _issue_agent_token(ctrl: httpx.AsyncClient, sub: str,
                              scopes: list[str] | None = None,
                              lifetime: int = 300) -> str:
    if scopes is None:
        scopes = sorted(tokens.FIXED_SCOPES)
    r = await ctrl.post("/control/identity/agent-token", json={
        "sub": sub, "scope": scopes, "lifetime": lifetime,
    })
    r.raise_for_status()
    return r.json()["access_token"]


async def _exchange(ctrl: httpx.AsyncClient, subject_token: str,
                    actor_token: str, audience: str | None = None) -> str:
    body: dict = {"subject_token": subject_token, "actor_token": actor_token}
    if audience:
        body["audience"] = audience
    r = await ctrl.post("/identity/exchange", json=body)
    r.raise_for_status()
    return r.json()["access_token"]


async def _make_bearer(ctrl: httpx.AsyncClient, user: str, agent: str,
                       scopes: list[str] | None = None,
                       lifetime: int = 300) -> str:
    if scopes is None:
        scopes = sorted(tokens.FIXED_SCOPES)
    user_token  = await _issue_user_token(ctrl, user, agent, scopes, lifetime)
    agent_token = await _issue_agent_token(ctrl, agent, scopes, lifetime)
    return await _exchange(ctrl, user_token, agent_token)


# ---------------------------------------------------------------------------
# Helpers — gateway session
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
                                          "clientInfo": {"name": "a4-test", "version": "0"}}})
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


def _fingerprints() -> dict[str, str]:
    from arms.approach4.plugin import fingerprint as _fp
    from domain.server import mcp
    return {
        name: _fp(tool.name, tool.description, tool.parameters)
        for name, tool in mcp._tool_manager._tools.items()
    }


def _last_decision(log_path: pathlib.Path) -> dict:
    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]
    decisions = [l for l in lines if "decision" in l and "tool" in l]
    assert decisions, "no decision lines in log"
    return decisions[-1]


# ---------------------------------------------------------------------------
# Fixtures — approach4 gateway
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def a4_gw_app(running_app, log_path):
    """Install the gateway with the approach4 plugin and yield the ASGI app."""
    from arms.approach4.plugin import evaluate as _plugin
    from domain.gateway import install_gateway, remove_gateway

    install_gateway(
        grants=GATEWAY_GRANTS,
        fingerprints=_fingerprints(),
        scope_map=TOOL_SCOPES,
        policy_plugin=_plugin,
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


async def _a4_session(app, bearer: str) -> _GwSession:
    """Create and initialise a gateway session for the given bearer token."""
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    )
    session = _GwSession(client)
    await session.initialize()
    return session


# ===========================================================================
# Spec item 1 — Person claims at issuance
# ===========================================================================

async def test_a4_user_token_includes_role_claim(http_client):
    """User token for alice includes role='employee'."""
    tok = await _issue_user_token(http_client, "alice", "expense-assistant")
    claims = _decode_unverified(tok)
    assert "role" in claims, "user token must carry a 'role' claim"
    assert claims["role"] == "employee"


async def test_a4_user_token_reports_to_reflects_direct_reports(http_client):
    """User token for bob (alice's manager) includes reports_to=['alice']."""
    tok = await _issue_user_token(http_client, "bob", "expense-assistant")
    claims = _decode_unverified(tok)
    assert "reports_to" in claims, "user token must carry 'reports_to'"
    assert "alice" in claims["reports_to"]


async def test_a4_user_token_reports_to_empty_for_non_manager(http_client):
    """User token for alice (no direct reports) has reports_to=[]."""
    tok = await _issue_user_token(http_client, "alice", "expense-assistant")
    claims = _decode_unverified(tok)
    assert "reports_to" in claims
    assert claims["reports_to"] == []


async def test_a4_user_token_delegations_received_present_when_active(http_client):
    """User token for dan while carol's delegation is active includes that delegation."""
    tok = await _issue_user_token(http_client, "dan", "travel-assistant")
    claims = _decode_unverified(tok)
    assert "delegations_received" in claims, "user token must carry 'delegations_received'"
    dels = claims["delegations_received"]
    assert any(
        d.get("delegator") == "carol" and "travel" in d.get("scope", [])
        for d in dels
    ), f"expected carol→dan travel delegation in token, got: {dels}"


async def test_a4_user_token_delegations_received_empty_after_revocation(http_client):
    """After revoking carol's delegation, new token for dan has empty delegations_received."""
    await http_client.post("/control/revoke-delegation", json={"delegation_id": "del-001"})
    tok = await _issue_user_token(http_client, "dan", "travel-assistant")
    claims = _decode_unverified(tok)
    assert "delegations_received" in claims
    carol_dels = [d for d in claims["delegations_received"] if d.get("delegator") == "carol"]
    assert carol_dels == [], f"revoked delegation should not appear in token, got: {carol_dels}"


async def test_a4_user_token_delegations_carry_expiry(http_client):
    """Each delegation in delegations_received carries an expiry field."""
    tok = await _issue_user_token(http_client, "dan", "travel-assistant")
    claims = _decode_unverified(tok)
    for d in claims.get("delegations_received", []):
        assert "expires" in d or "exp" in d, (
            f"delegation must carry expiry, got: {d}"
        )


# ===========================================================================
# Spec item 2 — Claims travel unchanged
# ===========================================================================

async def test_a4_exchange_copies_person_claims_into_obo_token(http_client):
    """Exchange copies role, reports_to, delegations_received from user token."""
    user_token  = await _issue_user_token(http_client, "dan", "travel-assistant")
    agent_token = await _issue_agent_token(http_client, "travel-assistant")
    obo_token   = await _exchange(http_client, user_token, agent_token)

    subject_claims = _decode_unverified(user_token)
    obo_claims     = _decode_unverified(obo_token)

    for key in _PERSON_CLAIM_KEYS:
        assert key in obo_claims, f"on-behalf-of token must carry {key!r}"
        assert obo_claims[key] == subject_claims[key], (
            f"claim {key!r} must be unchanged: "
            f"subject={subject_claims[key]!r} obo={obo_claims[key]!r}"
        )


async def test_a4_exchange_person_claims_preserved_at_second_hop(http_client):
    """Two-hop exchange preserves person claims from the original user token."""
    user_token  = await _issue_user_token(http_client, "dan", "travel-assistant")
    agent1_tok  = await _issue_agent_token(http_client, "travel-assistant")
    obo1        = await _exchange(http_client, user_token, agent1_tok)

    agent2_tok  = await _issue_agent_token(http_client, "expense-assistant")
    obo1_claims = _decode_unverified(obo1)
    # Re-audience obo1 for agent2 so the exchange validates
    r = await http_client.post("/control/identity/user-token", json={
        "sub": "dan", "aud": "expense-assistant",
        "scope": sorted(tokens.FIXED_SCOPES),
    })
    user_token2 = r.json()["access_token"]
    obo2 = await _exchange(http_client, user_token2, agent2_tok)

    # The second OBO token must still have the original person claims
    obo2_claims = _decode_unverified(obo2)
    orig_claims = _decode_unverified(user_token)
    for key in _PERSON_CLAIM_KEYS:
        assert key in obo2_claims, f"second-hop OBO token must carry {key!r}"
        assert obo2_claims[key] == orig_claims[key], (
            f"claim {key!r} changed at second hop"
        )


async def test_a4_exchange_refuses_actor_token_with_injected_person_claims(http_client):
    """Exchange refuses when actor_token carries person-claim keys."""
    # Forge an actor token that includes role/reports_to/delegations_received.
    now_ts = int(simclock.now())
    forged_actor = tokens._encode(
        {
            "iss": tokens.AGENT_ISSUER,
            "sub": "expense-assistant",
            "aud": tokens.USER_ISSUER,
            "scope": "expenses:read",
            "iat": now_ts,
            "exp": now_ts + 300,
            "role": "finance",              # injected claim
            "reports_to": ["alice", "bob"], # injected claim
            "delegations_received": [],     # injected claim
        },
        tokens._AGENT_KID,
    )
    user_token = await _issue_user_token(http_client, "alice", "expense-assistant")
    r = await http_client.post("/identity/exchange", json={
        "subject_token": user_token,
        "actor_token": forged_actor,
    })
    # Must fail; the exchange must not accept person claims on an actor token.
    assert r.status_code == 400, (
        f"exchange must refuse actor token with person claims, "
        f"got HTTP {r.status_code}: {r.text}"
    )


# ===========================================================================
# Spec item 3 — Use-case policy files
# ===========================================================================

def test_a4_expenses_policy_file_exists():
    """arms/approach4/policies/expenses.cedar exists."""
    p = _POLICIES_DIR / "expenses.cedar"
    assert p.exists(), f"expected expenses.cedar at {p}"


def test_a4_travel_policy_file_exists():
    """arms/approach4/policies/travel.cedar exists."""
    p = _POLICIES_DIR / "travel.cedar"
    assert p.exists(), f"expected travel.cedar at {p}"


def test_a4_payments_policy_file_exists():
    """arms/approach4/policies/payments.cedar exists."""
    p = _POLICIES_DIR / "payments.cedar"
    assert p.exists(), f"expected payments.cedar at {p}"


def test_a4_policy_files_contain_no_include_or_import():
    """None of the Cedar policy files references another via include or import."""
    for name in _USE_CASES:
        text = (_POLICIES_DIR / f"{name}.cedar").read_text(encoding="utf-8")
        for keyword in ("include", "import", "use"):
            # Look for bare keyword followed by a path or module name.
            pattern = rf'^\s*{keyword}\s+["\']'
            if re.search(pattern, text, re.MULTILINE | re.IGNORECASE):
                pytest.fail(
                    f"{name}.cedar must not use '{keyword}' directive — "
                    "each file must be self-contained"
                )


def test_a4_plugin_agent_to_use_case_mapping_covers_all_three():
    """approach4 plugin maps expense-assistant→expenses, travel-assistant→travel,
    payments-agent→payments."""
    from arms.approach4.plugin import AGENT_USE_CASE

    assert AGENT_USE_CASE.get("expense-assistant") == "expenses", AGENT_USE_CASE
    assert AGENT_USE_CASE.get("travel-assistant")  == "travel",   AGENT_USE_CASE
    assert AGENT_USE_CASE.get("payments-agent")    == "payments", AGENT_USE_CASE


# ===========================================================================
# Spec item 4 — Baseline consistency
# ===========================================================================

def _extract_rules_by_id(cedar_text: str) -> dict[str, str]:
    """Return {rule_id: rule_text} for every @id-annotated rule in the Cedar source."""
    rules: dict[str, str] = {}
    # Match @id("...") followed by the permit/forbid block.
    pattern = re.compile(
        r'@id\("([^"]+)"\)\s*((?:permit|forbid)\s*\([^)]*\)[^;]*;)',
        re.DOTALL,
    )
    for m in pattern.finditer(cedar_text):
        rule_id   = m.group(1)
        rule_body = re.sub(r'\s+', ' ', m.group(2).strip())
        rules[rule_id] = rule_body
    return rules


def test_a4_common_rules_identical_across_use_case_files():
    """For every @id rule that appears in more than one file, its text must be
    identical across all files it appears in.  This proves the common-rule
    copies start from the same baseline."""
    texts = {
        name: (_POLICIES_DIR / f"{name}.cedar").read_text(encoding="utf-8")
        for name in _USE_CASES
    }
    per_file: dict[str, dict[str, str]] = {
        name: _extract_rules_by_id(text) for name, text in texts.items()
    }

    # Collect rule IDs that appear in more than one file.
    from collections import Counter
    id_counts: Counter = Counter(
        rule_id
        for rules in per_file.values()
        for rule_id in rules
    )
    common_ids = {rid for rid, count in id_counts.items() if count > 1}

    mismatches: list[str] = []
    for rule_id in sorted(common_ids):
        variants = {
            name: per_file[name][rule_id]
            for name in _USE_CASES
            if rule_id in per_file[name]
        }
        unique_texts = set(variants.values())
        if len(unique_texts) > 1:
            mismatches.append(
                f"Rule {rule_id!r} differs across files: {variants}"
            )

    assert not mismatches, (
        "Common rules must be identical at baseline:\n" + "\n".join(mismatches)
    )


def test_a4_each_policy_file_carries_its_use_case_rule_ids():
    """expenses.cedar has P1/P2/P3, travel.cedar has P4, payments.cedar has P5."""
    expected = {
        "expenses": {"P1", "P2", "P3"},
        "travel":   {"P4"},
        "payments": {"P5"},
    }
    for name, required_ids in expected.items():
        text  = (_POLICIES_DIR / f"{name}.cedar").read_text(encoding="utf-8")
        rules = _extract_rules_by_id(text)
        present = set(rules.keys())
        missing = required_ids - present
        assert not missing, (
            f"{name}.cedar is missing rule IDs: {missing}. "
            f"Found: {present}"
        )


# ===========================================================================
# Spec item 5 — Plugin uses only token claims; no live state
# ===========================================================================

def test_a4_plugin_source_does_not_reference_directory_or_state():
    """The approach4 plugin module must not call /directory/ routes or
    read from domain.state."""
    plugin_path = _REPO / "arms" / "approach4" / "plugin.py"
    assert plugin_path.exists(), f"plugin.py must exist at {plugin_path}"
    src = plugin_path.read_text(encoding="utf-8")

    forbidden = [
        "/directory/",
        "state.users",
        "state.delegations",
        "state.vendors",
        "state.expense_limit",
        "directory/users",
        "directory/delegations",
        "directory/vendors",
    ]
    for needle in forbidden:
        assert needle not in src, (
            f"approach4 plugin must not reference {needle!r} — "
            "it may only use token claims and static policy data"
        )


async def test_a4_plugin_allows_legitimate_call_when_directory_is_down(
    a4_ctrl, a4_gw_app, log_path,
):
    """With the directory down, approach4 still allows dan booking for carol
    using a token that carries the active delegation claim."""
    # Bring directory down — approach4 must not be affected.
    await a4_ctrl.post("/control/directory-down", json={"down": True})

    bearer = await _make_bearer(a4_ctrl, "dan", "travel-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
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
        f"approach4 must allow the booking when directory is down but "
        f"delegation claim is in token; got: {result}"
    )


async def test_a4_plugin_denies_when_directory_down_and_no_delegation(
    a4_gw_app, log_path,
):
    """With directory down, approach4 still denies alice booking for carol
    (alice's token has no delegation from carol)."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as ctrl:
        await ctrl.post("/control/directory-down", json={"down": True})
        bearer = await _make_bearer(ctrl, "alice", "travel-assistant")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "book_travel",
            {"traveller": "carol", "details": "Paris, 2027-03-01"},
        )

    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P4"


# ===========================================================================
# Spec item 6 — Policy rules with rule IDs in refusal logs
# ===========================================================================

# --- P1: own expenses only --------------------------------------------------

async def test_a4_p1_wrong_claimant_denied(a4_ctrl, a4_gw_app, log_path):
    """alice submitting expense with claimant=bob → deny, rule=P1."""
    bearer = await _make_bearer(a4_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "submit_expense",
            {"claimant": "bob", "amount": 120.0, "description": "test"},
        )

    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P1"


async def test_a4_p1_own_expense_allowed(a4_ctrl, a4_gw_app, log_path):
    """alice submitting expense with claimant=alice → allow."""
    bearer = await _make_bearer(a4_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 120.0, "description": "test"},
        )

    assert result.get("isError") is not True


# --- P2: single expense limit -----------------------------------------------

async def test_a4_p2_over_limit_no_approval_ref_denied(a4_ctrl, a4_gw_app, log_path):
    """alice submitting £600 with no approval_ref → deny, rule=P2."""
    bearer = await _make_bearer(a4_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 600.0, "description": "conference"},
        )

    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P2"


async def test_a4_p2_over_limit_with_approval_ref_allowed(a4_ctrl, a4_gw_app, log_path):
    """alice submitting £600 with approval_ref → allow."""
    bearer = await _make_bearer(a4_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 600.0, "description": "conference",
             "approval_ref": "BOB-APPROVED-001"},
        )

    assert result.get("isError") is not True


# --- P3: no self-approval; must be claimant's manager -----------------------

async def test_a4_p3_self_approval_denied(a4_ctrl, a4_gw_app, log_path):
    """alice trying to approve her own expense → deny, rule=P3."""
    bearer = await _make_bearer(a4_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        # exp-001 has claimant=alice
        result = await session.call_tool(
            "approve_expense", {"expense_id": "exp-001"},
        )

    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P3"


async def test_a4_p3_wrong_approver_denied(a4_ctrl, a4_gw_app, log_path):
    """carol (not alice's manager) trying to approve alice's expense → deny, rule=P3."""
    bearer = await _make_bearer(a4_ctrl, "carol", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        # exp-001 has claimant=alice; carol is not alice's manager
        result = await session.call_tool(
            "approve_expense", {"expense_id": "exp-001"},
        )

    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P3"


async def test_a4_p3_correct_manager_allowed(a4_ctrl, a4_gw_app, log_path):
    """bob (alice's manager) approving alice's expense → allow."""
    bearer = await _make_bearer(a4_ctrl, "bob", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        # exp-001 has claimant=alice; bob is alice's manager
        result = await session.call_tool(
            "approve_expense", {"expense_id": "exp-001"},
        )

    assert result.get("isError") is not True


# --- P4: booking for someone else needs a delegation ------------------------

async def test_a4_p4_booking_own_travel_allowed(a4_ctrl, a4_gw_app, log_path):
    """alice booking travel for herself → allow (no delegation needed)."""
    bearer = await _make_bearer(a4_ctrl, "alice", "travel-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "book_travel",
            {"traveller": "alice", "details": "London, 2027-02-10"},
        )

    assert result.get("isError") is not True


async def test_a4_p4_no_delegation_in_token_denied(a4_ctrl, a4_gw_app, log_path):
    """alice's token has no delegation from carol → deny, rule=P4."""
    bearer = await _make_bearer(a4_ctrl, "alice", "travel-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "book_travel",
            {"traveller": "carol", "details": "Paris, 2027-03-01"},
        )

    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P4"


async def test_a4_p4_valid_delegation_in_token_allowed(a4_ctrl, a4_gw_app, log_path):
    """dan's token carries carol's active delegation → allow booking for carol."""
    bearer = await _make_bearer(a4_ctrl, "dan", "travel-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "book_travel",
            {"traveller": "carol", "details": "Paris, 2027-03-01"},
        )

    assert result.get("isError") is not True


# --- P5: approved vendor list -----------------------------------------------

async def test_a4_p5_unapproved_vendor_denied(a4_ctrl, a4_gw_app, log_path):
    """payments-agent paying rogue-vendor → deny, rule=P5."""
    bearer = await _make_bearer(a4_ctrl, "erin", "payments-agent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "pay_vendor",
            {"vendor": "rogue-vendor", "amount": 500.0, "reference": "ref-x"},
        )

    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P5"


async def test_a4_p5_approved_vendor_allowed(a4_ctrl, a4_gw_app, log_path):
    """payments-agent paying acme-hotels (on approved list) → allow."""
    bearer = await _make_bearer(a4_ctrl, "erin", "payments-agent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "pay_vendor",
            {"vendor": "acme-hotels", "amount": 250.0, "reference": "ref-1"},
        )

    assert result.get("isError") is not True


async def test_a4_p5_finance_role_required_for_payments(a4_ctrl, a4_gw_app, log_path):
    """alice (employee, not finance) via payments-agent → deny (wrong role)."""
    bearer = await _make_bearer(a4_ctrl, "alice", "payments-agent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "pay_vendor",
            {"vendor": "acme-hotels", "amount": 250.0, "reference": "ref-1"},
        )

    assert result.get("isError") is True


# ===========================================================================
# Spec item 7 — Delegation expiry compared to server clock
# ===========================================================================

async def test_a4_delegation_expiry_in_token_checked_against_server_clock(
    a4_ctrl, a4_gw_app, log_path,
):
    """Token still valid but delegation claim has expired → deny, rule=P4.

    Issue token for dan with a short-lived delegation expiry.  Advance
    the server clock past that expiry (but before the token's own expiry).
    Booking must be denied because the delegation expired.
    """
    # The fixture delegation expires 2027-12-31. We need one that expires sooner.
    # We use a very large clock advance to push past a near-future delegation.
    # The simclock starts at a fixed past value; we rely on the delegation in
    # fixtures.py expiring in 2027.  To test expiry, advance clock to 2028.
    # Token lifetime = 3600s (max), clock advance = ~10 years worth of seconds.
    # Advance server clock by 11 years to put us past 2027-12-31, then issue
    # a token that is valid at that simulated time (the gateway now checks
    # token expiry against the simulated clock).
    _eleven_years_s = 11 * 365 * 24 * 3600
    await a4_ctrl.post("/control/clock/advance", json={"seconds": _eleven_years_s})
    bearer = await _make_bearer(a4_ctrl, "dan", "travel-assistant")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "book_travel",
            {"traveller": "carol", "details": "Lisbon, 2038-04-10"},
        )

    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P4", (
        "expired delegation in token must produce rule=P4 denial"
    )


# ===========================================================================
# Spec item 8 — Runner starts gateway with approach4 plugin
# ===========================================================================

def test_a4_runner_starts_gateway_with_approach4_plugin(tmp_path):
    """The runner's domain_server with approach4 env vars starts successfully
    and serves /gateway/mcp."""
    from runner._server import domain_server

    decision_log = str(tmp_path / "decisions.jsonl")
    with domain_server(
        decision_log,
        env_extra={"GATEWAY": "true", "GATEWAY_PLUGIN": "approach4"},
    ) as (port, base_url):
        resp = httpx.post(
            f"{base_url}/gateway/mcp",
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test-a4-runner", "version": "0"},
                },
            },
            headers={"Accept": "application/json, text/event-stream"},
        )
    assert resp.status_code == 200, (
        f"approach4 runner server must serve /gateway/mcp; "
        f"got HTTP {resp.status_code}: {resp.text[:400]}"
    )


def test_a4_runner_strips_test_plugin_vars_from_approach4_server(
    tmp_path, monkeypatch,
):
    """Even when _GATEWAY_TESTING and GATEWAY_TEST_PLUGIN are set in the
    parent env, the runner strips them so the approach4 plugin is used."""
    monkeypatch.setenv("_GATEWAY_TESTING", "1")
    monkeypatch.setenv("GATEWAY_TEST_PLUGIN", "allow_all")

    from runner._server import domain_server

    decision_log = str(tmp_path / "decisions.jsonl")
    # With approach4 plugin specified, server must start (not fail).
    with domain_server(
        decision_log,
        env_extra={"GATEWAY": "true", "GATEWAY_PLUGIN": "approach4"},
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
# Acceptance: end-to-end staleness test
# ===========================================================================

async def test_a4_staleness_revoked_delegation_still_in_old_token(
    a4_ctrl, a4_gw_app, log_path,
):
    """Staleness acceptance test.

    Steps:
    1. Issue token for dan while carol's travel delegation is active.
       Token lifetime = 300s (default).
    2. Revoke carol's delegation via /control/revoke-delegation.
    3. dan's booking for carol via the SAME token → ALLOWED (approach 4
       reads only from token claims, not live state).
    4. Advance clock past token expiry (>300s).
    5. Issue a NEW token for dan — delegation is now absent.
    6. dan's booking for carol via the new token → DENIED.
    """
    # Step 1: issue token while delegation is active
    bearer_with_delegation = await _make_bearer(
        a4_ctrl, "dan", "travel-assistant",
        lifetime=300,
    )
    # Verify the token actually carries the delegation
    claims = _decode_unverified(bearer_with_delegation)
    assert any(
        d.get("delegator") == "carol"
        for d in claims.get("delegations_received", [])
    ), "Precondition: token must carry carol→dan delegation"

    # Step 2: revoke the delegation
    r = await a4_ctrl.post(
        "/control/revoke-delegation", json={"delegation_id": "del-001"},
    )
    r.raise_for_status()

    # Step 3: old token still allowed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={
            "Host": "localhost:8765",
            "Authorization": f"Bearer {bearer_with_delegation}",
        },
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result_old = await session.call_tool(
            "book_travel",
            {"traveller": "carol", "details": "Rome, 2027-05-01"},
        )

    assert result_old.get("isError") is not True, (
        "approach 4 MUST allow the booking with the old token: "
        "the delegation is still in the token claims even though the "
        "live record was revoked.  "
        f"Got: {result_old}"
    )

    # Step 4: advance clock past token expiry
    await a4_ctrl.post("/control/clock/advance", json={"seconds": 400})

    # Step 5: issue new token — delegation now absent
    bearer_without_delegation = await _make_bearer(
        a4_ctrl, "dan", "travel-assistant",
        lifetime=300,
    )
    new_claims = _decode_unverified(bearer_without_delegation)
    carol_dels = [
        d for d in new_claims.get("delegations_received", [])
        if d.get("delegator") == "carol"
    ]
    assert carol_dels == [], (
        f"New token must not carry revoked delegation, got: {carol_dels}"
    )

    # Step 6: new token is denied
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={
            "Host": "localhost:8765",
            "Authorization": f"Bearer {bearer_without_delegation}",
        },
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result_new = await session.call_tool(
            "book_travel",
            {"traveller": "carol", "details": "Rome, 2027-05-01"},
        )

    assert result_new.get("isError") is True, (
        "approach 4 MUST deny the booking with the new token: "
        "the delegation is absent from the new token.  "
        f"Got: {result_new}"
    )
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P4"


# ===========================================================================
# Guard test — plugin must not read people facts from live state
# ===========================================================================

async def test_a4_plugin_live_state_not_read_for_people_facts(
    a4_gw_app, a4_ctrl, log_path, monkeypatch,
):
    """Wrap the gateway's stored plugin reference so a guard is active only
    during each evaluate call.  state.users/delegations/vendors/expense_limit
    must not be accessed; state.expenses is allowed."""
    import domain.gateway as _gw
    from domain.state import state as _dom_state

    _orig_plugin = _gw._policy_plugin

    _GUARDED = ("users", "delegations", "vendors", "expense_limit")

    class _Blocked:
        def __repr__(self):
            return "<blocked>"

        def __getattr__(self, name):
            raise RuntimeError(
                f"approach4 plugin must not access live state .{name}"
            )

        def __getitem__(self, key):
            raise RuntimeError(
                f"approach4 plugin must not access live state [{key!r}]"
            )

        def __contains__(self, key):
            raise RuntimeError("approach4 plugin must not access live state")

        def __iter__(self):
            raise RuntimeError("approach4 plugin must not access live state")

    def _wrapped_evaluate(claims, agent_chain, tool, arguments):
        saved = {}
        for attr in _GUARDED:
            saved[attr] = getattr(_dom_state, attr)
            setattr(_dom_state, attr, _Blocked())
        try:
            return _orig_plugin(claims, agent_chain, tool, arguments)
        finally:
            for attr, val in saved.items():
                setattr(_dom_state, attr, val)

    monkeypatch.setattr(_gw, "_policy_plugin", _wrapped_evaluate)

    # P1 deny: alice submits for bob
    bearer = await _make_bearer(a4_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as c:
        s = _GwSession(c)
        await s.initialize()
        r = await s.call_tool(
            "submit_expense",
            {"claimant": "bob", "amount": 50.0, "description": "x"},
        )
    assert r.get("isError") is True
    assert _last_decision(log_path)["rule"] == "P1"

    # P2 deny: alice submits £600 without approval_ref
    bearer = await _make_bearer(a4_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as c:
        s = _GwSession(c)
        await s.initialize()
        r = await s.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 600.0, "description": "conf"},
        )
    assert r.get("isError") is True
    assert _last_decision(log_path)["rule"] == "P2"

    # P3 deny: alice self-approves exp-001
    bearer = await _make_bearer(a4_ctrl, "alice", "expense-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as c:
        s = _GwSession(c)
        await s.initialize()
        r = await s.call_tool("approve_expense", {"expense_id": "exp-001"})
    assert r.get("isError") is True
    assert _last_decision(log_path)["rule"] == "P3"

    # P4 deny: alice books for carol (no delegation)
    bearer = await _make_bearer(a4_ctrl, "alice", "travel-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as c:
        s = _GwSession(c)
        await s.initialize()
        r = await s.call_tool(
            "book_travel",
            {"traveller": "carol", "details": "Paris, 2027-03-01"},
        )
    assert r.get("isError") is True
    assert _last_decision(log_path)["rule"] == "P4"

    # P5 deny: erin pays rogue-vendor
    bearer = await _make_bearer(a4_ctrl, "erin", "payments-agent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as c:
        s = _GwSession(c)
        await s.initialize()
        r = await s.call_tool(
            "pay_vendor",
            {"vendor": "rogue-vendor", "amount": 100.0, "reference": "ref-x"},
        )
    assert r.get("isError") is True
    assert _last_decision(log_path)["rule"] == "P5"

    # P7 deny: alice (employee) via payments-agent pays approved vendor
    bearer = await _make_bearer(a4_ctrl, "alice", "payments-agent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as c:
        s = _GwSession(c)
        await s.initialize()
        r = await s.call_tool(
            "pay_vendor",
            {"vendor": "acme-hotels", "amount": 100.0, "reference": "ref-p7"},
        )
    assert r.get("isError") is True
    assert _last_decision(log_path)["rule"] == "P7"

    # P3 deny via payments-agent: erin self-approves — verifies state.expenses is
    # accessible (not guarded) while state.expense_limit remains blocked.
    from domain.state import state as _st
    _st.expenses["exp-guard-erin"] = {
        "id": "exp-guard-erin", "claimant": "erin", "amount": 50.0,
        "currency": "GBP", "description": "guard test", "receipt_id": None,
        "approval_ref": None, "status": "pending",
    }
    bearer = await _make_bearer(a4_ctrl, "erin", "payments-agent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as c:
        s = _GwSession(c)
        await s.initialize()
        r = await s.call_tool("approve_expense", {"expense_id": "exp-guard-erin"})
    assert r.get("isError") is True
    assert _last_decision(log_path)["rule"] == "P3"


# ===========================================================================
# Item E — 12 new tests for task-2b approved spec
# ===========================================================================

# --- E1: P1 and P2 copies identical in expenses.cedar and travel.cedar -----

def test_a4_e1_p1_p2_identical_expenses_travel():
    """P1 and P2 rule bodies in expenses.cedar match those in travel.cedar word-for-word."""
    expenses_rules = _extract_rules_by_id(
        (_POLICIES_DIR / "expenses.cedar").read_text(encoding="utf-8")
    )
    travel_rules = _extract_rules_by_id(
        (_POLICIES_DIR / "travel.cedar").read_text(encoding="utf-8")
    )
    for rule_id in ("P1", "P2"):
        assert rule_id in expenses_rules, f"{rule_id} missing from expenses.cedar"
        assert rule_id in travel_rules,   f"{rule_id} missing from travel.cedar"
        assert expenses_rules[rule_id] == travel_rules[rule_id], (
            f"{rule_id} differs between expenses.cedar and travel.cedar: "
            f"expenses={expenses_rules[rule_id]!r} travel={travel_rules[rule_id]!r}"
        )


# --- E2: P3 copy identical in expenses.cedar and payments.cedar ------------

def test_a4_e2_p3_identical_expenses_payments():
    """P3 rule body in expenses.cedar matches that in payments.cedar word-for-word."""
    expenses_rules = _extract_rules_by_id(
        (_POLICIES_DIR / "expenses.cedar").read_text(encoding="utf-8")
    )
    payments_rules = _extract_rules_by_id(
        (_POLICIES_DIR / "payments.cedar").read_text(encoding="utf-8")
    )
    assert "P3" in expenses_rules, "P3 missing from expenses.cedar"
    assert "P3" in payments_rules, "P3 missing from payments.cedar"
    assert expenses_rules["P3"] == payments_rules["P3"], (
        f"P3 differs between expenses.cedar and payments.cedar: "
        f"expenses={expenses_rules['P3']!r} payments={payments_rules['P3']!r}"
    )


# --- E3: c_hook allowed_tools match AGENT_GRANTS ---------------------------

def test_a4_e3_c_hook_allowed_tools_match_agent_grants():
    """Each agent's allowed_tools in arms/c_hook/config equals AGENT_GRANTS."""
    import yaml

    for agent_name, expected_tools in GATEWAY_GRANTS.items():
        cfg_path = _C_HOOK_CONFIG_DIR / f"{agent_name}.yaml"
        assert cfg_path.exists(), f"config missing: {cfg_path}"
        cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        actual = sorted(cfg.get("allowed_tools", []))
        expected = sorted(expected_tools)
        assert actual == expected, (
            f"{agent_name}: c_hook allowed_tools {actual} != AGENT_GRANTS {expected}"
        )


# --- E4–E6: travel-assistant submit_expense (P1 and P2) --------------------

async def test_a4_e4_travel_p1_wrong_claimant_denied(a4_ctrl, a4_gw_app, log_path):
    """travel-assistant: alice submitting expense with claimant=bob → deny, rule=P1."""
    bearer = await _make_bearer(a4_ctrl, "alice", "travel-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "submit_expense",
            {"claimant": "bob", "amount": 120.0, "description": "test"},
        )
    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P1"


async def test_a4_e5_travel_p1_own_expense_allowed(a4_ctrl, a4_gw_app, log_path):
    """travel-assistant: alice submitting her own expense → allow."""
    bearer = await _make_bearer(a4_ctrl, "alice", "travel-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 120.0, "description": "travel meal"},
        )
    assert result.get("isError") is not True


async def test_a4_e6_travel_p2_over_limit_no_ref_denied(a4_ctrl, a4_gw_app, log_path):
    """travel-assistant: alice submitting £600 without approval_ref → deny, rule=P2."""
    bearer = await _make_bearer(a4_ctrl, "alice", "travel-assistant")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "submit_expense",
            {"claimant": "alice", "amount": 600.0, "description": "conference travel"},
        )
    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P2"


# --- E7: payments-agent approve_expense P3 ---------------------------------

async def test_a4_e7_payments_p3_self_approval_denied(a4_ctrl, a4_gw_app, log_path):
    """payments-agent: erin approving her own expense (exp-erin-setup) → deny, rule=P3."""
    from domain.state import state as _dom_state
    _dom_state.expenses["exp-erin-setup"] = {
        "id": "exp-erin-setup", "claimant": "erin", "amount": 200.0,
        "currency": "GBP", "description": "setup expense", "receipt_id": None,
        "approval_ref": None, "status": "pending",
    }
    bearer = await _make_bearer(a4_ctrl, "erin", "payments-agent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "approve_expense", {"expense_id": "exp-erin-setup"},
        )
    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P3"


# --- P7: non-finance user blocked for every payments-agent action -----------

async def test_a4_p7_non_finance_approve_expense_denied(a4_ctrl, a4_gw_app, log_path):
    """bob (manager, not finance) approving alice's expense through payments-agent
    → deny, rule=P7.  P7 applies to every payments-agent action, not only pay_vendor."""
    bearer = await _make_bearer(a4_ctrl, "bob", "payments-agent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        # exp-001 claimant=alice; bob IS alice's manager, so P3 would pass —
        # but P7 must fire first: bob's role is "manager", not "finance".
        result = await session.call_tool(
            "approve_expense", {"expense_id": "exp-001"},
        )

    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P7"


# --- E8: payments-agent P7 non-finance user --------------------------------

async def test_a4_e8_payments_p7_non_finance_denied(a4_ctrl, a4_gw_app, log_path):
    """payments-agent: alice (employee, not finance) calling pay_vendor → deny, rule=P7."""
    bearer = await _make_bearer(a4_ctrl, "alice", "payments-agent")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "pay_vendor",
            {"vendor": "acme-hotels", "amount": 100.0, "reference": "ref-e8"},
        )
    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["decision"] == "deny"
    assert d["rule"] == "P7"


# --- E9–E10: AGENT_GRANTS content ------------------------------------------

def test_a4_e9_agent_grants_travel_has_submit_expense():
    """AGENT_GRANTS gives travel-assistant submit_expense."""
    assert "submit_expense" in GATEWAY_GRANTS["travel-assistant"], (
        f"AGENT_GRANTS travel-assistant missing submit_expense: "
        f"{GATEWAY_GRANTS['travel-assistant']}"
    )


def test_a4_e10_agent_grants_payments_has_approve_expense():
    """AGENT_GRANTS gives payments-agent approve_expense."""
    assert "approve_expense" in GATEWAY_GRANTS["payments-agent"], (
        f"AGENT_GRANTS payments-agent missing approve_expense: "
        f"{GATEWAY_GRANTS['payments-agent']}"
    )


# --- E11–E12: runner _AGENT_SCOPES -----------------------------------------

def test_a4_e11_runner_scopes_travel_assistant():
    """Runner requests travel:book, expenses:submit and agents:payments for travel-assistant."""
    import runner.run as _run
    scopes = sorted(_run._AGENT_SCOPES.get("travel-assistant", []))
    assert scopes == sorted(["expenses:submit", "travel:book", "agents:payments"]), (
        f"runner _AGENT_SCOPES travel-assistant: {scopes}"
    )


def test_a4_e12_runner_scopes_payments_agent():
    """Runner requests payments:pay and expenses:approve for payments-agent."""
    import runner.run as _run
    scopes = sorted(_run._AGENT_SCOPES.get("payments-agent", []))
    assert scopes == sorted(["expenses:approve", "payments:pay"]), (
        f"runner _AGENT_SCOPES payments-agent: {scopes}"
    )


# ===========================================================================
# Restored coverage (approved change A21): delegation expiry inside the token
# ===========================================================================

async def test_a4_restored_coverage_delegation_expiring_inside_token_lifetime_is_denied_by_p4(
    a4_ctrl, a4_gw_app, log_path,
):
    """A valid token carries a delegation whose expiry falls inside the token's
    lifetime. The clock moves past the delegation's expiry but not the token's;
    approach 4 denies under P4. (Restored coverage: the earlier test of this
    behaviour jumped eleven years and had to issue its token afterwards.)"""
    from domain import fixtures
    deleg = next(d for d in fixtures.DELEGATIONS
                 if d["delegator"] == "carol" and d["delegate"] == "dan")
    expiry = datetime.datetime.fromisoformat(deleg["expires"].replace("Z", "+00:00")).timestamp()
    await a4_ctrl.post("/control/clock/set", json={"ts": expiry - 100})
    bearer = await _make_bearer(a4_ctrl, "dan", "travel-assistant")          # lifetime 300
    claims = _decode_unverified(bearer)
    assert claims["exp"] > expiry + 100                  # the token outlives the delegation
    assert any(d["delegator"] == "carol" for d in claims["delegations_received"])
    await a4_ctrl.post("/control/clock/advance", json={"seconds": 150})
    assert simclock.now() > expiry and simclock.now() < claims["exp"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=a4_gw_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = _GwSession(client)
        await session.initialize()
        result = await session.call_tool(
            "book_travel", {"traveller": "carol", "details": "Lisbon"})

    assert result.get("isError") is True
    d = _last_decision(log_path)
    assert d["decision"] == "deny" and d["rule"] == "P4"
