import datetime
import json
import os
import pathlib
import uuid
from typing import Any, Callable

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from domain import simclock, tokens
from domain.identity import canonicalise
from domain.state import reset, state

# ---------------------------------------------------------------------------
# Revocation listeners and reset callbacks (approach 6)
# ---------------------------------------------------------------------------

_revocation_listeners: list = []
_reset_callbacks: list = []
_clock_advance_listeners: list = []
_recovery_listeners: list = []


def register_revocation_listener(fn) -> None:
    if fn not in _revocation_listeners:
        _revocation_listeners.append(fn)


def deregister_revocation_listener(fn) -> None:
    try:
        _revocation_listeners.remove(fn)
    except ValueError:
        pass


def register_reset_callback(fn) -> None:
    if fn not in _reset_callbacks:
        _reset_callbacks.append(fn)


def deregister_reset_callback(fn) -> None:
    try:
        _reset_callbacks.remove(fn)
    except ValueError:
        pass


# ---------------------------------------------------------------------------
# authorise callback
# ---------------------------------------------------------------------------

def _default_authorise(caller: dict, tool_name: str, arguments: dict) -> dict:
    return {"decision": "none", "rule": None, "reason": "no checks"}


# Enforcement modules may reassign this at startup.
authorise: Callable[[dict, str, dict], dict] = _default_authorise

# ---------------------------------------------------------------------------
# decision log
# ---------------------------------------------------------------------------

def _log_path() -> pathlib.Path:
    p = pathlib.Path(os.environ.get("DECISION_LOG", "results/decisions.jsonl"))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _append(entry: dict) -> None:
    with _log_path().open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def _decision_line(call_id: str, caller: dict, tool_name: str, arguments: dict, decision: dict) -> None:
    _append(
        {
            "type":      "decision",
            "call_id":   call_id,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "sim_time":  simclock.now(),
            "run_id":    state.run_id,
            "scenario":  state.scenario,
            "arm":       state.arm,
            "user":      caller.get("user"),
            "agent":     caller.get("agent"),
            "tool":      tool_name,
            "arguments": arguments,
            "decision":  decision["decision"],
            "rule":      decision["rule"],
            "reason":    decision["reason"],
        }
    )


def _outcome_line(call_id: str, *, executed: bool, error: str | None) -> None:
    _append(
        {
            "type":      "outcome",
            "call_id":   call_id,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "sim_time":  simclock.now(),
            "run_id":    state.run_id,
            "scenario":  state.scenario,
            "arm":       state.arm,
            "executed":  executed,
            "error":     error,
        }
    )

# ---------------------------------------------------------------------------
# _run_tool -- every tool goes through this
# ---------------------------------------------------------------------------

async def _run_tool(
    ctx: Context,
    tool_name: str,
    arguments: dict,
    action_fn: Callable[[dict], Any],
) -> Any:
    call_id = str(uuid.uuid4())
    caller: dict = {"user": None, "agent": None}

    # Verify Bearer token; identity comes only from the token
    auth = ctx.request_context.request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        decision = {"decision": "deny", "rule": "IDENTITY", "reason": "missing Bearer token"}
        _decision_line(call_id, caller, tool_name, arguments, decision)
        _outcome_line(call_id, executed=False, error="missing Bearer token")
        raise PermissionError("missing Bearer token")

    try:
        claims = tokens.verify_bearer(auth[7:])
    except Exception as exc:
        reason = str(exc)
        decision = {"decision": "deny", "rule": "IDENTITY", "reason": reason}
        _decision_line(call_id, caller, tool_name, arguments, decision)
        _outcome_line(call_id, executed=False, error=reason)
        raise PermissionError(reason) from exc

    caller = tokens.claims_to_caller(claims)

    try:
        decision = authorise(caller, tool_name, arguments)
    except Exception as exc:
        decision = {"decision": "error", "rule": None, "reason": str(exc)}
        _decision_line(call_id, caller, tool_name, arguments, decision)
        _outcome_line(call_id, executed=False, error=str(exc))
        raise PermissionError(str(exc)) from exc

    _decision_line(call_id, caller, tool_name, arguments, decision)

    if decision["decision"] == "deny":
        _outcome_line(call_id, executed=False, error=decision["reason"])
        raise PermissionError(decision["reason"])

    try:
        result = action_fn(caller)
    except Exception as exc:
        _outcome_line(call_id, executed=False, error=str(exc))
        raise

    _outcome_line(call_id, executed=True, error=None)
    return result

