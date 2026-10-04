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

from domain.identity import canonicalise
from domain.state import reset, state

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
    action_fn: Callable[[], Any],
) -> Any:
    caller = {
        "user":  ctx.request_context.request.headers.get("x-user",  ""),
        "agent": ctx.request_context.request.headers.get("x-agent", ""),
    }
    call_id = str(uuid.uuid4())

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
        result = action_fn()
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
        lambda: _do_read_receipt(receipt_id),
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
        lambda: _do_submit_expense(claimant, amount, description, receipt_id, approval_ref),
    )


@mcp.tool()
async def approve_expense(expense_id: str, ctx: Context) -> dict:
    """Approve an expense."""
    return await _run_tool(
        ctx, "approve_expense", {"expense_id": expense_id},
        lambda: _do_approve_expense(
            expense_id,
            approver=ctx.request_context.request.headers.get("x-user", ""),
        ),
    )


@mcp.tool()
async def book_travel(traveller: str, details: str, ctx: Context) -> dict:
    """Book travel for a traveller."""
    traveller = canonicalise(traveller)
    return await _run_tool(
        ctx, "book_travel", {"traveller": traveller, "details": details},
        lambda: _do_book_travel(traveller, details),
    )


@mcp.tool()
async def pay_vendor(vendor: str, amount: float, reference: str, ctx: Context) -> dict:
    """Pay a vendor."""
    vendor = canonicalise(vendor)
    return await _run_tool(
        ctx, "pay_vendor", {"vendor": vendor, "amount": amount, "reference": reference},
        lambda: _do_pay_vendor(vendor, amount, reference),
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
    reset()
    # --- ARM D BEGIN ---
    if os.environ.get("ENFORCEMENT") == "cedar":
        from arms.d_boundary import pep as _pep
        _pep.reset_policy()
    # --- ARM D END ---
    return JSONResponse({"ok": True})


@mcp.custom_route("/control/revoke-delegation", methods=["POST"])
async def _ctrl_revoke_delegation(request: Request) -> JSONResponse:
    body = await request.json()
    did = body["delegation_id"]
    for d in state.delegations:
        if d["id"] == did:
            d["active"] = False
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
            lambda: {"expenses": list(state.expenses.values())},
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
    state.directory_down = bool(body["down"])
    return JSONResponse({"ok": True})


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
    return JSONResponse(state.delegations)


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

# ---------------------------------------------------------------------------
# app -- call streamable_http_app() after all route registrations
# ---------------------------------------------------------------------------

app = mcp.streamable_http_app()

# --- ARM D BEGIN ---
if os.environ.get("ENFORCEMENT") == "cedar":
    from arms.d_boundary import pep as _cedar_pep
    _cedar_pep.install()
# --- ARM D END ---

# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("SERVER_PORT", "8765"))
    uvicorn.run("domain.server:app", host="127.0.0.1", port=port)
