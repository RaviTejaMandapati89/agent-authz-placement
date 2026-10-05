"""
Tests for multi-hop step 5b: A2A between agents.

ALL tests in this file FAIL against the current code. Failures are caused by:
  – agents:payments absent from tokens.FIXED_SCOPES
  – ask_payments_agent absent from domain/scopes.py TOOL_SCOPE_MAP
  – ask_payments_agent absent from domain/grants.py AGENT_GRANTS
  – /a2a/payments-agent endpoint not registered in domain/server.py
  – /gateway/a2a/payments-agent endpoint not registered in domain/gateway.py
  – domain.gateway.handle_a2a not implemented
  – no delegation-token exchange chain for the A2A path
  – decision log does not record agent chain at the A2A hop
  – /control/set_model_turns not implemented
  – /control/last_transcript not implemented
  – kit_snapshot/ missing domain files, policy.md, spec_input.md, _contract_tests.py

Design for acceptance tests
────────────────────────────
Acceptance tests drive the full two-agent pipeline with ScriptedModel so
Bedrock is never called and the result is deterministic.

_run_first_agent() is the central helper. It calls the arm's own run()
function with model=ScriptedModel(...) as the ONLY injection; MCPClient
connects to a real TCP server subprocess started via the runner's
domain_server. No test helper re-implements the tool or re-wires the agent.

Every end-to-end test enforces a 60-second asyncio.wait_for timeout so that
a deadlock fails the test rather than hanging indefinitely.

Event-loop safety (payments-agent A2A handler)
───────────────────────────────────────────────
payments-agent's A2A handler is an async Starlette route. For approaches 1-3
it runs the Strands agent in asyncio.to_thread(); the hook's sync HTTP calls
run in the worker thread while the event loop stays free to handle the
tool-call requests that the thread re-schedules via
asyncio.run_coroutine_threadsafe. For approaches 4-6 the agent calls
/gateway/mcp via httpx.AsyncClient with no thread, so the event loop is
never blocked.
"""

import asyncio
import contextlib
import importlib
import inspect
import json
import os
import pathlib
import re
import uuid
from collections.abc import AsyncGenerator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from domain import simclock, tokens
from runner.run import _AGENT_SCOPES

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

_REPO = pathlib.Path(__file__).parent.parent
_SERVER_PY = _REPO / "domain" / "server.py"
_GATEWAY_PY = _REPO / "domain" / "gateway.py"
_GRANTS_PY = _REPO / "domain" / "grants.py"
_SCOPES_PY = _REPO / "domain" / "scopes.py"

_A2A_PATH = "/a2a/payments-agent"
_GW_A2A_PATH = "/gateway/a2a/payments-agent"
_PAYMENTS_AGENT_AUD = "payments-agent"

# Approaches: (name, has_gateway)
# Approach 1 = arm A (prompt rules), 2 = arm C (hook), 3 = approach3 gen-1
# Approaches 4-6 use gateway plugins.
_NON_GW_APPROACHES = ["1", "2", "3"]
_GW_APPROACHES = ["4", "5", "6"]


# ---------------------------------------------------------------------------
# ScriptedModel: deterministic Strands-compatible model stub
#
# Each invocation consumes the next "turn" from the pre-defined list.
# A turn is either {"tool": name, "input": {…}} or {"text": "…"}.
# Implements strands.models.Model.stream() so it can be passed to
# strands.Agent(..., model=ScriptedModel([...])).
# ---------------------------------------------------------------------------

class ScriptedModel:
    """
    Deterministic Strands-compatible model stub for acceptance tests.

    Usage:
        model = ScriptedModel([
            {"tool": "ask_payments_agent", "input": {"request": "pay vendor..."}},
            {"text": "Payment was refused."},
        ])
        agent = Agent(model=model, tools=..., system_prompt=...)
        result = await agent("please pay vendor acme-hotels")
    """

    def __init__(self, turns: list[dict], *, label: str = "scripted") -> None:
        self._turns = list(turns)
        self._idx = 0
        self._label = label

    # ---- strands.models.Model interface ----

    def get_config(self) -> dict:
        return {}

    def update_config(self, **kwargs: Any) -> None:
        pass

    @property
    def context_window_limit(self) -> int:
        return 200_000

    def count_tokens(
        self, messages: list, tool_specs: list | None = None,
        system_prompt: str | None = None, **kwargs: Any,
    ) -> int:
        return len(str(messages)) // 4

    def estimate_utilization(self, input_tokens: int) -> float:
        return 0.0

    async def stream(
        self,
        messages: list,
        tool_specs: list | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict, None]:
        """Yield Strands streaming events for the next scripted turn."""
        turn = (
            self._turns[self._idx]
            if self._idx < len(self._turns)
            else {"text": "Done."}
        )
        self._idx += 1

        yield {"messageStart": {"role": "assistant"}}

        if "tool" in turn:
            tool_id = f"tooluse_{uuid.uuid4().hex[:8]}"
            yield {
                "contentBlockStart": {
                    "contentBlockIndex": 0,
                    "start": {
                        "toolUse": {"name": turn["tool"], "toolUseId": tool_id},
                    },
                },
            }
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {
                        "toolUse": {
                            "input": json.dumps(turn.get("input", {})),
                            "toolUseId": tool_id,
                            "name": turn["tool"],
                        },
                    },
                },
            }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {"text": ""}}}
            yield {
                "contentBlockDelta": {
                    "contentBlockIndex": 0,
                    "delta": {"text": turn.get("text", "Done.")},
                },
            }
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}

        yield {
            "metadata": {
                "usage": {
                    "inputTokens": 5, "outputTokens": 5, "totalTokens": 10,
                    "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0,
                },
                "metrics": {"latencyMs": 1},
            },
        }

    # stream must be an async generator
    stream.__wrapped__ = True


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _read_log(log_path: pathlib.Path) -> list[dict]:
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text().splitlines()
        if line.strip()
    ]


def _decisions(log_path: pathlib.Path) -> list[dict]:
    return [e for e in _read_log(log_path) if "decision" in e and "tool" in e]


def _hook_decisions(hook_log_path: pathlib.Path) -> list[dict]:
    """Read deny decisions from the PolicyHook log (HOOK_LOG)."""
    return _decisions(hook_log_path)


def _gw_source_extract_between(src: str, begin: str, end: str) -> str:
    s = src.find(begin)
    e = src.rfind(end)
    if s == -1 or e == -1:
        return ""
    return src[s : e + len(end)]


async def _issue_delegation_token(
    ctrl: httpx.AsyncClient,
    *,
    user: str,
    calling_agent: str,
    scope: list[str],
) -> str:
    """Issue a delegation token: sub=user, aud=payments-agent, act={sub: calling-agent}."""
    r = await ctrl.post("/control/identity/user-token", json={
        "sub": user, "aud": calling_agent, "scope": scope,
    })
    r.raise_for_status()
    user_tok = r.json()["access_token"]

    r = await ctrl.post("/control/identity/agent-token", json={
        "sub": calling_agent, "scope": scope,
    })
    r.raise_for_status()
    agent_tok = r.json()["access_token"]

    r = await ctrl.post("/identity/exchange", json={
        "subject_token": user_tok,
        "actor_token": agent_tok,
        "audience": _PAYMENTS_AGENT_AUD,
    })
    r.raise_for_status()
    return r.json()["access_token"]


async def _issue_tool_server_token(
    ctrl: httpx.AsyncClient,
    *,
    delegation_token: str,
    payments_agent_scope: list[str] | None = None,
) -> str:
    """Exchange delegation token for a tool-server token via payments-agent."""
    if payments_agent_scope is None:
        payments_agent_scope = list(_AGENT_SCOPES["payments-agent"])

    r = await ctrl.post("/control/identity/agent-token", json={
        "sub": "payments-agent", "scope": payments_agent_scope,
    })
    r.raise_for_status()
    pa_tok = r.json()["access_token"]

    r = await ctrl.post("/identity/exchange", json={
        "subject_token": delegation_token,
        "actor_token": pa_tok,
        "audience": tokens.SERVER_AUDIENCE,
    })
    r.raise_for_status()
    return r.json()["access_token"]


def _a2a_send_request(
    message_text: str,
    *,
    context_id: str | None = None,
) -> dict:
    """Build a minimal A2A message/send JSON-RPC request."""
    return {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "user",
                "parts": [{"kind": "text", "text": message_text}],
                **({"contextId": context_id} if context_id else {}),
            },
        },
    }


@contextlib.contextmanager
def _tcp_server(tmp_path, *, approach=None, gen=None, gateway_plugin=None):
    """Start a real TCP server subprocess via the runner's domain_server.

    approach= tells the server which approach payments-agent runs under
    (passed as PAYMENTS_AGENT_APPROACH in env_extra). For approach 3, also
    pass gen= (defaults to "gen-1"; passed as PAYMENTS_AGENT_GEN).

    For approaches 1-3: plain server (no gateway).
    For approaches 4-6: pass gateway_plugin="approach4" etc. to load the
    real plugin with GATEWAY=true + GATEWAY_PLUGIN=<name>.

    SERVER_TEST_MODE=1 is always set so /control/set_model_turns works.
    HOOK_LOG is set in both the server subprocess and the test process so
    hook decisions from both agents land in the same file.

    Yields (base_url, log_path, hook_log_path).

    FAILS now: the server subprocess exits immediately because
    /control/set_model_turns and /a2a/payments-agent are not implemented.
    """
    from runner._server import domain_server

    log_file = tmp_path / "decisions.jsonl"
    hook_file = tmp_path / "hook_decisions.jsonl"

    env_extra = {
        "SERVER_TEST_MODE": "1",
        "HOOK_LOG": str(hook_file),
    }
    if approach:
        env_extra["PAYMENTS_AGENT_APPROACH"] = approach
    if gen:
        env_extra["PAYMENTS_AGENT_GEN"] = gen
    if gateway_plugin:
        env_extra["GATEWAY"] = "true"
        env_extra["GATEWAY_PLUGIN"] = gateway_plugin

    old_hook = os.environ.get("HOOK_LOG")
    os.environ["HOOK_LOG"] = str(hook_file)
    try:
        with domain_server(str(log_file), env_extra=env_extra) as (_port, base_url):
            yield base_url, log_file, hook_file
    finally:
        if old_hook is not None:
            os.environ["HOOK_LOG"] = old_hook
        else:
            os.environ.pop("HOOK_LOG", None)