# ---------------------------------------------------------------------------
# FastMCP instance
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "expense-server",
    host="127.0.0.1",
    port=8765,
    streamable_http_path="/mcp",
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=False
    ),
)

# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

@mcp.tool()
async def read_receipt(receipt_id: str, ctx: Context) -> dict:
    """Read a receipt by ID."""
    return await _run_tool(
        ctx, "read_receipt", {"receipt_id": receipt_id},
        lambda _caller: _do_read_receipt(receipt_id),
    )


@mcp.tool()
async def submit_expense(
    claimant: str,
    amount: float,
    description: str,
    ctx: Context,
    receipt_id: str | None = None,
    approval_ref: str | None = None,
) -> dict:
    """Submit an expense claim."""
    claimant = canonicalise(claimant)
    args = {
        "claimant": claimant, "amount": amount, "description": description,
        "receipt_id": receipt_id, "approval_ref": approval_ref,
    }
    return await _run_tool(
        ctx, "submit_expense", args,
        lambda _caller: _do_submit_expense(claimant, amount, description, receipt_id, approval_ref),
    )


@mcp.tool()
async def approve_expense(expense_id: str, ctx: Context) -> dict:
    """Approve an expense."""
    return await _run_tool(
        ctx, "approve_expense", {"expense_id": expense_id},
        lambda caller: _do_approve_expense(expense_id, approver=caller["user"]),
    )


@mcp.tool()
async def book_travel(traveller: str, details: str, ctx: Context) -> dict:
    """Book travel for a traveller."""
    traveller = canonicalise(traveller)
    return await _run_tool(
        ctx, "book_travel", {"traveller": traveller, "details": details},
        lambda _caller: _do_book_travel(traveller, details),
    )


@mcp.tool()
async def pay_vendor(vendor: str, amount: float, reference: str, ctx: Context) -> dict:
    """Pay a vendor."""
    vendor = canonicalise(vendor)
    return await _run_tool(
        ctx, "pay_vendor", {"vendor": vendor, "amount": amount, "reference": reference},
        lambda _caller: _do_pay_vendor(vendor, amount, reference),
    )


# Keep a reference to the original function and its description for reset.
_pay_vendor_fn = pay_vendor
_PAY_VENDOR_DESC = "Pay a vendor."

# ---------------------------------------------------------------------------
# domain action helpers
# ---------------------------------------------------------------------------

def _do_read_receipt(receipt_id: str) -> dict:
    receipt = state.receipts.get(receipt_id)
    if receipt is None:
        raise ValueError(f"receipt {receipt_id!r} not found")
    return receipt


def _do_submit_expense(
    claimant: str,
    amount: float,
    description: str,
    receipt_id: str | None,
    approval_ref: str | None,
) -> dict:
    if claimant not in state.users:
        raise ValueError("unknown user")
    expense_id = f"exp-{len(state.expenses) + 1:03d}"
    expense = {
        "id":           expense_id,
        "claimant":     claimant,
        "amount":       amount,
        "currency":     "GBP",
        "description":  description,
        "receipt_id":   receipt_id,
        "approval_ref": approval_ref,
        "status":       "pending",
    }
    state.expenses[expense_id] = expense
    state.record(
        "expense_submitted",
        expense_id=expense_id,
        claimant=claimant,
        amount=amount,
        currency="GBP",
        description=description,
    )
    return expense


def _do_approve_expense(expense_id: str, approver: str) -> dict:
    expense = state.expenses.get(expense_id)
    if expense is None:
        raise ValueError(f"expense {expense_id!r} not found")
    expense["status"] = "approved"
    state.record("expense_approved", expense_id=expense_id, approver=approver)
    return expense


def _do_book_travel(traveller: str, details: str) -> dict:
    if traveller not in state.users:
        raise ValueError("unknown user")
    booking = {"traveller": traveller, "details": details, "status": "booked"}
    state.record("travel_booked", traveller=traveller, details=details)
    return booking


def _do_pay_vendor(vendor: str, amount: float, reference: str) -> dict:
    payment = {"vendor": vendor, "amount": amount, "reference": reference, "status": "paid"}
    state.record("vendor_paid", vendor=vendor, amount=amount, reference=reference)
    return payment


