"""Gateway core (task 2a).

Runs in the same process as the tool server.  Agents in gateway mode
connect to /gateway/mcp.  The gateway enforces:
  IDENTITY → GRANT → P6 → SCOPE → policy plugin
then delegates tool execution to the server.
"""
import datetime
import hashlib
import inspect
import json
import uuid
from typing import Any, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse

from domain import simclock, tokens
from domain.scopes import TOOL_SCOPE_MAP

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_active: bool = False
_grants: dict[str, list[str]] = {}
_fingerprints: dict[str, str] = {}
_scope_map: dict[str, str] = {}
_policy_plugin: Callable | None = None
_prev_authorise: Callable | None = None
_skip_token_expiry: bool = False


def is_active() -> bool:
    return _active


# ---------------------------------------------------------------------------
# Install / remove
# ---------------------------------------------------------------------------

def install_gateway(
    *,
    grants: dict[str, list[str]],
    fingerprints: dict[str, str],
    scope_map: dict[str, str] | None = None,
    policy_plugin: Callable | None,
) -> None:
    global _active, _grants, _fingerprints, _scope_map, _policy_plugin, _prev_authorise, _skip_token_expiry

    if policy_plugin is None:
        raise ValueError("gateway mode requires a policy plugin")

    import domain.server as srv
    _prev_authorise = srv.authorise
    srv.authorise = _gateway_bypass_authorise

    _grants = dict(grants)
    _fingerprints = dict(fingerprints)
    _scope_map = dict(scope_map) if scope_map is not None else dict(TOOL_SCOPE_MAP)
    _policy_plugin = policy_plugin
    _skip_token_expiry = getattr(policy_plugin, "skip_token_expiry", False)
    _active = True

    if hasattr(policy_plugin, "process_clock_advance"):
        if policy_plugin.process_clock_advance not in srv._clock_advance_listeners:
            srv._clock_advance_listeners.append(policy_plugin.process_clock_advance)
    if hasattr(policy_plugin, "on_recovery"):
        if policy_plugin.on_recovery not in srv._recovery_listeners:
            srv._recovery_listeners.append(policy_plugin.on_recovery)


def remove_gateway() -> None:
    global _active, _grants, _fingerprints, _scope_map, _policy_plugin, _prev_authorise, _skip_token_expiry

    import domain.server as srv
    if _prev_authorise is not None:
        srv.authorise = _prev_authorise

    if _policy_plugin is not None:
        if hasattr(_policy_plugin, "process_clock_advance"):
            try:
                srv._clock_advance_listeners.remove(_policy_plugin.process_clock_advance)
            except ValueError:
                pass
        if hasattr(_policy_plugin, "on_recovery"):
            try:
                srv._recovery_listeners.remove(_policy_plugin.on_recovery)
            except ValueError:
                pass

    _active = False
    _grants = {}
    _fingerprints = {}
    _scope_map = {}
    _policy_plugin = None
    _prev_authorise = None
    _skip_token_expiry = False


# ---------------------------------------------------------------------------
# Bypass authorise — replaces domain.server.authorise in gateway mode
# ---------------------------------------------------------------------------

def _gateway_bypass_authorise(
    caller: dict, tool_name: str, arguments: dict,
) -> dict:
    return {
        "decision": "deny",
        "rule": "GATEWAY_BYPASS",
        "reason": "gateway mode active; use /gateway/mcp",
    }


# ---------------------------------------------------------------------------
# Fingerprint computation (SHA-256 of name + description + parameters)
# ---------------------------------------------------------------------------

def _compute_fingerprint(name: str, description: str, parameters: dict) -> str:
    data = {"description": description, "name": name, "parameters": parameters}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _current_fingerprint(tool_name: str) -> str | None:
    from domain.server import mcp
    tool = mcp._tool_manager.get_tool(tool_name)
    if tool is None:
        return None
    return _compute_fingerprint(tool.name, tool.description, tool.parameters)


# ---------------------------------------------------------------------------
# Gateway logging
# ---------------------------------------------------------------------------

def _append_log(entry: dict) -> None:
    from domain.server import _append
    _append(entry)


def _gw_decision_line(
    call_id: str, caller: dict, tool_name: str,
    arguments: dict, decision: dict,
) -> None:
    entry: dict = {
        "type": "decision",
        "call_id": call_id,
        "layer": "gateway",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sim_time": simclock.now(),
        "user": caller.get("user"),
        "agent": caller.get("agent"),
        "tool": tool_name,
        "arguments": arguments,
        "decision": decision["decision"],
        "rule": decision["rule"],
        "reason": decision["reason"],
        "central_called": decision.get("central_called", False),
        "central_duration_ms": decision.get("central_duration_ms", 0),
    }
    for key in ("central_copy_version", "revocation_applied"):
        if key in decision:
            entry[key] = decision[key]
    _append_log(entry)


def _gw_outcome_line(
    call_id: str, *, executed: bool, error: str | None = None,
) -> None:
    _append_log({
        "type": "outcome",
        "call_id": call_id,
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sim_time": simclock.now(),
        "executed": executed,
        "error": error,
    })


# ---------------------------------------------------------------------------
# Check pipeline: IDENTITY → GRANT → P6 → SCOPE → policy plugin
# ---------------------------------------------------------------------------