async def _set_model_turns(ctrl: httpx.AsyncClient, turns: list[dict]) -> None:
    """Set scripted model turns for payments-agent's next A2A invocation.

    Calls /control/set_model_turns on the TCP server (which has
    SERVER_TEST_MODE=1 in its env_extra) to inject a ScriptedModel so
    acceptance tests never call Bedrock.

    FAILS until /control/set_model_turns is implemented in domain/server.py.
    """
    r = await ctrl.post("/control/set_model_turns", json={
        "agent": "payments-agent",
        "turns": turns,
    })
    r.raise_for_status()


async def _run_first_agent(
    approach: str,
    base_url: str,
    *,
    user: str,
    calling_agent: str,
    task: str,
    first_agent_turns: list[dict],
    payments_agent_turns: list[dict],
    scope: list[str] | None = None,
    use_gateway: bool = False,
    timeout: float = 60.0,
) -> dict:
    """Drive the full two-agent pipeline through each arm's production code.

    base_url is the TCP server started by _tcp_server (via domain_server).

    Calls the arm's own run() with model=ScriptedModel(...) as the ONLY
    test injection. MCPClient connects to the real TCP server. model= is the
    only difference between the test path and the real runner path. The
    arm's built-in ask_payments_agent tool is used; this helper must not
    redefine or re-wire it.

    A 60-second asyncio.wait_for wraps the agent execution so deadlocks fail
    the test rather than hanging.

    Returns {"delegation_token": str}.

    FAILS now because:
    – agents:payments absent from FIXED_SCOPES → delegation token fails
    – /control/set_model_turns returns 404
    – production run() does not accept model= → TypeError
    """
    if scope is None:
        scope = list(_AGENT_SCOPES[calling_agent])

    async with httpx.AsyncClient(base_url=base_url) as ctrl:
        deleg_tok = await _issue_delegation_token(
            ctrl,
            user=user,
            calling_agent=calling_agent,
            scope=scope,
        )
        await _set_model_turns(ctrl, payments_agent_turns)

    first_model = ScriptedModel(first_agent_turns, label=f"first-{approach}")

    if approach in ("1", "4", "5", "6"):
        from arms.a_guides.agent import run as _arm_run
    elif approach == "2":
        from arms.c_hook.agent import run as _arm_run
    elif approach == "3":
        from arms.approach3.agent import run as _arm_run
    else:
        raise ValueError(f"Unknown approach: {approach!r}")

    async def _drive() -> None:
        await asyncio.to_thread(
            _arm_run,
            agent_name=calling_agent,
            user=user,
            turns=[task],
            mcp_url=f"{base_url}/mcp",
            bearer_token=deleg_tok,
            model=first_model,
            use_gateway=use_gateway,
        )

    await asyncio.wait_for(_drive(), timeout=timeout)
    return {"delegation_token": deleg_tok}


# ---------------------------------------------------------------------------
# ── Item 0: kit snapshot is complete ────────────────────────────────────────
# ---------------------------------------------------------------------------

def test_item0_kit_snapshot_complete():
    """
    arms/approach3/kit_snapshot/ must contain every kit input so that
    build_kit reads ALL its sources from the snapshot, not the live tree.

    Required snapshot contents (the full set of kit inputs):
      domain/ (all 8 allowed domain files)
      policy.md
      spec_input.md
      _contract_tests.py
      config/agents/expense-assistant.yaml
      config/agents/payments-agent.yaml
      config/agents/travel-assistant.yaml
      uv.lock

    Also verifies that build_kit reads agent configs and uv.lock from the
    snapshot (currently true) and will also read domain files, policy.md,
    spec_input.md, and _contract_tests.py from it after step 5b.

    FAILS now: kit_snapshot/ is missing domain/, policy.md, spec_input.md,
    and _contract_tests.py.
    """
    _APPROACH3_DIR = _REPO / "arms" / "approach3"
    _SNAPSHOT = _APPROACH3_DIR / "kit_snapshot"

    required = [
        "domain/__init__.py",
        "domain/fixtures.py",
        "domain/identity.py",
        "domain/scopes.py",
        "domain/server.py",
        "domain/simclock.py",
        "domain/state.py",
        "domain/tokens.py",
        "policy.md",
        "spec_input.md",
        "_contract_tests.py",
        "config/agents/expense-assistant.yaml",
        "config/agents/payments-agent.yaml",
        "config/agents/travel-assistant.yaml",
        "uv.lock",
    ]

    missing = [r for r in required if not (_SNAPSHOT / r).exists()]
    assert not missing, (
        "arms/approach3/kit_snapshot/ is missing required kit inputs:\n"
        + "\n".join(f"  {f}" for f in missing)
        + "\n\nAll kit inputs must be committed to kit_snapshot/ so that "
        "build_kit reads from the snapshot (not the live tree) and the "
        "byte-identical test remains stable after live-tree changes."
    )

    # Verify build_kit reads configs and uv.lock from the snapshot.
    import tempfile
    from arms.approach3.make_kit import build_kit

    with tempfile.TemporaryDirectory() as tmp:
        target = pathlib.Path(tmp) / "kit"
        build_kit(target)

        for rel in (
            "config/agents/expense-assistant.yaml",
            "config/agents/payments-agent.yaml",
            "config/agents/travel-assistant.yaml",
            "uv.lock",
        ):
            built = (target / rel).read_bytes()
            snap = (_SNAPSHOT / rel).read_bytes()
            assert built == snap, (
                f"Kit output {rel!r} does not match kit_snapshot/{rel}. "
                "build_kit must read this file exclusively from kit_snapshot/."
            )

        # Verify build_kit also reads domain files from kit_snapshot/domain/
        # (post-step-5b requirement; fails until build_kit is updated).
        for name in (
            "__init__.py", "fixtures.py", "identity.py", "scopes.py",
            "simclock.py", "state.py", "tokens.py",
        ):
            built = (target / "domain" / name).read_bytes()
            snap = (_SNAPSHOT / "domain" / name).read_bytes()
            assert built == snap, (
                f"Kit domain/{name} does not match kit_snapshot/domain/{name}. "
                "build_kit must read domain files from kit_snapshot/, not the live tree."
            )


# ---------------------------------------------------------------------------
# ── Item 1: A2A server registered in server.py GATEWAY block ────────────────
# ---------------------------------------------------------------------------

def test_item1_a2a_server_in_gateway_block():
    """
    payments-agent's A2A server must be registered inside the GATEWAY markers
    in domain/server.py.

    FAILS now: no A2AServer or /a2a/ route inside the GATEWAY block.
    """
    src = _SERVER_PY.read_text(encoding="utf-8")
    gw_block = _gw_source_extract_between(
        src, "# --- GATEWAY BEGIN ---", "# --- GATEWAY END ---",
    )
    assert gw_block, "No GATEWAY BEGIN/END block found in domain/server.py."
    assert "A2AServer" in gw_block or "/a2a/" in gw_block or "a2a_server" in gw_block, (
        "payments-agent A2AServer not registered inside GATEWAY markers in "
        "domain/server.py. Add it there so it starts only in gateway mode "
        "and shares the same event loop."
    )


def test_item1_a2a_endpoint_registered_on_main_app():
    """
    The /a2a/payments-agent path must appear in domain/server.py so the
    A2AServer is mounted on the same Starlette app as all other routes.

    FAILS now: the path is not registered.
    """
    src = _SERVER_PY.read_text(encoding="utf-8")
    assert "/a2a/payments-agent" in src or "a2a" in src.lower(), (
        "/a2a/payments-agent route is not registered in domain/server.py."
    )


async def test_item1_a2a_route_accessible_via_asgi(running_app):
    """
    The /a2a/payments-agent endpoint must be reachable via the ASGI app
    (response is not 404 or 405).

    FAILS now: the route does not exist → 404.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as client:
        resp = await client.post(
            _A2A_PATH,
            json=_a2a_send_request("hello"),
            headers={"Content-Type": "application/json"},
        )
    # In the correct implementation: 401 (no token) or 200 (accepted).
    # In the current code: 404.
    assert resp.status_code not in (404, 405), (
        f"Expected /a2a/payments-agent to be registered; got {resp.status_code}."
    )


# ---------------------------------------------------------------------------
# ── Item 2: A2A endpoint token verification (audience = payments-agent) ──────
# ---------------------------------------------------------------------------

async def test_item2_a2a_missing_token_identity_denied(running_app, log_path):
    """
    A call to /a2a/payments-agent without a bearer token must be refused
    with rule IDENTITY.

    FAILS now: the endpoint does not exist → 404 instead of IDENTITY denial.
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app),
        base_url="http://localhost:8765",
        headers={"Host": "localhost:8765"},
    ) as client:
        resp = await client.post(
            _A2A_PATH,
            json=_a2a_send_request("pay vendor acme-hotels"),
            headers={"Content-Type": "application/json"},
        )
    # The endpoint must exist and return an IDENTITY denial (HTTP 401 / 403,
    # or a JSON-RPC error body with IDENTITY).
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    text = json.dumps(body)
    assert resp.status_code in (400, 401, 403) or "IDENTITY" in text, (
        "Missing bearer to /a2a/payments-agent should produce an IDENTITY denial; "
        f"got status={resp.status_code} body={text[:200]}"
    )
    decisions = _decisions(log_path)
    if decisions:
        last = decisions[-1]
        assert last["decision"] == "deny"
        assert last["rule"] == "IDENTITY"


