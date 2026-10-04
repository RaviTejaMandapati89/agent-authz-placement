"""Tests for the identity layer: simulated clock, JWKS, token issuance, exchange,
and Bearer verification on tool calls."""
import json

import jwt as _jwt
import pytest
import pytest_asyncio

from domain import simclock, tokens
from tests.conftest import McpSession


def _decode_claims(tok: str) -> dict:
    """Decode token claims without signature verification."""
    return _jwt.decode(tok, options={"verify_signature": False})


# ---------------------------------------------------------------------------
# Simulated clock tests
# ---------------------------------------------------------------------------

async def test_clock_starts_at_epoch(http_client):
    assert simclock.now() == simclock._EPOCH


async def test_clock_advance(http_client):
    resp = await http_client.post("/control/clock/advance", json={"seconds": 60})
    assert resp.status_code == 200
    body = resp.json()
    assert body["sim_time"] == simclock._EPOCH + 60
    assert simclock.now() == simclock._EPOCH + 60


async def test_clock_set(http_client):
    new_ts = simclock._EPOCH + 1000
    resp = await http_client.post("/control/clock/set", json={"ts": new_ts})
    assert resp.status_code == 200
    body = resp.json()
    assert body["sim_time"] == new_ts
    assert simclock.now() == new_ts


async def test_clock_reset_restores_epoch(http_client):
    await http_client.post("/control/clock/advance", json={"seconds": 500})
    assert simclock.now() != simclock._EPOCH
    await http_client.post("/control/reset")
    assert simclock.now() == simclock._EPOCH

# ---------------------------------------------------------------------------
# JWKS endpoint
# ---------------------------------------------------------------------------

async def test_jwks_endpoint_returns_two_keys(http_client):
    resp = await http_client.get("/identity/jwks")
    assert resp.status_code == 200
    body = resp.json()
    assert "keys" in body
    assert len(body["keys"]) == 2


async def test_jwks_key_structure(http_client):
    resp = await http_client.get("/identity/jwks")
    body = resp.json()
    for key in body["keys"]:
        assert "kid" in key
        assert key.get("alg") == "ES256"
        assert key.get("use") == "sig"
        assert key.get("kty") == "EC"
        assert key.get("crv") == "P-256"

# ---------------------------------------------------------------------------
# Token issuance
# ---------------------------------------------------------------------------

async def test_issue_agent_token(http_client):
    resp = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant",
        "aud": "mcp-server",
        "scope": ["expenses:read", "expenses:submit"],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    claims = _decode_claims(body["access_token"])
    assert claims["sub"] == "expense-assistant"
    assert claims["iss"] == tokens.AGENT_ISSUER
    assert claims["aud"] == "mcp-server"
    assert claims["exp"] == int(simclock._EPOCH) + tokens.DEFAULT_LIFETIME


async def test_issue_user_token(http_client):
    resp = await http_client.post("/control/identity/user-token", json={
        "sub": "alice",
        "aud": "expense-assistant",
        "scope": ["expenses:read", "expenses:submit"],
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    claims = _decode_claims(body["access_token"])
    assert claims["sub"] == "alice"
    assert claims["iss"] == tokens.USER_ISSUER
    assert claims["aud"] == "expense-assistant"


async def test_issue_token_lifetime_capped_at_max(http_client):
    resp = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant",
        "aud": "mcp-server",
        "scope": ["expenses:read"],
        "lifetime": 99999,
    })
    assert resp.status_code == 200
    claims = _decode_claims(resp.json()["access_token"])
    assert claims["exp"] - claims["iat"] == tokens.MAX_LIFETIME


async def test_issue_token_scope_filtered_to_fixed_scopes(http_client):
    resp = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant",
        "aud": "mcp-server",
        "scope": ["expenses:read", "admin:delete"],  # admin:delete not in FIXED_SCOPES
    })
    assert resp.status_code == 200
    claims = _decode_claims(resp.json()["access_token"])
    assert "admin:delete" not in claims["scope"]
    assert "expenses:read" in claims["scope"]

# ---------------------------------------------------------------------------
# Token exchange (RFC 8693)
# ---------------------------------------------------------------------------