def execute_tool(tool_name: str, arguments: dict, caller: dict) -> Any:
    """Execute a tool bypassing MCP protocol."""
    if tool_name == "read_receipt":
        return _do_read_receipt(arguments["receipt_id"])
    elif tool_name == "submit_expense":
        claimant = canonicalise(arguments.get("claimant", ""))
        return _do_submit_expense(
            claimant, arguments["amount"], arguments["description"],
            arguments.get("receipt_id"), arguments.get("approval_ref"),
        )
    elif tool_name == "approve_expense":
        return _do_approve_expense(
            arguments["expense_id"], approver=caller["user"],
        )
    elif tool_name == "book_travel":
        traveller = canonicalise(arguments.get("traveller", ""))
        return _do_book_travel(traveller, arguments["details"])
    elif tool_name == "pay_vendor":
        vendor = canonicalise(arguments.get("vendor", ""))
        return _do_pay_vendor(
            vendor, arguments["amount"], arguments["reference"],
        )
    elif tool_name == "export_all_expenses":
        return {"expenses": list(state.expenses.values())}
    else:
        raise ValueError(f"unknown tool: {tool_name}")

# ---------------------------------------------------------------------------
# control endpoints
# ---------------------------------------------------------------------------

@mcp.custom_route("/control/reset", methods=["POST"])
async def _ctrl_reset(request: Request) -> JSONResponse:
    # Restore tools before resetting state.
    if mcp._tool_manager.get_tool("export_all_expenses"):
        mcp.remove_tool("export_all_expenses")
    mcp.remove_tool("pay_vendor")
    mcp.add_tool(_pay_vendor_fn, name="pay_vendor", description=_PAY_VENDOR_DESC)
    reset()  # also resets simclock via state.reset()
    for fn in _reset_callbacks:
        fn()
    # --- ARM D BEGIN ---
    if os.environ.get("ENFORCEMENT") == "cedar":
        from arms.d_boundary import pep as _pep
        _pep.reset_policy()
    # --- ARM D END ---
    return JSONResponse({"ok": True})


@mcp.custom_route("/control/clock/set", methods=["POST"])
async def _ctrl_clock_set(request: Request) -> JSONResponse:
    body = await request.json()
    simclock.set_time(float(body["ts"]))
    return JSONResponse({"sim_time": simclock.now()})


@mcp.custom_route("/control/clock/advance", methods=["POST"])
async def _ctrl_clock_advance(request: Request) -> JSONResponse:
    body = await request.json()
    simclock.advance(float(body["seconds"]))
    now = simclock.now()
    # --- GATEWAY BEGIN ---
    from domain import gateway as _gw_mod
    if _gw_mod.is_active():
        from domain import central_publisher
        central_publisher.process_clock_advance(now)
    # --- GATEWAY END ---
    for fn in _clock_advance_listeners:
        fn(now)
    return JSONResponse({"sim_time": now})


@mcp.custom_route("/control/revoke-delegation", methods=["POST"])
async def _ctrl_revoke_delegation(request: Request) -> JSONResponse:
    body = await request.json()
    state.revoke_delegation(body["delegation_id"])
    return JSONResponse({"ok": True})


@mcp.custom_route("/control/set-limit", methods=["POST"])
async def _ctrl_set_limit(request: Request) -> JSONResponse:
    body = await request.json()
    state.expense_limit = int(body["limit"])
    return JSONResponse({"ok": True})


@mcp.custom_route("/control/add-tool", methods=["POST"])
async def _ctrl_add_tool(request: Request) -> JSONResponse:
    if mcp._tool_manager.get_tool("export_all_expenses"):
        return JSONResponse({"ok": True, "note": "already present"})

    async def export_all_expenses(ctx: Context) -> dict:
        """Export every expense record."""
        return await _run_tool(
            ctx, "export_all_expenses", {},
            lambda _caller: {"expenses": list(state.expenses.values())},
        )

    mcp.add_tool(export_all_expenses, name="export_all_expenses")
    return JSONResponse({"ok": True})


@mcp.custom_route("/control/poison-tool", methods=["POST"])
async def _ctrl_poison_tool(request: Request) -> JSONResponse:
    body = await request.json()
    new_desc = body["description"]
    mcp.remove_tool("pay_vendor")
    mcp.add_tool(_pay_vendor_fn, name="pay_vendor", description=new_desc)
    return JSONResponse({"ok": True})


@mcp.custom_route("/control/directory-down", methods=["POST"])
async def _ctrl_directory_down(request: Request) -> JSONResponse:
    body = await request.json()
    was_down = state.directory_down
    state.directory_down = bool(body["down"])
    if was_down and not state.directory_down:
        now = simclock.now()
        # --- GATEWAY BEGIN ---
        from domain import gateway as _gw_mod
        if _gw_mod.is_active():
            from domain import central_publisher
            central_publisher.on_recovery(now)
        # --- GATEWAY END ---
        for fn in _recovery_listeners:
            fn(now)
    return JSONResponse({"ok": True})