async def test_item2_a2a_wrong_audience_identity_denied(running_app, http_client, log_path):
    """
    A bearer token with aud != payments-agent must be refused with rule IDENTITY.

    FAILS now: the endpoint does not exist → 404.
    """
    # Issue a valid token but with aud=mcp-server (wrong for /a2a/payments-agent)
    r = await http_client.post("/control/identity/user-token", json={
        "sub": "alice",
        "aud": "expense-assistant",
        "scope": sorted(tokens.FIXED_SCOPES),
    })
    r.raise_for_status()
    user_tok = r.json()["access_token"]
    r = await http_client.post("/control/identity/agent-token", json={
        "sub": "expense-assistant",
        "scope": sorted(tokens.FIXED_SCOPES),
    })
    r.raise_for_status()
    agent_tok = r.json()["access_token"]
    r = await http_client.post("/identity/exchange", json={
        "subject_token": user_tok,
        "actor_token": agent_tok,
        # aud defaults to mcp-server, NOT payments-agent
    })
    r.raise_for_status()
    mcp_token = r.json()["access_token"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=running_app),
        base_url="http://localhost:8765",
        headers={
            "Host": "localhost:8765",
            "Authorization": f"Bearer {mcp_token}",
        },
    ) as client:
        resp = await client.post(
            _A2A_PATH,
            json=_a2a_send_request("pay vendor acme-hotels"),
            headers={"Content-Type": "application/json"},
        )
    body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
    text = json.dumps(body)
    assert resp.status_code in (400, 401, 403) or "IDENTITY" in text, (
        "Token with aud=mcp-server (not payments-agent) should produce IDENTITY denial "
        f"at /a2a/payments-agent; got status={resp.status_code} body={text[:200]}"
    )
    decisions = _decisions(log_path)
    if decisions:
        last = decisions[-1]
        assert last["decision"] == "deny"
        assert last["rule"] == "IDENTITY"


# ---------------------------------------------------------------------------
# ── Item 3: token chain (delegation → tool-server) ──────────────────────────
# ---------------------------------------------------------------------------

async def test_item3_delegation_token_has_correct_claims(http_client):
    """
    The delegation token produced by the runner's pre-issue step must have:
      sub = alice
      aud = payments-agent
      act = {sub: expense-assistant}
      scope includes agents:payments

    FAILS now: agents:payments is not in FIXED_SCOPES so the scope is
    filtered out during token issue.
    """
    import jwt as _jwt

    deleg_tok = await _issue_delegation_token(
        http_client,
        user="alice",
        calling_agent="expense-assistant",
        scope=list(_AGENT_SCOPES["expense-assistant"]),
    )
    claims = _jwt.decode(deleg_tok, options={"verify_signature": False})

    assert claims["sub"] == "alice"
    assert claims["aud"] == _PAYMENTS_AGENT_AUD
    assert claims.get("act", {}).get("sub") == "expense-assistant", (
        f"act.sub should be 'expense-assistant', got {claims.get('act')}"
    )
    assert "agents:payments" in claims.get("scope", ""), (
        "'agents:payments' not in delegation token scope. "
        "It must be added to FIXED_SCOPES and the runner's per-agent scope list."
    )


async def test_item3_tool_server_token_has_two_hop_chain(http_client):
    """
    After payments-agent exchanges the delegation token, the resulting tool-server
    token must have:
      sub = alice
      aud = mcp-server
      act = {sub: payments-agent, act: {sub: expense-assistant}}
      scope = intersection of expense-assistant and payments-agent scopes

    With real scopes the intersection is {expenses:approve} — payments:pay
    is absent because expense-assistant never carries it.

    FAILS now: the exchange chain doesn't work because agents:payments is
    absent from FIXED_SCOPES and the delegation token cannot be created.
    """
    import jwt as _jwt

    deleg_tok = await _issue_delegation_token(
        http_client,
        user="alice",
        calling_agent="expense-assistant",
        scope=list(_AGENT_SCOPES["expense-assistant"]),
    )
    tool_tok = await _issue_tool_server_token(http_client, delegation_token=deleg_tok)

    claims = _jwt.decode(tool_tok, options={"verify_signature": False})
    assert claims["sub"] == "alice"
    assert claims["aud"] == tokens.SERVER_AUDIENCE
    act = claims.get("act", {})
    assert act.get("sub") == "payments-agent", (
        f"act.sub should be 'payments-agent', got {act.get('sub')!r}"
    )
    inner_act = act.get("act", {})
    assert inner_act.get("sub") == "expense-assistant", (
        f"act.act.sub should be 'expense-assistant', got {inner_act.get('sub')!r}"
    )
    scope_set = set(claims.get("scope", "").split()) - {""}
    assert scope_set == {"expenses:approve"}, (
        f"Expected tool-server scope {{expenses:approve}} (intersection of "
        f"expense-assistant and payments-agent scopes), got {scope_set!r}."
    )


async def test_item3_no_token_in_a2a_message_text(tmp_path):
    """
    JWT tokens must never appear in the decision log. This test first runs
    the full two-agent chain via _run_first_agent (so log entries exist),
    then asserts: (a) at least one log entry was written, and (b) none of
    the entries contain a JWT token string.

    FAILS now: /control/set_model_turns is not implemented (404), so
    _run_first_agent fails before any log entries are written.
    """
    with _tcp_server(tmp_path) as (base_url, log_path, _hook_log_path):
        await _run_first_agent(
            approach="1",
            base_url=base_url,
            user="alice",
            calling_agent="expense-assistant",
            task="Pay vendor acme-hotels £100",
            first_agent_turns=[
                {"tool": "ask_payments_agent", "input": {"request": "pay vendor acme-hotels 100"}},
                {"text": "Payment refused."},
            ],
            payments_agent_turns=[
                {"text": "I cannot authorise payments for non-finance users."},
            ],
        )

        entries = _read_log(log_path)
        assert entries, (
            "No decision-log entries found after running the two-agent chain. "
            "Either the /a2a/payments-agent endpoint is not logging decisions or "
            "it is not implemented yet."
        )

        jwt_pat = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")
        for entry in entries:
            raw = json.dumps(entry)
            assert not jwt_pat.search(raw), (
                "A JWT token string appeared in the decision log. "
                "Tokens must never appear in logged prompts or transcripts."
            )


# ---------------------------------------------------------------------------
# ── Item 4: ask_payments_agent tool in calling-agent code ───────────────────
# ---------------------------------------------------------------------------

def test_item4_arm_a_has_ask_payments_agent_tool():
    """
    arms/a_guides/agent.py must define or import ask_payments_agent as a
    Strands tool so expense-assistant (approach 1) can call it.

    FAILS now: no such tool exists.
    """
    src = (_REPO / "arms" / "a_guides" / "agent.py").read_text(encoding="utf-8")
    assert "ask_payments_agent" in src, (
        "ask_payments_agent tool not found in arms/a_guides/agent.py. "
        "Add it as a Strands tool (local, no fingerprint)."
    )


def test_item4_arm_c_has_ask_payments_agent_tool():
    """
    arms/c_hook/agent.py must define or import ask_payments_agent.

    FAILS now: no such tool exists.
    """
    src = (_REPO / "arms" / "c_hook" / "agent.py").read_text(encoding="utf-8")
    assert "ask_payments_agent" in src, (
        "ask_payments_agent tool not found in arms/c_hook/agent.py."
    )


def test_item4_approach3_agent_has_ask_payments_agent_tool():
    """
    arms/approach3/agent.py must define or import ask_payments_agent.

    FAILS now: no such tool exists.
    """
    src = (_REPO / "arms" / "approach3" / "agent.py").read_text(encoding="utf-8")
    assert "ask_payments_agent" in src, (
        "ask_payments_agent tool not found in arms/approach3/agent.py."
    )


def test_item4_approach4_agent_has_ask_payments_agent_tool():
    """
    arms/approach4 (or the gateway-mode agent) must expose ask_payments_agent.
    FAILS now: no such tool exists.
    """
    agent_files = list((_REPO / "arms").rglob("agent.py"))
    found = any(
        "ask_payments_agent" in f.read_text(encoding="utf-8")
        for f in agent_files
    )
    assert found, (
        "ask_payments_agent not found in any arms/*/agent.py file. "
        "It must be added as a local Strands tool for approaches 4-6."
    )


def test_item4_ask_payments_agent_no_mcp_fingerprint_required():
    """
    ask_payments_agent is a LOCAL Strands tool (not an MCP tool), so it must
    NOT appear in domain/fingerprints.json (no fingerprint).

    Passes now: ask_payments_agent is not in fingerprints.json (because it
    doesn't exist at all yet). This check becomes a meaningful guard once
    the tool is implemented.
    """
    fp_path = _REPO / "domain" / "fingerprints.json"
    if not fp_path.exists():
        pytest.fail("domain/fingerprints.json does not exist.")
    fps = json.loads(fp_path.read_text(encoding="utf-8"))
    assert "ask_payments_agent" not in fps, (
        "ask_payments_agent appeared in domain/fingerprints.json. "
        "It is a local Strands tool and must not have a gateway fingerprint."
    )