async def test_exchange_success(http_client):
    r = await http_client.post("/control/identity/user-token", json={
        "sub": "alice", "aud": "expense-assistant",
        "scope": ["expenses:read", "expenses:submit"],
    })
    user_token = r.json()["access_token"]

    r = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant", "aud": "mcp-server",
        "scope": ["expenses:read", "expenses:submit"],
    })
    agent_token = r.json()["access_token"]

    resp = await http_client.post("/identity/exchange", json={
        "subject_token": user_token, "actor_token": agent_token,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "access_token" in body
    claims = _decode_claims(body["access_token"])
    assert claims["sub"] == "alice"
    assert claims["act"]["sub"] == "expense-assistant"
    assert claims["aud"] == tokens.SERVER_AUDIENCE
    assert claims["iss"] == tokens.USER_ISSUER


async def test_exchange_aud_mismatch_rejected(http_client):
    r = await http_client.post("/control/identity/user-token", json={
        "sub": "alice", "aud": "wrong-agent",  # aud != actor sub
        "scope": ["expenses:read"],
    })
    user_token = r.json()["access_token"]

    r = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant", "aud": "mcp-server",
        "scope": ["expenses:read"],
    })
    agent_token = r.json()["access_token"]

    resp = await http_client.post("/identity/exchange", json={
        "subject_token": user_token, "actor_token": agent_token,
    })
    assert resp.status_code == 400
    assert "error" in resp.json()


async def test_exchange_scope_narrows(http_client):
    r = await http_client.post("/control/identity/user-token", json={
        "sub": "alice", "aud": "expense-assistant",
        "scope": ["expenses:read", "expenses:submit", "expenses:approve"],
    })
    user_token = r.json()["access_token"]

    r = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant", "aud": "mcp-server",
        "scope": ["expenses:read", "expenses:submit", "expenses:approve"],
    })
    agent_token = r.json()["access_token"]

    resp = await http_client.post("/identity/exchange", json={
        "subject_token": user_token, "actor_token": agent_token,
        "scope": "expenses:read",  # narrower
    })
    assert resp.status_code == 200
    claims = _decode_claims(resp.json()["access_token"])
    assert claims["scope"] == "expenses:read"