async def _check_pipeline(
    auth_header: str | None, tool_name: str, arguments: dict,
) -> tuple[dict, dict, str]:
    """Returns (caller, decision, call_id)."""
    call_id = str(uuid.uuid4())
    caller: dict = {"user": None, "agent": None}

    # 1. IDENTITY
    if not auth_header or not auth_header.startswith("Bearer "):
        decision = {
            "decision": "deny", "rule": "IDENTITY",
            "reason": "missing Bearer token",
        }
        return caller, decision, call_id

    try:
        claims = tokens.verify_bearer(auth_header[7:], check_expiry=not _skip_token_expiry)
    except Exception as exc:
        decision = {
            "decision": "deny", "rule": "IDENTITY",
            "reason": str(exc),
        }
        return caller, decision, call_id

    caller = tokens.claims_to_caller(claims)

    # 2. GRANT
    agent = caller.get("agent")
    agent_name = (
        agent if isinstance(agent, str)
        else agent[0] if isinstance(agent, list) and agent
        else None
    )

    if agent_name not in _grants:
        decision = {
            "decision": "deny", "rule": "GRANT",
            "reason": f"agent {agent_name!r} not in grants",
        }
        return caller, decision, call_id

    if tool_name not in _grants[agent_name]:
        decision = {
            "decision": "deny", "rule": "GRANT",
            "reason": f"tool {tool_name!r} not granted to {agent_name!r}",
        }
        return caller, decision, call_id

    # 3. P6 — tool integrity (fingerprint)
    expected_fp = _fingerprints.get(tool_name)
    if expected_fp is None:
        decision = {
            "decision": "deny", "rule": "P6",
            "reason": f"no reviewed fingerprint for {tool_name!r}",
        }
        return caller, decision, call_id

    actual_fp = _current_fingerprint(tool_name)
    if actual_fp != expected_fp:
        decision = {
            "decision": "deny", "rule": "P6",
            "reason": f"fingerprint mismatch for {tool_name!r}",
        }
        return caller, decision, call_id

    # 4. SCOPE
    required_scope = _scope_map.get(tool_name)
    if required_scope is None:
        decision = {
            "decision": "deny", "rule": "SCOPE",
            "reason": f"no required scope for {tool_name!r}",
        }
        return caller, decision, call_id

    token_scopes = set(claims.get("scope", "").split())
    if required_scope not in token_scopes:
        decision = {
            "decision": "deny", "rule": "SCOPE",
            "reason": f"missing scope {required_scope!r}",
        }
        return caller, decision, call_id

    # 5. Policy plugin (supports both sync and async plugins)
    agent_chain = caller.get("agent")
    if isinstance(agent_chain, str):
        agent_chain = [agent_chain]

    try:
        result = _policy_plugin(claims, agent_chain, tool_name, arguments)
        if inspect.iscoroutine(result):
            result = await result
    except Exception as exc:
        decision = {"decision": "error", "rule": None, "reason": str(exc)}
        return caller, decision, call_id

    decision = {
        "decision": result["decision"],
        "rule": result.get("rule"),
        "reason": result.get("reason", ""),
        "central_called": result.get("central_called", False),
        "central_duration_ms": result.get("central_duration_ms", 0),
    }
    for key in ("central_copy_version", "revocation_applied"):
        if key in result:
            decision[key] = result[key]
    return caller, decision, call_id


# ---------------------------------------------------------------------------
# tools/list filtering
# ---------------------------------------------------------------------------

def _filtered_tools(auth_header: str | None) -> list[dict]:
    """Return tools granted to the authenticated agent with matching fingerprints."""
    agent_name = None
    if auth_header and auth_header.startswith("Bearer "):
        try:
            claims = tokens.verify_bearer(auth_header[7:])
            caller = tokens.claims_to_caller(claims)
            agent = caller.get("agent")
            agent_name = (
                agent if isinstance(agent, str)
                else agent[0] if isinstance(agent, list) and agent
                else None
            )
        except Exception:
            pass

    granted = set(_grants.get(agent_name, []))

    from domain.server import mcp
    all_tools = mcp._tool_manager._tools

    result = []
    for name, tool in all_tools.items():
        if name not in granted:
            continue
        expected_fp = _fingerprints.get(name)
        if expected_fp is None:
            continue
        actual_fp = _current_fingerprint(name)
        if actual_fp != expected_fp:
            continue
        result.append({
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.parameters,
        })
    return result


# ---------------------------------------------------------------------------
# JSON-RPC handler for /gateway/mcp
# ---------------------------------------------------------------------------

async def handle_mcp(request: Request) -> JSONResponse:
    if not _active:
        return JSONResponse(
            {"jsonrpc": "2.0", "error": {"code": -32000, "message": "gateway not active"}},
            status_code=404,
        )

    body = await request.json()
    method = body.get("method")
    req_id = body.get("id")
    params = body.get("params", {})

    if method == "initialize":
        session_id = str(uuid.uuid4())
        resp = JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "expense-server-gateway", "version": "0.1"},
            },
        })
        resp.headers["mcp-session-id"] = session_id
        return resp

    if method == "tools/list":
        auth = request.headers.get("authorization")
        tools = _filtered_tools(auth)
        return JSONResponse({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": tools},
        })

    if method == "tools/call":
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})
        auth = request.headers.get("authorization")

        caller, decision, call_id = await _check_pipeline(auth, tool_name, arguments)
        _gw_decision_line(call_id, caller, tool_name, arguments, decision)

        if decision["decision"] in ("deny", "error"):
            _gw_outcome_line(call_id, executed=False, error=decision["reason"])
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": decision["reason"]}],
                    "isError": True,
                },
            })

        try:
            from domain.server import execute_tool
            result = execute_tool(tool_name, arguments, caller)
            _gw_outcome_line(call_id, executed=True)
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result)}],
                },
            })
        except Exception as exc:
            _gw_outcome_line(call_id, executed=False, error=str(exc))
            return JSONResponse({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": str(exc)}],
                    "isError": True,
                },
            })

    return JSONResponse({
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"unknown method {method!r}"},
    })