def test_item4_ask_payments_agent_config_in_c_hook():
    """
    arms/c_hook/config/*.yaml files for expense-assistant and travel-assistant
    must list ask_payments_agent in allowed_tools (no fingerprint entry).

    FAILS now: the tool is not in those config files.
    """
    for agent in ("expense-assistant", "travel-assistant"):
        cfg_path = _REPO / "arms" / "c_hook" / "config" / f"{agent}.yaml"
        if not cfg_path.exists():
            pytest.fail(f"Config file {cfg_path} not found.")
        text = cfg_path.read_text(encoding="utf-8")
        assert "ask_payments_agent" in text, (
            f"ask_payments_agent not in {cfg_path}. "
            "Add it to allowed_tools (no fingerprint — it's a local tool)."
        )


# ---------------------------------------------------------------------------
# ── Item 5: gateway /gateway/a2a/payments-agent ──────────────────────────────
# ---------------------------------------------------------------------------

def test_item5_gateway_a2a_route_in_gateway_py():
    """
    domain/gateway.py must implement a handler for /gateway/a2a/payments-agent.

    FAILS now: no such route exists.
    """
    src = _GATEWAY_PY.read_text(encoding="utf-8")
    assert "/gateway/a2a/" in src or "handle_a2a" in src or "a2a" in src.lower(), (
        "No /gateway/a2a/ route or handle_a2a function found in domain/gateway.py."
    )


def test_item5_server_registers_gateway_a2a_route():
    """
    domain/server.py must register the /gateway/a2a/payments-agent route
    inside the GATEWAY markers.

    FAILS now: no such route is registered.
    """
    src = _SERVER_PY.read_text(encoding="utf-8")
    gw_block = _gw_source_extract_between(
        src, "# --- GATEWAY BEGIN ---", "# --- GATEWAY END ---",
    )
    assert "/gateway/a2a/" in gw_block or "handle_a2a" in gw_block, (
        "/gateway/a2a/payments-agent not registered in server.py GATEWAY block."
    )


async def test_item5_gateway_a2a_missing_token_identity(running_app, log_path):
    """
    POST /gateway/a2a/payments-agent without token → IDENTITY denial.
    Order: IDENTITY must fire first.

    FAILS now: the endpoint does not exist → 404.
    """
    from domain.gateway import install_gateway, remove_gateway
    from arms.approach4.plugin import evaluate as _a4_plugin

    from arms.d_boundary.pep import fingerprint as _fp
    from domain.server import mcp as _mcp
    fps = {
        name: _fp(tool.name, tool.description, tool.parameters)
        for name, tool in _mcp._tool_manager._tools.items()
    }
    from domain.grants import AGENT_GRANTS

    install_gateway(grants=AGENT_GRANTS, fingerprints=fps, policy_plugin=_a4_plugin)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=running_app),
            base_url="http://localhost:8765",
            headers={"Host": "localhost:8765"},
        ) as client:
            resp = await client.post(
                _GW_A2A_PATH,
                json=_a2a_send_request("pay vendor acme-hotels"),
                headers={"Content-Type": "application/json"},
            )
        assert resp.status_code in (400, 401, 403, 200), (
            f"Expected the /gateway/a2a/ route to exist; got {resp.status_code}."
        )
        body = resp.json() if resp.headers.get("content-type", "").startswith("application/json") else {}
        decisions = _decisions(log_path)
        if decisions:
            last = decisions[-1]
            assert last["rule"] == "IDENTITY", (
                f"Expected IDENTITY denial, got rule={last['rule']!r}."
            )
    finally:
        remove_gateway()


async def test_item5_gateway_a2a_grant_checked(running_app, http_client, log_path):
    """
    A bearer with a valid IDENTITY but unknown agent → GRANT denial.
    Order: IDENTITY passes, GRANT fires next.

    FAILS now: the /gateway/a2a/ endpoint does not exist.
    """
    from domain.gateway import install_gateway, remove_gateway
    from arms.approach4.plugin import evaluate as _a4_plugin
    from arms.d_boundary.pep import fingerprint as _fp
    from domain.server import mcp as _mcp
    from domain.grants import AGENT_GRANTS

    fps = {
        name: _fp(tool.name, tool.description, tool.parameters)
        for name, tool in _mcp._tool_manager._tools.items()
    }
    install_gateway(grants=AGENT_GRANTS, fingerprints=fps, policy_plugin=_a4_plugin)
    try:
        # rogue-agent is not in AGENT_GRANTS → GRANT denial
        deleg_tok = await _issue_delegation_token(
            http_client,
            user="alice",
            calling_agent="rogue-agent",
            scope=["agents:payments", "payments:pay"],
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=running_app),
            base_url="http://localhost:8765",
            headers={
                "Host": "localhost:8765",
                "Authorization": f"Bearer {deleg_tok}",
            },
        ) as client:
            resp = await client.post(
                _GW_A2A_PATH,
                json=_a2a_send_request("pay vendor acme-hotels"),
                headers={"Content-Type": "application/json"},
            )
        decisions = _decisions(log_path)
        assert decisions, "No decision logged."
        last = decisions[-1]
        assert last["decision"] == "deny"
        assert last["rule"] == "GRANT", (
            f"Expected GRANT denial, got rule={last['rule']!r}."
        )
    finally:
        remove_gateway()


async def test_item5_gateway_a2a_scope_checked(running_app, http_client, log_path):
    """
    A bearer with IDENTITY+GRANT but missing agents:payments scope → SCOPE denial.
    Order: IDENTITY, GRANT pass; SCOPE fires next.

    FAILS now: the /gateway/a2a/ endpoint does not exist.
    """
    from domain.gateway import install_gateway, remove_gateway
    from arms.approach4.plugin import evaluate as _a4_plugin
    from arms.d_boundary.pep import fingerprint as _fp
    from domain.server import mcp as _mcp
    from domain.grants import AGENT_GRANTS

    fps = {
        name: _fp(tool.name, tool.description, tool.parameters)
        for name, tool in _mcp._tool_manager._tools.items()
    }
    install_gateway(grants=AGENT_GRANTS, fingerprints=fps, policy_plugin=_a4_plugin)
    try:
        # Token has no agents:payments scope
        deleg_tok = await _issue_delegation_token(
            http_client,
            user="alice",
            calling_agent="expense-assistant",
            scope=[s for s in _AGENT_SCOPES["expense-assistant"] if s != "agents:payments"],
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=running_app),
            base_url="http://localhost:8765",
            headers={
                "Host": "localhost:8765",
                "Authorization": f"Bearer {deleg_tok}",
            },
        ) as client:
            resp = await client.post(
                _GW_A2A_PATH,
                json=_a2a_send_request("pay vendor acme-hotels"),
                headers={"Content-Type": "application/json"},
            )
        decisions = _decisions(log_path)
        assert decisions, "No decision logged."
        last = decisions[-1]
        assert last["decision"] == "deny"
        assert last["rule"] == "SCOPE", (
            f"Expected SCOPE denial, got rule={last['rule']!r}."
        )
    finally:
        remove_gateway()


async def test_item5_direct_a2a_refused_gateway_bypass(
    running_app, http_client, log_path,
):
    """
    In gateway mode, a direct POST to /a2a/payments-agent (bypassing
    /gateway/a2a/) must be refused with rule=GATEWAY_BYPASS.

    FAILS now: the /a2a/payments-agent endpoint does not enforce this.
    """
    from domain.gateway import install_gateway, remove_gateway
    from arms.approach4.plugin import evaluate as _a4_plugin
    from arms.d_boundary.pep import fingerprint as _fp
    from domain.server import mcp as _mcp
    from domain.grants import AGENT_GRANTS

    fps = {
        name: _fp(tool.name, tool.description, tool.parameters)
        for name, tool in _mcp._tool_manager._tools.items()
    }
    install_gateway(grants=AGENT_GRANTS, fingerprints=fps, policy_plugin=_a4_plugin)
    try:
        deleg_tok = await _issue_delegation_token(
            http_client,
            user="alice",
            calling_agent="expense-assistant",
            scope=list(_AGENT_SCOPES["expense-assistant"]),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=running_app),
            base_url="http://localhost:8765",
            headers={
                "Host": "localhost:8765",
                "Authorization": f"Bearer {deleg_tok}",
            },
        ) as client:
            resp = await client.post(
                _A2A_PATH,  # direct, not /gateway/a2a/
                json=_a2a_send_request("pay vendor acme-hotels"),
                headers={"Content-Type": "application/json"},
            )
        decisions = _decisions(log_path)
        assert decisions, "No decision logged."
        last = decisions[-1]
        assert last["decision"] == "deny"
        assert last["rule"] == "GATEWAY_BYPASS", (
            f"Expected GATEWAY_BYPASS, got rule={last['rule']!r}."
        )
    finally:
        remove_gateway()


# ---------------------------------------------------------------------------
# ── Item 6: scope, scope-map, and grants ────────────────────────────────────
# These are the cheapest checks and are fundamental to everything else.
# ---------------------------------------------------------------------------