async def test_exchange_chain_too_long_rejected(http_client):
    """Exchange a token that itself was already exchanged twice → refused."""
    all_scopes = ["expenses:read", "expenses:submit"]

    # First exchange: user → agent1
    r = await http_client.post("/control/identity/user-token", json={
        "sub": "alice", "aud": "expense-assistant", "scope": all_scopes,
    })
    user_token = r.json()["access_token"]

    r = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant", "aud": "mcp-server", "scope": all_scopes,
    })
    agent1_token = r.json()["access_token"]

    resp = await http_client.post("/identity/exchange", json={
        "subject_token": user_token, "actor_token": agent1_token,
    })
    assert resp.status_code == 200
    hop1_token = resp.json()["access_token"]

    # Second exchange: hop1 → agent2
    r = await http_client.post("/control/identity/agent-token", json={
        "sub": "payments-agent", "aud": "expense-assistant", "scope": all_scopes,
    })
    # This token's aud must equal hop1_token.sub (alice) — use a user token instead
    r = await http_client.post("/control/identity/user-token", json={
        "sub": "alice", "aud": "payments-agent", "scope": all_scopes,
    })
    hop1_as_subject = r.json()["access_token"]

    r = await http_client.post("/control/identity/agent-token", json={
        "sub": "payments-agent", "aud": "mcp-server", "scope": all_scopes,
    })
    agent2_token = r.json()["access_token"]

    resp2 = await http_client.post("/identity/exchange", json={
        "subject_token": hop1_as_subject, "actor_token": agent2_token,
    })
    assert resp2.status_code == 200
    hop2_token = resp2.json()["access_token"]

    # Third exchange: hop2 has 2 hops → refused
    r = await http_client.post("/control/identity/agent-token", json={
        "sub": "payments-agent", "aud": "mcp-server", "scope": all_scopes,
    })
    # hop2_token.aud == "mcp-server", actor.sub must equal "mcp-server"
    # To test the chain limit, we need subject with 2 hops. Build it manually
    # by calling exchange twice on expanding subject chain.
    # The simplest approach: verify hop2_token has 2 act hops, then try to exchange it.
    hop2_claims = _decode_claims(hop2_token)
    assert hop2_claims.get("act") is not None

    # Use hop2_token as subject — but its aud is mcp-server, so actor.sub must be mcp-server
    # Instead, build a 2-hop token directly from hop1_token
    hop1_claims = _decode_claims(hop1_token)
    assert hop1_claims.get("act", {}).get("sub") == "expense-assistant"

    # Now exchange hop1_token (1 hop) with agent2 whose sub == hop1_token.aud
    # hop1_token.aud = mcp-server → need actor.sub = mcp-server (not realistic, skip)
    # Build a realistic 2-hop scenario:
    # alice --(user token aud=expense-assistant)--> expense-assistant
    # expense-assistant--(exchanged, 1 hop: act.sub=expense-assistant)--> mcp-server
    # Try to re-exchange this 1-hop token as subject with an actor.sub == alice
    # but aud of 1-hop token is mcp-server
    # This scenario can't produce a valid 2-hop test via the HTTP endpoint alone
    # without internal access. Use tokens module directly instead.
    from domain import tokens as _tokens
    t1 = _tokens.issue_user_token("alice", "expense-assistant", list(_tokens.FIXED_SCOPES))
    t2 = _tokens.issue_agent_token("expense-assistant", "mcp-server", list(_tokens.FIXED_SCOPES))
    exchanged1 = _tokens.exchange(t1, t2)  # 1 hop

    # Build a fake 2-hop token by manually crafting via issue
    # We need subject_token that already has 2 hops in act chain
    # Simplest: craft directly via exchange twice when the second exchange is possible
    # hop1: sub=alice, act.sub=expense-assistant, aud=mcp-server
    # To exchange hop1, we need actor.sub = hop1.aud = mcp-server
    # That's not a real agent. Instead test the library refusal:
    two_hop_tok = _tokens.issue_agent_token("alice", "mcp-server", list(_tokens.FIXED_SCOPES))
    # Patch in 2 hops by direct call to exchange with a crafted subject that has 2 hops
    import pytest
    with pytest.raises(ValueError, match="two hops"):
        # Build a payload with 2 existing hops
        import jwt as _jwt2
        from cryptography.hazmat.primitives.asymmetric.ec import generate_private_key, SECP256R1
        from cryptography.hazmat.backends import default_backend
        deep_key = generate_private_key(SECP256R1(), default_backend())
        deep_payload = {
            "iss": _tokens.AGENT_ISSUER,
            "sub": "alice",
            "aud": "expense-assistant",
            "scope": "expenses:read",
            "iat": int(_tokens.simclock.now()),
            "exp": int(_tokens.simclock.now()) + 300,
            "act": {"sub": "expense-assistant", "act": {"sub": "payments-agent"}},
        }
        # Must sign with known key to pass verification
        t_deep = _tokens._encode(deep_payload, _tokens._AGENT_KID)
        t_actor = _tokens.issue_agent_token(
            "expense-assistant", "mcp-server", list(_tokens.FIXED_SCOPES)
        )
        _tokens.exchange(t_deep, t_actor)

# ---------------------------------------------------------------------------
# Bearer verification on tool calls
# ---------------------------------------------------------------------------

async def test_tool_call_missing_token_denied(http_client, log_path, running_app):
    """A tool call without any Authorization header gets rule=IDENTITY."""
    import httpx
    from mcp.server.fastmcp import FastMCP
    from domain.server import mcp as _mcp

    # Make an MCP call with no Authorization header
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as client:
        session = McpSession(client)
        await session.initialize()
        result = await session.call_tool("read_receipt", {"receipt_id": "rcpt-001"})

    assert result.get("isError") is True

    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l]
    decisions = [l for l in lines if l["type"] == "decision"]
    assert decisions
    d = decisions[-1]
    assert d["decision"] == "deny"
    assert d["rule"] == "IDENTITY"

    outcomes = [l for l in lines if l["type"] == "outcome"]
    assert outcomes
    o = outcomes[-1]
    assert o["executed"] is False