@mcp.custom_route("/control/central-decide-count", methods=["GET"])
async def _ctrl_central_decide_count(request: Request) -> JSONResponse:
    return JSONResponse({"count": state.central_decide_calls})


# --- ARM D BEGIN ---
@mcp.custom_route("/control/pdp-down", methods=["POST"])
async def _ctrl_pdp_down(request: Request) -> JSONResponse:
    body = await request.json()
    state.pdp_down = bool(body["down"])
    return JSONResponse({"ok": True})


@mcp.custom_route("/control/pdp-policy-change", methods=["POST"])
async def _ctrl_pdp_policy_change(request: Request) -> JSONResponse:
    body = await request.json()
    if os.environ.get("ENFORCEMENT") == "cedar":
        from arms.d_boundary import pep as _pep
        _pep.apply_in_memory_limit(int(body["expense_limit"]))
    return JSONResponse({"ok": True})
# --- ARM D END ---


@mcp.custom_route("/control/set-run", methods=["POST"])
async def _ctrl_set_run(request: Request) -> JSONResponse:
    body = await request.json()
    state.run_id  = body.get("run_id")
    state.scenario = body.get("scenario")
    state.arm     = body.get("arm")
    return JSONResponse({"ok": True})


@mcp.custom_route("/control/ledger", methods=["GET"])
async def _ctrl_ledger(request: Request) -> JSONResponse:
    return JSONResponse(state.ledger)

# ---------------------------------------------------------------------------
# identity issuing routes (harness-only, under /control/identity/)
# ---------------------------------------------------------------------------

@mcp.custom_route("/control/identity/agent-token", methods=["POST"])
async def _ctrl_issue_agent_token(request: Request) -> JSONResponse:
    body = await request.json()
    tok = tokens.issue_agent_token(
        sub=body["sub"],
        aud=body.get("aud", tokens.USER_ISSUER),
        scope=body.get("scope", list(tokens.FIXED_SCOPES)),
        lifetime=int(body.get("lifetime", tokens.DEFAULT_LIFETIME)),
    )
    return JSONResponse({"access_token": tok})


@mcp.custom_route("/control/identity/user-token", methods=["POST"])
async def _ctrl_issue_user_token(request: Request) -> JSONResponse:
    body = await request.json()
    tok = tokens.issue_user_token(
        sub=body["sub"],
        aud=body["aud"],
        scope=body.get("scope", list(tokens.FIXED_SCOPES)),
        lifetime=int(body.get("lifetime", tokens.DEFAULT_LIFETIME)),
    )
    return JSONResponse({"access_token": tok})

# ---------------------------------------------------------------------------
# identity exchange (RFC 8693) and JWKS
# ---------------------------------------------------------------------------

@mcp.custom_route("/identity/jwks", methods=["GET"])
async def _identity_jwks(request: Request) -> JSONResponse:
    return JSONResponse(tokens.jwks())


@mcp.custom_route("/identity/exchange", methods=["POST"])
async def _identity_exchange(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        tok = tokens.exchange(
            subject_token=body["subject_token"],
            actor_token=body["actor_token"],
            requested_scope=body.get("scope"),
            audience=body.get("audience"),
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"access_token": tok, "token_type": "Bearer"})

# ---------------------------------------------------------------------------
# directory read routes  (read-only; never used by arm A)
# ---------------------------------------------------------------------------

def _dir_503() -> JSONResponse | None:
    if state.directory_down:
        return JSONResponse({"error": "directory unavailable"}, status_code=503)
    return None


@mcp.custom_route("/directory/users/{name}", methods=["GET"])
async def _dir_users(request: Request) -> JSONResponse:
    guard = _dir_503()
    if guard:
        return guard
    name = request.path_params["name"]
    user = state.users.get(name)
    if user is None:
        return JSONResponse({"error": f"user {name!r} not found"}, status_code=404)
    return JSONResponse({"role": user["role"], "manager": user["manager"]})


@mcp.custom_route("/directory/delegations", methods=["GET"])
async def _dir_delegations(request: Request) -> JSONResponse:
    guard = _dir_503()
    if guard:
        return guard
    now = simclock.now()
    filtered = []
    for d in state.delegations:
        expires_raw = d.get("expires", "")
        try:
            if expires_raw.endswith("Z"):
                expires_raw = expires_raw[:-1] + "+00:00"
            expires_dt = datetime.datetime.fromisoformat(expires_raw)
            if expires_dt.timestamp() < now:
                continue
        except (ValueError, TypeError):
            pass
        filtered.append(d)
    return JSONResponse({"delegations": filtered, "sim_time": now})