def test_item6_agents_payments_in_fixed_scopes():
    """
    'agents:payments' must be in tokens.FIXED_SCOPES.

    FAILS now: FIXED_SCOPES only contains the five expense/travel/payments
    tool scopes; agents:payments has not been added yet.
    """
    assert "agents:payments" in tokens.FIXED_SCOPES, (
        "'agents:payments' is not in tokens.FIXED_SCOPES. "
        "Add it so delegation tokens can carry this scope."
    )


def test_item6_scope_map_has_ask_payments_agent():
    """
    TOOL_SCOPE_MAP must map ask_payments_agent → agents:payments.

    FAILS now: domain/scopes.py TOOL_SCOPE_MAP only has the five MCP tools.
    """
    from domain.scopes import TOOL_SCOPE_MAP
    assert "ask_payments_agent" in TOOL_SCOPE_MAP, (
        "'ask_payments_agent' missing from TOOL_SCOPE_MAP in domain/scopes.py."
    )
    assert TOOL_SCOPE_MAP["ask_payments_agent"] == "agents:payments", (
        f"Expected ask_payments_agent → 'agents:payments', "
        f"got {TOOL_SCOPE_MAP['ask_payments_agent']!r}."
    )


def test_item6_expense_assistant_grant_includes_ask_payments_agent():
    """
    expense-assistant's AGENT_GRANTS must include ask_payments_agent.

    FAILS now: AGENT_GRANTS only has read_receipt, submit_expense,
    approve_expense for expense-assistant.
    """
    from domain.grants import AGENT_GRANTS
    granted = AGENT_GRANTS.get("expense-assistant", [])
    assert "ask_payments_agent" in granted, (
        "ask_payments_agent not in AGENT_GRANTS['expense-assistant']. "
        "Add it to domain/grants.py."
    )


def test_item6_travel_assistant_grant_includes_ask_payments_agent():
    """
    travel-assistant's AGENT_GRANTS must include ask_payments_agent.

    FAILS now: AGENT_GRANTS only has book_travel, submit_expense for
    travel-assistant.
    """
    from domain.grants import AGENT_GRANTS
    granted = AGENT_GRANTS.get("travel-assistant", [])
    assert "ask_payments_agent" in granted, (
        "ask_payments_agent not in AGENT_GRANTS['travel-assistant']. "
        "Add it to domain/grants.py."
    )


def test_item6_runner_agent_scopes_include_agents_payments():
    """
    runner.run._AGENT_SCOPES for expense-assistant and travel-assistant must
    include 'agents:payments'.

    FAILS now: neither agent has this scope in runner/run.py.
    """
    from runner.run import _AGENT_SCOPES
    for agent_name in ("expense-assistant", "travel-assistant"):
        scopes = _AGENT_SCOPES.get(agent_name, [])
        assert "agents:payments" in scopes, (
            f"'agents:payments' not in runner._AGENT_SCOPES[{agent_name!r}]. "
            "Add it so delegation tokens carry this scope."
        )


# ---------------------------------------------------------------------------
# ── Item 7: decision log records full agent chain ────────────────────────────
# ---------------------------------------------------------------------------

async def test_item7_a2a_hop_decision_has_agent_chain(tmp_path):
    """
    A pay_vendor decision logged at the A2A hop (via ask_payments_agent →
    /a2a/payments-agent → pay_vendor) must include both agents in the chain.

    The test drives the full pipeline with _run_first_agent so the logged
    decision is produced by the real A2A pathway, not a direct /mcp call.

    FAILS now: /control/set_model_turns and /a2a/payments-agent are not
    implemented; _run_first_agent fails before any decision is logged.
    """
    with _tcp_server(tmp_path) as (base_url, log_path, _hook_log_path):
        await _run_first_agent(
            approach="1",
            base_url=base_url,
            user="alice",
            calling_agent="expense-assistant",
            task="Pay vendor acme-hotels £100",
            first_agent_turns=[
                {"tool": "ask_payments_agent", "input": {"request": "pay vendor acme-hotels 100 ref-chain"}},
                {"text": "Done."},
            ],
            payments_agent_turns=[
                {"tool": "pay_vendor", "input": {"vendor": "acme-hotels", "amount": 100.0, "reference": "ref-chain"}},
                {"text": "Payment processed."},
            ],
        )

        decisions = _decisions(log_path)
        pay_decisions = [d for d in decisions if d.get("tool") == "pay_vendor"]
        assert pay_decisions, (
            "No pay_vendor decision logged after A2A two-agent chain. "
            "The decision for pay_vendor must be logged when payments-agent "
            "receives it via the A2A hop."
        )
        d = pay_decisions[-1]

        agent = d.get("agent")
        assert isinstance(agent, list), (
            f"Expected agent to be a list (full chain) but got {agent!r}. "
            "Decision log must record the full agent chain for two-hop calls."
        )
        assert "payments-agent" in agent, (
            f"'payments-agent' not in agent chain {agent!r}."
        )
        assert "expense-assistant" in agent, (
            f"'expense-assistant' not in agent chain {agent!r}."
        )
        assert d.get("user") == "alice", (
            f"user in log entry should be 'alice', got {d.get('user')!r}."
        )


# ---------------------------------------------------------------------------
# ── Acceptance: S15 shape — alice / expense-assistant / pay_vendor ───────────
# (for all six approaches)
# ---------------------------------------------------------------------------
#
# Pattern for all S15 tests: _run_first_agent drives expense-assistant
# (first hop) → payments-agent (second hop). The first agent's scripted
# model calls ask_payments_agent; the second agent's scripted model attempts
# pay_vendor. Policy refuses at either the second hop (approaches 2-3) or
# the gateway (approaches 4-6); approach 1 model self-refuses.

async def test_s15_approach1_pay_vendor_executes_documenting_exposure(tmp_path):
    """
    Approach 1 cannot be tested for refusal with a scripted model: the
    script decides what to call, not the prompt rules. Instead, verify
    approach 1's exposure: with a script that calls pay_vendor, the payment
    IS executed and nothing else refuses it.

    Checks:
      1. The request reaches payments-agent (ledger has vendor_paid)
      2. payments-agent's system prompt contains the policy rules
      3. The token is the narrowed two-hop token (sub=alice,
         aud=payments-agent, act.sub=expense-assistant)
      4. pay_vendor IS executed — no enforcement beyond the prompt

    FAILS now: /control/set_model_turns and /a2a/payments-agent are not
    implemented; _run_first_agent fails immediately.
    """
    with _tcp_server(tmp_path, approach="1") as (base_url, log_path, hook_log_path):
        result = await _run_first_agent(
            approach="1",
            base_url=base_url,
            user="alice",
            calling_agent="expense-assistant",
            task="Pay vendor acme-hotels £100 reference ref-s15a1",
            first_agent_turns=[
                {"tool": "ask_payments_agent", "input": {"request": "pay vendor acme-hotels 100 ref-s15a1"}},
                {"text": "Done."},
            ],
            payments_agent_turns=[
                {"tool": "pay_vendor", "input": {"vendor": "acme-hotels", "amount": 100.0, "reference": "ref-s15a1"}},
                {"text": "Payment sent."},
            ],
        )

        # 1. Payment IS executed (nothing else refuses it)
        async with httpx.AsyncClient(base_url=base_url) as ctrl:
            ledger = (await ctrl.get("/control/ledger")).json()
        vendor_paid = [e for e in ledger if e.get("action_type") == "vendor_paid"]
        assert vendor_paid, (
            "pay_vendor was NOT executed for alice via approach 1. "
            "Approach 1 relies solely on prompt rules; with a scripted model "
            "that calls pay_vendor, nothing else should refuse the payment."
        )

        # 2. payments-agent's system prompt contains policy rules
        async with httpx.AsyncClient(base_url=base_url) as ctrl:
            r = await ctrl.get("/control/last_transcript")
        assert r.status_code == 200, (
            f"/control/last_transcript returned {r.status_code}; "
            "must be implemented so tests can verify the system prompt."
        )
        transcript_text = json.dumps(r.json())
        assert any(kw in transcript_text.lower() for kw in ("finance", "policy", "pay_vendor")), (
            "payments-agent's transcript does not contain policy rules. "
            "Approach 1 depends on these rules being in the system prompt."
        )

        # 3. The delegation token is the narrowed two-hop token
        import jwt as _jwt
        claims = _jwt.decode(
            result["delegation_token"], options={"verify_signature": False},
        )
        assert claims["sub"] == "alice"
        assert claims["aud"] == _PAYMENTS_AGENT_AUD
        assert claims.get("act", {}).get("sub") == "expense-assistant"


async def test_s15_approach2_alice_expense_pay_vendor_refused(tmp_path):
    """
    S15 / approach 2: alice via expense-assistant calls ask_payments_agent →
    payments-agent. The scripted model calls pay_vendor; payments-agent's own
    PolicyHook denies it (alice is not a finance user). The deny must appear
    in payments-agent's hook log (HOOK_LOG), not only the gateway log.

    FAILS now: /control/set_model_turns and /a2a/payments-agent are not
    implemented.
    """
    with _tcp_server(tmp_path, approach="2") as (base_url, log_path, hook_log_path):
        await _run_first_agent(
            approach="2",
            base_url=base_url,
            user="alice",
            calling_agent="expense-assistant",
            task="Pay vendor acme-hotels £100 reference ref-s15a2",
            first_agent_turns=[
                {"tool": "ask_payments_agent", "input": {"request": "pay vendor acme-hotels 100 ref-s15a2"}},
                {"text": "Payment refused."},
            ],
            payments_agent_turns=[
                {"tool": "pay_vendor", "input": {"vendor": "acme-hotels", "amount": 100.0, "reference": "ref-s15a2"}},
                {"text": "The payment was refused."},
            ],
        )

        async with httpx.AsyncClient(base_url=base_url) as ctrl:
            ledger = (await ctrl.get("/control/ledger")).json()
        vendor_paid = [e for e in ledger if e.get("action_type") == "vendor_paid"]
        assert not vendor_paid, (
            f"pay_vendor executed for alice (approach 2 S15) — denied by hook. "
            f"Ledger: {ledger}"
        )
        hook_denied = _hook_decisions(hook_log_path)
        denied = [d for d in hook_denied if d.get("decision") == "deny" and d.get("tool") == "pay_vendor"]
        assert denied, (
            "Expected a deny decision for pay_vendor in payments-agent's own "
            "hook log (approach 2 PolicyHook at the second hop)."
        )