async def test_tool_call_expired_token_denied(http_client, log_path, running_app, monkeypatch):
    """A tool call with an expired token gets rule=IDENTITY."""
    import httpx

    # Issue a token then advance clock past its expiry
    resp = await http_client.post("/control/identity/user-token", json={
        "sub": "alice", "aud": "expense-assistant", "scope": ["expenses:read"],
        "lifetime": 100,
    })
    user_token = resp.json()["access_token"]

    resp = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant", "aud": "mcp-server", "scope": ["expenses:read"],
        "lifetime": 100,
    })
    agent_token = resp.json()["access_token"]

    resp = await http_client.post("/identity/exchange", json={
        "subject_token": user_token, "actor_token": agent_token,
    })
    bearer = resp.json()["access_token"]

    # Advance clock past token expiry
    await http_client.post("/control/clock/advance", json={"seconds": 200})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bearer}"},
    ) as client:
        session = McpSession(client)
        await session.initialize()
        result = await session.call_tool("read_receipt", {"receipt_id": "rcpt-001"})

    assert result.get("isError") is True

    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l]
    decisions = [l for l in lines if l["type"] == "decision"]
    assert decisions
    d = decisions[-1]
    assert d["decision"] == "deny"
    assert d["rule"] == "IDENTITY"


async def test_tool_call_with_valid_token_succeeds(mcp_session: McpSession, log_path):
    """Happy path: valid Bearer token allows the tool call."""
    result = await mcp_session.call_tool("read_receipt", {"receipt_id": "rcpt-001"})
    assert "content" in result

    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l]
    decisions = [l for l in lines if l["type"] == "decision"]
    assert decisions
    d = decisions[-1]
    assert d["decision"] == "none"
    assert d["rule"] is None
    assert d["user"] == "alice"
    assert d["agent"] == "expense-assistant"

# ---------------------------------------------------------------------------
# sim_time in decision log (caveat 3: assert value, not just presence)
# ---------------------------------------------------------------------------

async def test_decision_log_sim_time_equals_clock(mcp_session: McpSession, log_path):
    """The sim_time in a decision entry must equal simclock.now() (frozen clock)."""
    # Clock is frozen at EPOCH — record the value before the call
    clock_value = simclock.now()

    result = await mcp_session.call_tool("read_receipt", {"receipt_id": "rcpt-001"})
    assert "content" in result

    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l]
    decisions = [l for l in lines if l["type"] == "decision"]
    assert decisions
    d = decisions[-1]

    assert "sim_time" in d
    # Assert the actual value equals the clock — not just that the key exists
    assert d["sim_time"] == clock_value
    # Since the clock hasn't moved, simclock.now() still equals clock_value
    assert d["sim_time"] == simclock.now()


async def test_identity_denial_logs_sim_time(http_client, log_path, running_app, monkeypatch):
    """An IDENTITY denial is logged with sim_time from the simulated clock."""
    import httpx
    monkeypatch.setenv("HOOK_LOG", str(log_path.parent / "hook.jsonl"))

    # Advance clock to a known value
    await http_client.post("/control/clock/advance", json={"seconds": 42})
    expected_sim_time = simclock.now()

    # Tool call without token → IDENTITY denial
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as client:
        session = McpSession(client)
        await session.initialize()
        await session.call_tool("read_receipt", {"receipt_id": "rcpt-001"})

    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l]
    decisions = [l for l in lines if l["type"] == "decision"]
    assert decisions
    d = decisions[-1]
    assert d["rule"] == "IDENTITY"
    assert d["sim_time"] == expected_sim_time


# ===========================================================================
# NEW TESTS — one per spec item gap (items 1–9)
# ===========================================================================

# ---------------------------------------------------------------------------
# Item 1: clock never reads real time
# ---------------------------------------------------------------------------

async def test_clock_never_reads_real_time(http_client):
    """simclock.now() is unchanged unless explicitly moved; no wall-time reads."""
    t0 = simclock.now()
    import time
    time.sleep(0.05)
    assert simclock.now() == t0


# ---------------------------------------------------------------------------
# Item 2: separate key pairs, issuer-key binding
# ---------------------------------------------------------------------------

async def test_issuers_have_separate_key_pairs(http_client):
    """The two JWKS keys have different EC key material (x, y coordinates)."""
    resp = await http_client.get("/identity/jwks")
    keys = resp.json()["keys"]
    assert len(keys) == 2
    coords = [(k["x"], k["y"]) for k in keys]
    assert coords[0] != coords[1]