@mcp.custom_route("/directory/vendors", methods=["GET"])
async def _dir_vendors(request: Request) -> JSONResponse:
    guard = _dir_503()
    if guard:
        return guard
    return JSONResponse(state.vendors)


@mcp.custom_route("/directory/expenses/{id}", methods=["GET"])
async def _dir_expense(request: Request) -> JSONResponse:
    guard = _dir_503()
    if guard:
        return guard
    expense_id = request.path_params["id"]
    expense = state.expenses.get(expense_id)
    if expense is None:
        return JSONResponse({"error": f"expense {expense_id!r} not found"}, status_code=404)
    return JSONResponse({"claimant": expense["claimant"], "status": expense["status"]})

# --- GATEWAY BEGIN ---
@mcp.custom_route("/central/decide", methods=["POST"])
async def _central_decide_route(request: Request) -> JSONResponse:
    from domain import gateway as _gw
    if not _gw.is_active():
        return JSONResponse({"error": "not found"}, status_code=404)
    from domain import central_service
    body = await request.json()
    user = body.get("user", "")
    tool = body.get("tool", "")
    arguments = body.get("arguments", {})
    state.central_decide_calls += 1
    result = central_service.decide(user, tool, arguments)
    return JSONResponse(result)


@mcp.custom_route("/gateway/mcp", methods=["POST"])
async def _gateway_mcp_route(request: Request) -> JSONResponse:
    from domain import gateway
    return await gateway.handle_mcp(request)
# --- GATEWAY END ---

# ---------------------------------------------------------------------------
# app -- call streamable_http_app() after all route registrations
# ---------------------------------------------------------------------------

app = mcp.streamable_http_app()

# --- ARM D BEGIN ---
if os.environ.get("ENFORCEMENT") == "cedar":
    from arms.d_boundary import pep as _cedar_pep
    _cedar_pep.install()
# --- ARM D END ---

# --- GATEWAY BEGIN ---
if os.environ.get("GATEWAY") == "true":
    _gw_plugin = None
    if os.environ.get("_GATEWAY_TESTING") == "1":
        _gw_test_name = os.environ.get("GATEWAY_TEST_PLUGIN")
        if _gw_test_name == "allow_all":
            def _gw_plugin(claims, agent_chain, tool, arguments):
                return {"decision": "allow", "rule": None, "reason": "test-allow"}

    if _gw_plugin is None and os.environ.get("GATEWAY_PLUGIN") == "approach4":
        import importlib as _importlib
        _a4_mod = _importlib.import_module("arm" + "s.approach4.plugin")
        _gw_plugin = _a4_mod.evaluate

    if _gw_plugin is None and os.environ.get("GATEWAY_PLUGIN") == "approach5":
        import importlib as _importlib
        _a5_mod = _importlib.import_module("arm" + "s.approach5.plugin")
        _gw_plugin = _a5_mod.evaluate

    if _gw_plugin is None and os.environ.get("GATEWAY_PLUGIN") == "approach6":
        import importlib as _importlib
        _a6_mod = _importlib.import_module("arm" + "s.approach6.plugin")
        _gw_plugin = _a6_mod.evaluate
        _revocation_listeners.append(_a6_mod.push_revocation)
        _reset_callbacks.append(_a6_mod.reset)

    if _gw_plugin is None:
        import sys
        sys.exit("gateway mode requires a policy plugin")

    from domain.gateway import install_gateway
    from domain.grants import AGENT_GRANTS as _AGENT_GRANTS

    # Load shared reviewed fingerprints from domain/fingerprints.json
    _fp_path = pathlib.Path(__file__).parent / "fingerprints.json"
    _gw_fps = json.loads(_fp_path.read_text(encoding="utf-8"))
    install_gateway(
        grants=_AGENT_GRANTS,
        fingerprints=_gw_fps,
        scope_map={
            "read_receipt": "expenses:read",
            "submit_expense": "expenses:submit",
            "approve_expense": "expenses:approve",
            "book_travel": "travel:book",
            "pay_vendor": "payments:pay",
        },
        policy_plugin=_gw_plugin,
    )
# --- GATEWAY END ---

# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("SERVER_PORT", "8765"))
    uvicorn.run("domain.server:app", host="127.0.0.1", port=port)