async def test_s15_approach3_alice_expense_pay_vendor_refused(tmp_path):
    """
    S15 / approach 3: alice via expense-assistant calls ask_payments_agent →
    payments-agent. The gen-1 generated checkpoint denies pay_vendor for
    alice. The deny must appear in payments-agent's own checkpoint log
    (HOOK_LOG), not only the main decision log.

    FAILS now: /control/set_model_turns and /a2a/payments-agent are not
    implemented.
    """
    with _tcp_server(tmp_path, approach="3", gen="gen-1") as (base_url, log_path, hook_log_path):
        await _run_first_agent(
            approach="3",
            base_url=base_url,
            user="alice",
            calling_agent="expense-assistant",
            task="Pay vendor acme-hotels £100 reference ref-s15a3",
            first_agent_turns=[
                {"tool": "ask_payments_agent", "input": {"request": "pay vendor acme-hotels 100 ref-s15a3"}},
                {"text": "Payment refused by policy."},
            ],
            payments_agent_turns=[
                {"tool": "pay_vendor", "input": {"vendor": "acme-hotels", "amount": 100.0, "reference": "ref-s15a3"}},
                {"text": "The payment was refused."},
            ],
        )

        async with httpx.AsyncClient(base_url=base_url) as ctrl:
            ledger = (await ctrl.get("/control/ledger")).json()
        vendor_paid = [e for e in ledger if e.get("action_type") == "vendor_paid"]
        assert not vendor_paid, (
            f"pay_vendor executed for alice (approach 3 S15) — should be refused. "
            f"Ledger: {ledger}"
        )
        hook_denied = _hook_decisions(hook_log_path)
        denied = [d for d in hook_denied if d.get("decision") == "deny" and d.get("tool") == "pay_vendor"]
        assert denied, (
            "Expected a deny decision for pay_vendor in payments-agent's own "
            "checkpoint log (approach 3 generated checkpoint at the second hop)."
        )


@pytest.mark.parametrize("approach,plugin_name", [
    ("4", "approach4"),
    ("5", "approach5"),
    ("6", "approach6"),
])
async def test_s15_gateway_approach_alice_expense_pay_vendor_refused(
    approach, plugin_name, tmp_path,
):
    """
    S15 / approaches 4-6: alice via expense-assistant calls ask_payments_agent
    → /gateway/a2a/payments-agent. The gateway (Cedar / central service /
    publisher) denies pay_vendor for alice (rule P7). Refusal is a logged
    rule, not an error.

    FAILS now: /control/set_model_turns and /gateway/a2a/payments-agent are
    not implemented.
    """
    with _tcp_server(tmp_path, approach=approach, gateway_plugin=plugin_name) as (base_url, log_path, hook_log_path):
        await _run_first_agent(
            approach=approach,
            base_url=base_url,
            user="alice",
            calling_agent="expense-assistant",
            task="Pay vendor acme-hotels £100 reference ref-s15",
            first_agent_turns=[
                {"tool": "ask_payments_agent", "input": {"request": "pay vendor acme-hotels 100 ref-s15"}},
                {"text": "Payment refused."},
            ],
            payments_agent_turns=[
                {"tool": "pay_vendor", "input": {"vendor": "acme-hotels", "amount": 100.0, "reference": "ref-s15"}},
                {"text": "Refused."},
            ],
            use_gateway=True,
        )

        async with httpx.AsyncClient(base_url=base_url) as ctrl:
            ledger = (await ctrl.get("/control/ledger")).json()
        vendor_paid = [e for e in ledger if e.get("action_type") == "vendor_paid"]
        assert not vendor_paid, (
            f"pay_vendor executed for alice (approach {approach} S15) — "
            f"should be denied. Ledger: {ledger}"
        )
        decisions = _decisions(log_path)
        denied = [d for d in decisions if d.get("decision") == "deny"]
        assert denied, (
            f"No deny decision logged (approach {approach} S15). "
            "Refusal must be a logged rule, not a Python exception."
        )
        last_deny = denied[-1]
        assert last_deny["rule"] == "SCOPE", (
            f"Expected SCOPE denial for pay_vendor (approach {approach} S15); "
            f"got rule={last_deny.get('rule')!r}. "
            "The narrowed token must lack payments:pay so SCOPE fires before the plugin."
        )


# ---------------------------------------------------------------------------
# ── Acceptance: S16 shape — alice / travel-assistant / approve_expense ───────
# (for all six approaches)
# ---------------------------------------------------------------------------

async def test_s16_approach1_approve_expense_executes_documenting_exposure(tmp_path):
    """
    Approach 1 cannot be tested for refusal with a scripted model: the
    script decides what to call, not the prompt rules. Instead, verify
    approach 1's exposure: with a script that calls approve_expense, the
    action IS executed and nothing else refuses it.

    Checks:
      1. The request reaches payments-agent (ledger has expense_approved)
      2. payments-agent's system prompt contains the policy rules
      3. The token is the narrowed two-hop token (sub=alice,
         aud=payments-agent, act.sub=travel-assistant)
      4. approve_expense IS executed — no enforcement beyond the prompt

    FAILS now: /control/set_model_turns and /a2a/payments-agent are not
    implemented.
    """
    with _tcp_server(tmp_path, approach="1") as (base_url, log_path, hook_log_path):
        result = await _run_first_agent(
            approach="1",
            base_url=base_url,
            user="alice",
            calling_agent="travel-assistant",
            task="Please approve expense exp-001",
            first_agent_turns=[
                {"tool": "ask_payments_agent", "input": {"request": "approve expense exp-001"}},
                {"text": "Done."},
            ],
            payments_agent_turns=[
                {"tool": "approve_expense", "input": {"expense_id": "exp-001"}},
                {"text": "Expense approved."},
            ],
        )

        # 1. approve_expense IS executed (nothing else refuses it)
        async with httpx.AsyncClient(base_url=base_url) as ctrl:
            ledger = (await ctrl.get("/control/ledger")).json()
        approved = [e for e in ledger if e.get("action_type") == "expense_approved"]
        assert approved, (
            "approve_expense was NOT executed for alice via approach 1. "
            "Approach 1 relies solely on prompt rules; with a scripted model "
            "that calls approve_expense, nothing else should refuse the action."
        )

        # 2. payments-agent's system prompt contains policy rules
        async with httpx.AsyncClient(base_url=base_url) as ctrl:
            r = await ctrl.get("/control/last_transcript")
        assert r.status_code == 200, (
            f"/control/last_transcript returned {r.status_code}; "
            "must be implemented so tests can verify the system prompt."
        )
        transcript_text = json.dumps(r.json())
        assert any(kw in transcript_text.lower() for kw in ("manager", "policy", "approve_expense")), (
            "payments-agent's transcript does not contain policy rules. "
            "Approach 1 depends on these rules being in the system prompt."
        )

        # 3. The delegation token is the narrowed two-hop token
        import jwt as _jwt
        claims = _jwt.decode(
            result["delegation_token"], options={"verify_signature": False},
        )
        assert claims["sub"] == "alice"
        assert claims["aud"] == _PAYMENTS_AGENT_AUD
        assert claims.get("act", {}).get("sub") == "travel-assistant"


async def test_s16_approach2_alice_travel_approve_expense_refused(tmp_path):
    """
    S16 / approach 2: alice via travel-assistant calls ask_payments_agent →
    payments-agent. Payments-agent's own PolicyHook denies approve_expense
    for alice (no manager role). The deny must appear in payments-agent's
    hook log (HOOK_LOG), not only the main decision log.

    FAILS now: /control/set_model_turns and /a2a/payments-agent are not
    implemented.
    """
    with _tcp_server(tmp_path, approach="2") as (base_url, log_path, hook_log_path):
        await _run_first_agent(
            approach="2",
            base_url=base_url,
            user="alice",
            calling_agent="travel-assistant",
            task="Please approve expense exp-001",
            first_agent_turns=[
                {"tool": "ask_payments_agent", "input": {"request": "approve expense exp-001"}},
                {"text": "Approval refused."},
            ],
            payments_agent_turns=[
                {"tool": "approve_expense", "input": {"expense_id": "exp-001"}},
                {"text": "The approval was refused."},
            ],
        )

        async with httpx.AsyncClient(base_url=base_url) as ctrl:
            ledger = (await ctrl.get("/control/ledger")).json()
        approved = [e for e in ledger if e.get("action_type") == "expense_approved"]
        assert not approved, (
            f"approve_expense executed for alice (approach 2 S16) — should be refused. "
            f"Ledger: {ledger}"
        )
        hook_denied = _hook_decisions(hook_log_path)
        denied = [d for d in hook_denied if d.get("decision") == "deny" and d.get("tool") == "approve_expense"]
        assert denied, (
            "Expected a deny decision for approve_expense in payments-agent's own "
            "hook log (approach 2 PolicyHook at the second hop)."
        )