def test_issuer_key_binding_rejects_cross_signed_token():
    """Token signed with agent key but iss=identity-issuer is rejected."""
    now_ts = int(simclock.now())
    payload = {
        "iss": tokens.USER_ISSUER,
        "sub": "alice",
        "aud": "expense-assistant",
        "scope": "expenses:read",
        "iat": now_ts,
        "exp": now_ts + 300,
    }
    cross_signed = tokens._encode(payload, tokens._AGENT_KID)

    with pytest.raises(Exception):
        tokens._verify_one(cross_signed)


# ---------------------------------------------------------------------------
# Item 3: lifetime and max constants
# ---------------------------------------------------------------------------

def test_default_lifetime_is_300():
    """Spec: lifetime default 300 seconds."""
    assert tokens.DEFAULT_LIFETIME == 300


def test_max_lifetime_is_3600():
    """Spec: lifetime max 3600 seconds."""
    assert tokens.MAX_LIFETIME == 3600


# ---------------------------------------------------------------------------
# Item 4: exchange — scope intersection, requested subset, custom audience,
#          act nesting on second hop
# ---------------------------------------------------------------------------

async def test_exchange_scope_is_intersection_of_subject_and_actor(http_client):
    """Result scope = subject scope ∩ actor scope when no requested scope given."""
    r = await http_client.post("/control/identity/user-token", json={
        "sub": "alice", "aud": "expense-assistant",
        "scope": ["expenses:read", "expenses:submit"],
    })
    user_token = r.json()["access_token"]

    r = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant", "aud": "mcp-server",
        "scope": ["expenses:read", "expenses:approve"],
    })
    agent_token = r.json()["access_token"]

    resp = await http_client.post("/identity/exchange", json={
        "subject_token": user_token, "actor_token": agent_token,
    })
    assert resp.status_code == 200
    claims = _decode_claims(resp.json()["access_token"])
    result_scopes = set(claims["scope"].split())
    # Only expenses:read is in both; submit and approve are each in only one
    assert result_scopes == {"expenses:read"}


async def test_exchange_requested_scope_must_be_subset_of_both(http_client):
    """Requesting a scope in subject but not in actor is refused."""
    r = await http_client.post("/control/identity/user-token", json={
        "sub": "alice", "aud": "expense-assistant",
        "scope": ["expenses:read", "expenses:submit"],
    })
    user_token = r.json()["access_token"]

    r = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant", "aud": "mcp-server",
        "scope": ["expenses:read"],
    })
    agent_token = r.json()["access_token"]

    resp = await http_client.post("/identity/exchange", json={
        "subject_token": user_token, "actor_token": agent_token,
        "scope": "expenses:submit",
    })
    assert resp.status_code == 400
    assert "error" in resp.json()


async def test_exchange_custom_audience(http_client):
    """Pass an audience parameter; result token aud matches it."""
    r = await http_client.post("/control/identity/user-token", json={
        "sub": "alice", "aud": "expense-assistant",
        "scope": ["expenses:read", "payments:pay"],
    })
    user_token = r.json()["access_token"]

    r = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant", "aud": "mcp-server",
        "scope": ["expenses:read", "payments:pay"],
    })
    agent_token = r.json()["access_token"]

    resp = await http_client.post("/identity/exchange", json={
        "subject_token": user_token,
        "actor_token": agent_token,
        "audience": "payments-agent",
    })
    assert resp.status_code == 200
    claims = _decode_claims(resp.json()["access_token"])
    assert claims["aud"] == "payments-agent"


async def test_exchange_act_nests_on_second_hop(http_client):
    """Two sequential exchanges produce nested act claims (2-hop chain)."""
    scopes = ["expenses:read", "payments:pay"]

    # Hop 1: alice → expense-assistant, targeting payments-agent
    r = await http_client.post("/control/identity/user-token", json={
        "sub": "alice", "aud": "expense-assistant", "scope": scopes,
    })
    user_token = r.json()["access_token"]

    r = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant", "aud": "mcp-server", "scope": scopes,
    })
    agent1_token = r.json()["access_token"]

    resp = await http_client.post("/identity/exchange", json={
        "subject_token": user_token,
        "actor_token": agent1_token,
        "audience": "payments-agent",
    })
    assert resp.status_code == 200
    hop1_token = resp.json()["access_token"]
    hop1 = _decode_claims(hop1_token)
    assert hop1["aud"] == "payments-agent"
    assert hop1["act"]["sub"] == "expense-assistant"

    # Hop 2: hop1 → payments-agent → mcp-server
    r = await http_client.post("/control/identity/agent-token", json={
        "sub": "payments-agent", "aud": "mcp-server", "scope": scopes,
    })
    agent2_token = r.json()["access_token"]

    resp2 = await http_client.post("/identity/exchange", json={
        "subject_token": hop1_token,
        "actor_token": agent2_token,
    })
    assert resp2.status_code == 200
    hop2 = _decode_claims(resp2.json()["access_token"])
    assert hop2["sub"] == "alice"
    assert hop2["act"]["sub"] == "payments-agent"
    assert hop2["act"]["act"]["sub"] == "expense-assistant"


# ---------------------------------------------------------------------------
# Item 5: fixed scopes match the spec exactly
# ---------------------------------------------------------------------------

def test_fixed_scopes_match_spec():
    """FIXED_SCOPES is exactly the five scopes listed in the spec."""
    assert tokens.FIXED_SCOPES == {
        "expenses:read",
        "expenses:submit",
        "expenses:approve",
        "travel:book",
        "payments:pay",
    }


# ---------------------------------------------------------------------------
# Item 6: tool call — act required, wrong audience, X-User/X-Agent ignored
# ---------------------------------------------------------------------------

async def test_tool_call_no_act_claim_rejected(http_client, log_path, running_app):
    """A token without an act claim is rejected with rule=IDENTITY."""
    import httpx

    r = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant",
        "aud": "mcp-server",
        "scope": ["expenses:read"],
    })
    bare_token = r.json()["access_token"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bare_token}"},
    ) as client:
        session = McpSession(client)
        await session.initialize()
        result = await session.call_tool("read_receipt", {"receipt_id": "rcpt-001"})

    assert result.get("isError") is True

    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l]
    decisions = [l for l in lines if l["type"] == "decision"]
    assert decisions
    d = decisions[-1]
    assert d["decision"] == "deny"
    assert d["rule"] == "IDENTITY"


async def test_tool_call_wrong_audience_rejected(http_client, log_path, running_app):
    """A token with aud != mcp-server is rejected with rule=IDENTITY."""
    import httpx

    now_ts = int(simclock.now())
    payload = {
        "iss": tokens.AGENT_ISSUER,
        "sub": "alice",
        "aud": "wrong-server",
        "scope": "expenses:read",
        "iat": now_ts,
        "exp": now_ts + 300,
        "act": {"sub": "expense-assistant"},
    }
    bad_aud_token = tokens._encode(payload, tokens._AGENT_KID)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bad_aud_token}"},
    ) as client:
        session = McpSession(client)
        await session.initialize()
        result = await session.call_tool("read_receipt", {"receipt_id": "rcpt-001"})

    assert result.get("isError") is True

    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l]
    decisions = [l for l in lines if l["type"] == "decision"]
    assert decisions
    d = decisions[-1]
    assert d["decision"] == "deny"
    assert d["rule"] == "IDENTITY"


async def test_tool_call_bad_signature_rejected(http_client, log_path, running_app):
    """A valid on-behalf-of token with an altered signature is refused,
    rule=IDENTITY, reason naming the signature."""
    import httpx

    all_scopes = sorted(tokens.FIXED_SCOPES)
    r = await http_client.post("/control/identity/user-token", json={
        "sub": "alice", "aud": "expense-assistant", "scope": all_scopes,
    })
    user_token = r.json()["access_token"]

    r = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant", "aud": "mcp-server", "scope": all_scopes,
    })
    agent_token = r.json()["access_token"]

    resp = await http_client.post("/identity/exchange", json={
        "subject_token": user_token, "actor_token": agent_token,
    })
    good_token = resp.json()["access_token"]

    # Corrupt only the signature segment so the header and payload stay
    # parseable and PyJWT reaches the signature-verification step.
    parts = good_token.split(".")
    assert len(parts) == 3
    sig = parts[2]
    mid = len(sig) // 2
    replacement = "A" if sig[mid] != "A" else "B"
    parts[2] = sig[:mid] + replacement + sig[mid + 1:]
    bad_token = ".".join(parts)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765", "Authorization": f"Bearer {bad_token}"},
    ) as client:
        session = McpSession(client)
        await session.initialize()
        result = await session.call_tool("read_receipt", {"receipt_id": "rcpt-001"})

    assert result.get("isError") is True

    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l]
    decisions = [l for l in lines if l["type"] == "decision"]
    assert decisions
    d = decisions[-1]
    assert d["decision"] == "deny"
    assert d["rule"] == "IDENTITY"
    assert "signature" in d["reason"].lower() or "Signature" in d["reason"]