async def test_s16_approach3_alice_travel_approve_expense_refused(tmp_path):
    """
    S16 / approach 3: alice via travel-assistant calls ask_payments_agent →
    payments-agent. The gen-1 generated checkpoint denies approve_expense
    for alice. The deny must appear in payments-agent's own checkpoint log
    (HOOK_LOG), not only the main decision log.

    FAILS now: /control/set_model_turns and /a2a/payments-agent are not
    implemented.
    """
    with _tcp_server(tmp_path, approach="3", gen="gen-1") as (base_url, log_path, hook_log_path):
        await _run_first_agent(
            approach="3",
            base_url=base_url,
            user="alice",
            calling_agent="travel-assistant",
            task="Please approve expense exp-001",
            first_agent_turns=[
                {"tool": "ask_payments_agent", "input": {"request": "approve expense exp-001"}},
                {"text": "Approval refused by policy."},
            ],
            payments_agent_turns=[
                {"tool": "approve_expense", "input": {"expense_id": "exp-001"}},
                {"text": "The approval was refused."},
            ],
        )

        async with httpx.AsyncClient(base_url=base_url) as ctrl:
            ledger = (await ctrl.get("/control/ledger")).json()
        approved = [e for e in ledger if e.get("action_type") == "expense_approved"]
        assert not approved, (
            f"approve_expense executed for alice (approach 3 S16) — should be refused. "
            f"Ledger: {ledger}"
        )
        hook_denied = _hook_decisions(hook_log_path)
        denied = [d for d in hook_denied if d.get("decision") == "deny" and d.get("tool") == "approve_expense"]
        assert denied, (
            "Expected a deny decision for approve_expense in payments-agent's own "
            "checkpoint log (approach 3 generated checkpoint at the second hop)."
        )


@pytest.mark.parametrize("approach,plugin_name", [
    ("4", "approach4"),
    ("5", "approach5"),
    ("6", "approach6"),
])
async def test_s16_gateway_approach_alice_travel_approve_expense_refused(
    approach, plugin_name, tmp_path,
):
    """
    S16 / approaches 4-6: alice via travel-assistant calls ask_payments_agent
    → /gateway/a2a/payments-agent. Cedar policy denies approve_expense for
    alice. Logged rule at gateway, not an error.

    FAILS now: /control/set_model_turns and /gateway/a2a/payments-agent are
    not implemented.
    """
    with _tcp_server(tmp_path, approach=approach, gateway_plugin=plugin_name) as (base_url, log_path, hook_log_path):
        await _run_first_agent(
            approach=approach,
            base_url=base_url,
            user="alice",
            calling_agent="travel-assistant",
            task="Please approve expense exp-001",
            first_agent_turns=[
                {"tool": "ask_payments_agent", "input": {"request": "approve expense exp-001"}},
                {"text": "Approval refused."},
            ],
            payments_agent_turns=[
                {"tool": "approve_expense", "input": {"expense_id": "exp-001"}},
                {"text": "Refused."},
            ],
            use_gateway=True,
        )

        async with httpx.AsyncClient(base_url=base_url) as ctrl:
            ledger = (await ctrl.get("/control/ledger")).json()
        approved = [e for e in ledger if e.get("action_type") == "expense_approved"]
        assert not approved, (
            f"approve_expense executed for alice (approach {approach} S16) — should be denied."
        )
        decisions = _decisions(log_path)
        denied = [d for d in decisions if d.get("decision") == "deny"]
        assert denied, f"No deny decision logged (approach {approach} S16)."
        last_deny = denied[-1]
        assert last_deny["rule"] == "SCOPE", (
            f"Expected SCOPE denial for approve_expense (approach {approach} S16); "
            f"got rule={last_deny.get('rule')!r}. "
            "The narrowed token must have empty scope so SCOPE fires before the plugin."
        )


# ---------------------------------------------------------------------------
# ── N1: token scope narrowing and actor chain at the tool server ────────────
# ---------------------------------------------------------------------------

async def test_n1_s15_token_scope_narrowed_to_expenses_approve(http_client):
    """
    N1 / S15: the tool-server token payments-agent presents must have:
      scope = {"expenses:approve"}  (payments:pay absent — narrowed away)
      act = {sub: payments-agent, act: {sub: expense-assistant}}

    expense-assistant's real scopes (from runner._AGENT_SCOPES):
      [expenses:read, expenses:submit, expenses:approve, agents:payments]
    payments-agent's agent token scopes:
      [payments:pay, expenses:approve]
    Intersection = {expenses:approve} — payments:pay is absent so the
    gateway must deny pay_vendor with SCOPE before the plugin runs.

    FAILS now: agents:payments absent from FIXED_SCOPES → delegation token
    cannot carry agents:payments (filtered out during issue).
    """
    import jwt as _jwt

    deleg_tok = await _issue_delegation_token(
        http_client,
        user="alice",
        calling_agent="expense-assistant",
        scope=list(_AGENT_SCOPES["expense-assistant"]),
    )
    tool_tok = await _issue_tool_server_token(http_client, delegation_token=deleg_tok)
    claims = _jwt.decode(tool_tok, options={"verify_signature": False})

    scope_set = set(claims.get("scope", "").split()) - {""}
    assert scope_set == {"expenses:approve"}, (
        f"Expected tool-server token scope {{expenses:approve}}, got {scope_set!r}. "
        "Scope narrowing: expense-assistant has no payments:pay, so it "
        "cannot survive the exchange to the tool-server token."
    )

    act = claims.get("act", {})
    assert act.get("sub") == "payments-agent", (
        f"Expected act.sub='payments-agent', got {act.get('sub')!r}"
    )
    inner = act.get("act", {})
    assert inner.get("sub") == "expense-assistant", (
        f"Expected act.act.sub='expense-assistant', got {inner.get('sub')!r}"
    )


async def test_n1_s16_token_scope_empty(http_client):
    """
    N1 / S16: the tool-server token payments-agent presents must have:
      scope = ""  (empty — no scopes survive narrowing)
      act = {sub: payments-agent, act: {sub: travel-assistant}}

    travel-assistant's real scopes:
      [travel:book, expenses:submit, agents:payments]
    payments-agent's agent token scopes:
      [payments:pay, expenses:approve]
    Intersection = {} (empty) — approve_expense requires expenses:approve
    which is missing, so the gateway must deny with SCOPE.

    FAILS now: agents:payments absent from FIXED_SCOPES.
    """
    import jwt as _jwt

    deleg_tok = await _issue_delegation_token(
        http_client,
        user="alice",
        calling_agent="travel-assistant",
        scope=list(_AGENT_SCOPES["travel-assistant"]),
    )
    tool_tok = await _issue_tool_server_token(http_client, delegation_token=deleg_tok)
    claims = _jwt.decode(tool_tok, options={"verify_signature": False})

    scope_set = set(claims.get("scope", "").split()) - {""}
    assert scope_set == set(), (
        f"Expected empty tool-server token scope, got {scope_set!r}. "
        "travel-assistant's scopes share no intersection with payments-agent's."
    )

    act = claims.get("act", {})
    assert act.get("sub") == "payments-agent", (
        f"Expected act.sub='payments-agent', got {act.get('sub')!r}"
    )
    inner = act.get("act", {})
    assert inner.get("sub") == "travel-assistant", (
        f"Expected act.act.sub='travel-assistant', got {inner.get('sub')!r}"
    )


# ---------------------------------------------------------------------------
# ── Acceptance: positive control — erin's payment through two-agent chain ───
# ---------------------------------------------------------------------------

async def test_positive_erin_payment_via_two_agent_chain(tmp_path):
    """
    Positive control (approach 1): erin (finance role) via expense-assistant
    calls ask_payments_agent → payments-agent. The payment IS executed.
    The decision log shows BOTH expense-assistant AND payments-agent in the
    agent chain.

    FAILS now: /control/set_model_turns and /a2a/payments-agent are not
    implemented; agents:payments not in FIXED_SCOPES.
    """
    with _tcp_server(tmp_path) as (base_url, log_path, hook_log_path):
        await _run_first_agent(
            approach="1",
            base_url=base_url,
            user="erin",
            calling_agent="expense-assistant",
            task="Pay vendor acme-hotels £100 reference ref-pos1",
            first_agent_turns=[
                {"tool": "ask_payments_agent", "input": {"request": "pay vendor acme-hotels 100 ref-pos1"}},
                {"text": "Payment sent."},
            ],
            payments_agent_turns=[
                {"tool": "pay_vendor", "input": {"vendor": "acme-hotels", "amount": 100.0, "reference": "ref-pos1"}},
                {"text": "Payment sent."},
            ],
        )

        async with httpx.AsyncClient(base_url=base_url) as ctrl:
            ledger = (await ctrl.get("/control/ledger")).json()
        vendor_paid = [e for e in ledger if e.get("action_type") == "vendor_paid"]
        assert vendor_paid, (
            "pay_vendor was NOT executed for erin. "
            "Positive control: erin (finance) must be allowed to pay vendors."
        )

        decisions = _decisions(log_path)
        pay_decisions = [d for d in decisions if d.get("tool") == "pay_vendor"]
        assert pay_decisions, "No pay_vendor decision logged."
        d = pay_decisions[-1]
        agent = d.get("agent")
        assert isinstance(agent, list), (
            f"Expected agent to be a list (full chain), got {agent!r}."
        )
        assert "expense-assistant" in agent, "expense-assistant missing from logged chain."
        assert "payments-agent" in agent, "payments-agent missing from logged chain."
        assert d.get("user") == "erin", f"user should be 'erin', got {d.get('user')!r}."