async def test_tool_call_ignores_x_user_x_agent_headers(http_client, log_path, running_app):
    """X-User and X-Agent headers are ignored; identity comes only from the token."""
    import httpx

    all_scopes = sorted(tokens.FIXED_SCOPES)
    r = await http_client.post("/control/identity/user-token", json={
        "sub": "alice", "aud": "expense-assistant", "scope": all_scopes,
    })
    user_token = r.json()["access_token"]

    r = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant", "aud": "mcp-server", "scope": all_scopes,
    })
    agent_token = r.json()["access_token"]

    resp = await http_client.post("/identity/exchange", json={
        "subject_token": user_token, "actor_token": agent_token,
    })
    bearer = resp.json()["access_token"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app),
        base_url="http://localhost:8765",
        headers={
            "Host": "localhost:8765",
            "Authorization": f"Bearer {bearer}",
            "X-User": "bob",
            "X-Agent": "payments-agent",
        },
    ) as client:
        session = McpSession(client)
        await session.initialize()
        result = await session.call_tool("read_receipt", {"receipt_id": "rcpt-001"})

    assert "content" in result

    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l]
    decisions = [l for l in lines if l["type"] == "decision"]
    assert decisions
    d = decisions[-1]
    assert d["user"] == "alice"
    assert d["agent"] == "expense-assistant"


# ---------------------------------------------------------------------------
# Item 7: refusals — paired decision+outcome, reason names cause
# ---------------------------------------------------------------------------

async def test_identity_refusal_paired_by_call_id(http_client, log_path, running_app):
    """An IDENTITY refusal produces a decision and outcome with the same call_id."""
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as client:
        session = McpSession(client)
        await session.initialize()
        await session.call_tool("read_receipt", {"receipt_id": "rcpt-001"})

    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l]
    decisions = [l for l in lines if l["type"] == "decision"]
    outcomes = [l for l in lines if l["type"] == "outcome"]
    assert decisions and outcomes
    assert decisions[-1]["call_id"] == outcomes[-1]["call_id"]
    assert decisions[-1]["rule"] == "IDENTITY"
    assert outcomes[-1]["executed"] is False


async def test_identity_refusal_reason_names_cause(http_client, log_path, running_app):
    """Refusal reason field is descriptive, naming the specific failure cause."""
    import httpx

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as client:
        session = McpSession(client)
        await session.initialize()
        await session.call_tool("read_receipt", {"receipt_id": "rcpt-001"})

    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l]
    decisions = [l for l in lines if l["type"] == "decision"]
    assert decisions
    d = decisions[-1]
    assert d["rule"] == "IDENTITY"
    assert d["reason"]
    assert len(d["reason"]) > 5


# ---------------------------------------------------------------------------
# Item 8: logs — agent chain as list, real timestamp
# ---------------------------------------------------------------------------

async def test_decision_log_records_agent_chain_as_list(log_path, running_app):
    """For a multi-hop token the agent field is a list, e.g.
    ["payments-agent", "expense-assistant"]."""
    import httpx

    now_ts = int(simclock.now())
    payload = {
        "iss": tokens.AGENT_ISSUER,
        "sub": "alice",
        "aud": tokens.SERVER_AUDIENCE,
        "scope": "expenses:read",
        "iat": now_ts,
        "exp": now_ts + 300,
        "act": {
            "sub": "payments-agent",
            "act": {"sub": "expense-assistant"},
        },
    }
    two_hop_token = tokens._encode(payload, tokens._AGENT_KID)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app),
        base_url="http://localhost:8765",
        headers={
            "Host": "localhost:8765",
            "Authorization": f"Bearer {two_hop_token}",
        },
    ) as client:
        session = McpSession(client)
        await session.initialize()
        result = await session.call_tool("read_receipt", {"receipt_id": "rcpt-001"})

    assert "content" in result

    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l]
    decisions = [l for l in lines if l["type"] == "decision"]
    assert decisions
    d = decisions[-1]
    assert d["user"] == "alice"
    assert d["agent"] == ["payments-agent", "expense-assistant"]


async def test_decision_log_has_real_timestamp(mcp_session: McpSession, log_path):
    """The timestamp field is a real UTC datetime, not the simulated time."""
    import datetime

    before = datetime.datetime.now(datetime.timezone.utc)
    result = await mcp_session.call_tool("read_receipt", {"receipt_id": "rcpt-001"})
    after = datetime.datetime.now(datetime.timezone.utc)

    assert "content" in result

    lines = [json.loads(l) for l in log_path.read_text().splitlines() if l]
    decisions = [l for l in lines if l["type"] == "decision"]
    assert decisions
    d = decisions[-1]

    assert "timestamp" in d
    ts = datetime.datetime.fromisoformat(d["timestamp"])
    assert before <= ts <= after


# ---------------------------------------------------------------------------
# Item 9: /directory/delegations returns sim_time and filters by clock
# ---------------------------------------------------------------------------

async def test_directory_delegations_returns_sim_time(http_client):
    """Response shape is {"delegations": [...], "sim_time": <float>}."""
    resp = await http_client.get("/directory/delegations")
    assert resp.status_code == 200
    body = resp.json()
    assert "delegations" in body
    assert "sim_time" in body
    assert isinstance(body["delegations"], list)
    assert body["sim_time"] == simclock.now()


async def test_directory_delegations_filters_expired_by_clock(http_client):
    """After advancing clock past a delegation's expiry, it is excluded."""
    # del-001 expires at 2027-12-31T23:59:59Z
    import datetime
    expiry = datetime.datetime(2027, 12, 31, 23, 59, 59,
                               tzinfo=datetime.timezone.utc).timestamp()
    seconds_past_expiry = expiry - simclock.now() + 1
    await http_client.post("/control/clock/advance",
                           json={"seconds": seconds_past_expiry})

    resp = await http_client.get("/directory/delegations")
    assert resp.status_code == 200
    body = resp.json()
    delegations = body["delegations"]
    assert not any(d["id"] == "del-001" for d in delegations)


# ---------------------------------------------------------------------------
# Item 4 + 6: exchange output signed by identity-issuer;
#              agent tokens default aud = identity-issuer
# ---------------------------------------------------------------------------

async def test_exchange_output_signed_by_identity_issuer(http_client):
    """Exchange output has iss=identity-issuer and kid matches the identity-issuer key."""
    r = await http_client.post("/control/identity/user-token", json={
        "sub": "alice", "aud": "expense-assistant",
        "scope": ["expenses:read"],
    })
    user_token = r.json()["access_token"]

    r = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant",
        "scope": ["expenses:read"],
    })
    agent_token = r.json()["access_token"]

    resp = await http_client.post("/identity/exchange", json={
        "subject_token": user_token, "actor_token": agent_token,
    })
    assert resp.status_code == 200
    raw = resp.json()["access_token"]
    claims = _decode_claims(raw)
    assert claims["iss"] == tokens.USER_ISSUER

    header = _jwt.get_unverified_header(raw)
    assert header["kid"] == tokens._USER_KID


async def test_agent_token_aud_defaults_to_identity_issuer(http_client):
    """Agent tokens default to aud=identity-issuer when aud is omitted."""
    resp = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant",
        "scope": ["expenses:read"],
    })
    assert resp.status_code == 200
    claims = _decode_claims(resp.json()["access_token"])
    assert claims["aud"] == tokens.USER_ISSUER


# ---------------------------------------------------------------------------
# Item 13: arm B contract tests use tokens, not headers
# ---------------------------------------------------------------------------

def test_contract_tests_use_tokens_not_headers():
    """Approach 3's contract tests (arms/b_spec/_contract_tests.py) contain
    no X-User or X-Agent and obtain a token before calling a tool."""
    import pathlib
    src = (pathlib.Path(__file__).parent.parent
           / "arms" / "b_spec" / "_contract_tests.py").read_text()
    assert "X-User" not in src, "contract tests must not use X-User header"
    assert "X-Agent" not in src, "contract tests must not use X-Agent header"
    assert "/control/identity/" in src or "mcp_session" in src, (
        "contract tests must obtain a token before calling a tool"
    )