async def test_positive_erin_gateway_approach4_payment_executes(tmp_path):
    """
    Positive control (approach 4): erin via expense-assistant calls
    ask_payments_agent → /gateway/a2a/payments-agent → pay_vendor executes.
    Both agents logged in chain.

    FAILS now: /control/set_model_turns and /gateway/a2a/payments-agent are
    not implemented; agents:payments not in FIXED_SCOPES.
    """
    # Positive control: add payments:pay so the narrowed tool-server token
    # carries it and the gateway SCOPE check passes; proves the plumbing
    # works with adequate scopes (real narrowing tested in N1 tests).
    pos_scope = list(_AGENT_SCOPES["expense-assistant"]) + ["payments:pay"]
    with _tcp_server(tmp_path, gateway_plugin="approach4") as (base_url, log_path, hook_log_path):
        await _run_first_agent(
            approach="4",
            base_url=base_url,
            user="erin",
            calling_agent="expense-assistant",
            task="Pay vendor acme-hotels £100 reference ref-pos-gw",
            first_agent_turns=[
                {"tool": "ask_payments_agent", "input": {"request": "pay vendor acme-hotels 100 ref-pos-gw"}},
                {"text": "Payment sent."},
            ],
            payments_agent_turns=[
                {"tool": "pay_vendor", "input": {"vendor": "acme-hotels", "amount": 100.0, "reference": "ref-pos-gw"}},
                {"text": "Payment sent."},
            ],
            scope=pos_scope,
            use_gateway=True,
        )

        async with httpx.AsyncClient(base_url=base_url) as ctrl:
            ledger = (await ctrl.get("/control/ledger")).json()
        vendor_paid = [e for e in ledger if e.get("action_type") == "vendor_paid"]
        assert vendor_paid, (
            "pay_vendor not executed for erin via gateway (approach 4). "
            "Positive control must succeed."
        )
        decisions = _decisions(log_path)
        pay_dec = [d for d in decisions if d.get("tool") in ("pay_vendor", "ask_payments_agent")]
        assert pay_dec, "No relevant decision logged."
        agents_in_chain = {
            a
            for d in pay_dec
            for a in (d.get("agent") if isinstance(d.get("agent"), list) else [d.get("agent")])
            if a
        }
        assert "expense-assistant" in agents_in_chain, (
            "expense-assistant not in logged agent chain."
        )


# ---------------------------------------------------------------------------
# ── Token safety: no JWT token string in transcripts ────────────────────────
# ---------------------------------------------------------------------------

async def test_tokens_not_in_transcript_via_scripted_model(tmp_path):
    """
    JWT tokens must never appear in either agent's transcript. This test:
    1. Runs the full two-agent chain via _run_first_agent.
    2. Scans the first agent's Strands message list for JWT strings.
    3. Fetches payments-agent's transcript via /control/last_transcript and
       scans it too.

    FAILS now:
    – /control/set_model_turns is not implemented (404)
    – /control/last_transcript is not implemented (404)
    """
    with _tcp_server(tmp_path) as (base_url, log_path, hook_log_path):
        result = await _run_first_agent(
            approach="1",
            base_url=base_url,
            user="alice",
            calling_agent="expense-assistant",
            task="Pay vendor acme-hotels £100",
            first_agent_turns=[
                {"tool": "ask_payments_agent", "input": {"request": "pay vendor acme-hotels 100"}},
                {"text": "Payment refused."},
            ],
            payments_agent_turns=[
                {"text": "I cannot authorise payments for non-finance users."},
            ],
        )

        jwt_pattern = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+")

        first_transcript = json.dumps(result.get("messages", []))
        assert not jwt_pattern.search(first_transcript), (
            "JWT token found in first agent's (expense-assistant) transcript. "
            "Tokens must only appear in Authorization headers, never in messages."
        )

        async with httpx.AsyncClient(base_url=base_url) as ctrl:
            r = await ctrl.get("/control/last_transcript")
        assert r.status_code == 200, (
            f"/control/last_transcript returned {r.status_code}. "
            "This endpoint must be added to domain/server.py as part of step 5b "
            "so token-safety tests can inspect payments-agent's message history."
        )
        pa_transcript = json.dumps(r.json())
        assert not jwt_pattern.search(pa_transcript), (
            "JWT token found in payments-agent's transcript. "
            "Tokens must only appear in Authorization headers, never in messages."
        )


# ---------------------------------------------------------------------------
# ── Testing flag: /control/set_model_turns gated by SERVER_TEST_MODE ────────
# ---------------------------------------------------------------------------

async def test_set_model_turns_refused_without_testing_flag(
    running_app, http_client, monkeypatch,
):
    """
    POST /control/set_model_turns must return 403 when SERVER_TEST_MODE is
    not set. The runner strips this env var so the endpoint is never
    accessible in production.

    FAILS now: /control/set_model_turns is not implemented (returns 404
    instead of 403). Once implemented, it must check SERVER_TEST_MODE.
    """
    monkeypatch.delenv("SERVER_TEST_MODE", raising=False)
    resp = await http_client.post(
        "/control/set_model_turns",
        json={"agent": "payments-agent", "turns": [{"text": "Done."}]},
    )
    assert resp.status_code == 403, (
        f"Expected 403 from /control/set_model_turns without SERVER_TEST_MODE, "
        f"got {resp.status_code}. "
        "The endpoint must be gated by the SERVER_TEST_MODE env var so the "
        "runner's start path can never reach it."
    )


def test_runner_strips_server_test_mode(tmp_path, monkeypatch):
    """
    domain_server() (the runner's server context manager) must strip
    SERVER_TEST_MODE from the child process environment so /control/
    set_model_turns is never accessible during production runs.

    FAILS now: runner/_server.py does not strip SERVER_TEST_MODE from the
    child env (only _GATEWAY_TESTING and GATEWAY_TEST_PLUGIN are stripped).
    """
    import httpx as _httpx
    from runner._server import domain_server

    monkeypatch.setenv("SERVER_TEST_MODE", "1")
    decision_log = str(tmp_path / "decisions.jsonl")
    with domain_server(decision_log) as (_port, base_url):
        resp = _httpx.post(
            f"{base_url}/control/set_model_turns",
            json={"agent": "payments-agent", "turns": [{"text": "Done."}]},
            timeout=5.0,
        )
        assert resp.status_code == 403, (
            f"domain_server() did not strip SERVER_TEST_MODE: "
            f"/control/set_model_turns returned {resp.status_code} (expected 403). "
            "Add SERVER_TEST_MODE to the stripped keys in runner/_server.py."
        )


# ---------------------------------------------------------------------------
# ── Kit byte-identity: step 5b must not alter the approach3 kit ─────────────
# ---------------------------------------------------------------------------

def test_kit_byte_identical_after_step5b():
    """
    The approach3 kit must remain byte-identical to the frozen reference
    (from the approach3-pipeline-frozen tag) after step-5b changes.

    This delegates to the same check as test_step5a.py's
    test_kit_byte_identical_to_frozen so that any accidental change to
    approach3 domain files causes an immediate failure here too.

    FAILS now if kit_snapshot is not committed — same as test_step5a.
    """
    from tests.test_step5a import test_kit_byte_identical_to_frozen
    test_kit_byte_identical_to_frozen()


# ---------------------------------------------------------------------------
# ── ScriptedModel smoke test (verifies stream format) ───────────────────────
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scripted_model_stream_tool_use():
    """
    ScriptedModel.stream() must emit events in the order that Strands'
    event loop expects for a tool_use turn.

    This test runs independently of all plumbing and verifies the model
    stub used in acceptance tests is correctly formatted.

    Passes immediately once the ScriptedModel class is defined (above).
    Used as a design contract — if this breaks, the acceptance tests
    cannot run.
    """
    model = ScriptedModel([
        {"tool": "pay_vendor", "input": {"vendor": "acme-hotels", "amount": 100.0, "reference": "ref-1"}},
    ])
    events = []
    async for ev in model.stream(messages=[]):
        events.append(ev)

    keys = [list(e.keys())[0] for e in events]
    assert keys[0] == "messageStart"
    assert "contentBlockStart" in keys
    assert "contentBlockDelta" in keys
    assert "contentBlockStop" in keys
    assert keys[-2] == "messageStop"
    assert keys[-1] == "metadata"

    # Verify the tool name appears in the contentBlockStart
    cb_start = next(e for e in events if "contentBlockStart" in e)
    tool_use = cb_start["contentBlockStart"]["start"]["toolUse"]
    assert tool_use["name"] == "pay_vendor"

    # Verify the input is valid JSON in the delta
    cb_delta = next(e for e in events if "contentBlockDelta" in e)
    delta_input = cb_delta["contentBlockDelta"]["delta"]["toolUse"]["input"]
    parsed = json.loads(delta_input)
    assert parsed["vendor"] == "acme-hotels"


@pytest.mark.asyncio
async def test_scripted_model_stream_text():
    """ScriptedModel text turn emits correct events."""
    model = ScriptedModel([{"text": "Payment refused."}])
    events = []
    async for ev in model.stream(messages=[]):
        events.append(ev)

    keys = [list(e.keys())[0] for e in events]
    assert keys[0] == "messageStart"
    assert "contentBlockDelta" in keys
    stop = next(e for e in events if "messageStop" in e)
    assert stop["messageStop"]["stopReason"] == "end_turn"
