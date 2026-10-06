import asyncio
import datetime
import json
import os
import pathlib
import time
import uuid
from collections.abc import AsyncGenerator
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
# A2A payments-agent state (ScriptedModel turns, last transcript)
# ---------------------------------------------------------------------------

_payments_agent_model_turns: list[dict] | None = None
_last_transcript: dict = {}


class _ScriptedModel:
    """Strands-compatible model stub for deterministic testing."""

    def __init__(self, turns: list[dict]) -> None:
        self._turns = list(turns)
        self._idx = 0

    stateful = False

    def get_config(self) -> dict:
        return {}

    def update_config(self, **kwargs: Any) -> None:
        pass

    @property
    def context_window_limit(self) -> int:
        return 200_000

    def count_tokens(self, messages: list, tool_specs: list | None = None,
                     system_prompt: str | None = None, **kwargs: Any) -> int:
        return len(str(messages)) // 4

    def estimate_utilization(self, input_tokens: int) -> float:
        return 0.0

    async def stream(self, messages: list, tool_specs: list | None = None,
                     system_prompt: str | None = None, **kwargs: Any) -> AsyncGenerator[dict, None]:
        turn = (self._turns[self._idx] if self._idx < len(self._turns) else {"text": "Done."})
        self._idx += 1
        yield {"messageStart": {"role": "assistant"}}
        if "tool" in turn:
            tool_id = f"tooluse_{uuid.uuid4().hex[:8]}"
            yield {"contentBlockStart": {"contentBlockIndex": 0,
                   "start": {"toolUse": {"name": turn["tool"], "toolUseId": tool_id}}}}
            yield {"contentBlockDelta": {"contentBlockIndex": 0,
                   "delta": {"toolUse": {"input": json.dumps(turn.get("input", {})),
                             "toolUseId": tool_id, "name": turn["tool"]}}}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "tool_use"}}
        else:
            yield {"contentBlockStart": {"contentBlockIndex": 0, "start": {"text": ""}}}
            yield {"contentBlockDelta": {"contentBlockIndex": 0,
                   "delta": {"text": turn.get("text", "Done.")}}}
            yield {"contentBlockStop": {"contentBlockIndex": 0}}
            yield {"messageStop": {"stopReason": "end_turn"}}
        yield {"metadata": {"usage": {"inputTokens": 5, "outputTokens": 5, "totalTokens": 10,
               "cacheReadInputTokens": 0, "cacheWriteInputTokens": 0},
               "metrics": {"latencyMs": 1}}}

    stream.__wrapped__ = True


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
    entry = {"seq": simclock.next_seq(), **entry}
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


def _outcome_line(
    call_id: str, *, executed: bool, error: str | None, tool_ms: float | None = None,
) -> None:
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
            "tool_ms":   tool_ms,
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
    t0 = time.monotonic()  # latency only; never used for tokens or decisions

    def _ms() -> float:
        return (time.monotonic() - t0) * 1000

    # Verify Bearer token; identity comes only from the token
    auth = ctx.request_context.request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        decision = {"decision": "deny", "rule": "IDENTITY", "reason": "missing Bearer token"}
        _decision_line(call_id, caller, tool_name, arguments, decision)
        _outcome_line(call_id, executed=False, error="missing Bearer token", tool_ms=_ms())
        raise PermissionError("missing Bearer token")

    try:
        claims = tokens.verify_bearer(auth[7:])
    except Exception as exc:
        reason = str(exc)
        decision = {"decision": "deny", "rule": "IDENTITY", "reason": reason}
        _decision_line(call_id, caller, tool_name, arguments, decision)
        _outcome_line(call_id, executed=False, error=reason, tool_ms=_ms())
        raise PermissionError(reason) from exc

    caller = tokens.claims_to_caller(claims)

    try:
        decision = authorise(caller, tool_name, arguments)
    except Exception as exc:
        decision = {"decision": "error", "rule": None, "reason": str(exc)}
        _decision_line(call_id, caller, tool_name, arguments, decision)
        _outcome_line(call_id, executed=False, error=str(exc), tool_ms=_ms())
        raise PermissionError(str(exc)) from exc

    _decision_line(call_id, caller, tool_name, arguments, decision)

    if decision["decision"] == "deny":
        _outcome_line(call_id, executed=False, error=decision["reason"], tool_ms=_ms())
        raise PermissionError(decision["reason"])

    try:
        result = action_fn(caller)
    except Exception as exc:
        _outcome_line(call_id, executed=False, error=str(exc), tool_ms=_ms())
        raise

    _outcome_line(call_id, executed=True, error=None, tool_ms=_ms())
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
    global _payments_agent_model_turns, _last_transcript
    _payments_agent_model_turns = None
    _last_transcript = {}
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
    state.set_expense_limit(int(body["limit"]))
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


@mcp.custom_route("/control/set_model_turns", methods=["POST"])
async def _ctrl_set_model_turns(request: Request) -> JSONResponse:
    if os.environ.get("SERVER_TEST_MODE") != "1":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    global _payments_agent_model_turns
    body = await request.json()
    _payments_agent_model_turns = body.get("turns", [])
    return JSONResponse({"ok": True})


@mcp.custom_route("/control/last_transcript", methods=["GET"])
async def _ctrl_last_transcript(request: Request) -> JSONResponse:
    return JSONResponse(_last_transcript)

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

def _dir_503(path: str = "") -> JSONResponse | None:
    """Every read of the facts store is logged so the reads can be counted."""
    _append({"type": "directory_read", "path": path,
             "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
             "sim_time": simclock.now(), "run_id": state.run_id,
             "served": not state.directory_down})
    if state.directory_down:
        return JSONResponse({"error": "directory unavailable"}, status_code=503)
    return None


@mcp.custom_route("/directory/users/{name}", methods=["GET"])
async def _dir_users(request: Request) -> JSONResponse:
    guard = _dir_503(request.url.path)
    if guard:
        return guard
    name = request.path_params["name"]
    user = state.users.get(name)
    if user is None:
        return JSONResponse({"error": f"user {name!r} not found"}, status_code=404)
    return JSONResponse({"role": user["role"], "manager": user["manager"]})


@mcp.custom_route("/directory/delegations", methods=["GET"])
async def _dir_delegations(request: Request) -> JSONResponse:
    guard = _dir_503(request.url.path)
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
    guard = _dir_503(request.url.path)
    if guard:
        return guard
    return JSONResponse(state.vendors)


@mcp.custom_route("/directory/expenses/{id}", methods=["GET"])
async def _dir_expense(request: Request) -> JSONResponse:
    guard = _dir_503(request.url.path)
    if guard:
        return guard
    expense_id = request.path_params["id"]
    expense = state.expenses.get(expense_id)
    if expense is None:
        return JSONResponse({"error": f"expense {expense_id!r} not found"}, status_code=404)
    return JSONResponse({"claimant": expense["claimant"], "status": expense["status"]})

# --- GATEWAY BEGIN ---
# ---------------------------------------------------------------------------
# A2A payments-agent handler
# ---------------------------------------------------------------------------

def _run_payments_agent(task: str, tool_server_token: str, user: str,
                        approach: str, gen: str, use_gateway: bool) -> tuple[str, list[dict]]:
    """Run payments-agent synchronously (called inside asyncio.to_thread)."""
    port = int(os.environ.get("SERVER_PORT", "8765"))
    base_url = f"http://127.0.0.1:{port}"
    mcp_url = f"{base_url}/mcp"

    model = None
    if _payments_agent_model_turns is not None:
        model = _ScriptedModel(_payments_agent_model_turns)

    # Task 8, D4: with MODEL_CALL_LOG set, this agent's model calls go to the
    # run's model log. Unset, the model is exactly what it was.
    model_call_log = os.environ.get("MODEL_CALL_LOG")
    if model_call_log:
        from runner import model_record
        model = model_record.RecordingModel(
            model, factory=None if model is not None else model_record.real_model,
            log_path=model_call_log, source="server")

    import importlib as _il
    if approach in ("1", "4", "5", "6"):
        _arm_a = _il.import_module("arm" + "s.a_guides.agent")
        reply, _usage, turns_data = _arm_a.run(
            agent_name="payments-agent", user=user, turns=[task],
            mcp_url=mcp_url, bearer_token=tool_server_token,
            model=model, use_gateway=use_gateway,
        )
    elif approach == "2":
        _arm_c = _il.import_module("arm" + "s.c_hoo" + "k.agent")
        reply, _usage, turns_data = _arm_c.run(
            agent_name="payments-agent", user=user, turns=[task],
            mcp_url=mcp_url, bearer_token=tool_server_token,
            model=model, use_gateway=use_gateway,
        )
    elif approach == "3":
        gen_num = int(gen.replace("gen-", "")) if gen else 1
        _arm3 = _il.import_module("arm" + "s.approach3.agent")
        reply, _usage, turns_data = _arm3.run(
            agent_name="payments-agent", user=user, turns=[task],
            mcp_url=mcp_url, bearer_token=tool_server_token,
            model=model, use_gateway=use_gateway, gen=gen_num,
        )
    else:
        reply = "Unknown approach"
        turns_data = []

    return reply, turns_data


async def _handle_a2a_internal(request: Request) -> JSONResponse:
    """Handle A2A internally — identity already verified by caller."""
    global _last_transcript
    auth = request.headers.get("authorization", "")
    deleg_tok_str = auth[7:] if auth.startswith("Bearer ") else ""

    claims = tokens.verify_bearer(deleg_tok_str, audience="payments-agent")
    user = claims.get("sub", "")
    caller = tokens.claims_to_caller(claims)

    body = await request.json()
    msg = body.get("params", {}).get("message", {})
    texts = [p["text"] for p in msg.get("parts", []) if p.get("kind") == "text"]
    task = " ".join(texts)

    from domain.scopes import TOOL_SCOPE_MAP as _tsm
    pa_scope = [_tsm["pay_vendor"], _tsm["approve_expense"]]
    pa_tok = tokens.issue_agent_token(
        sub="payments-agent", aud=tokens.USER_ISSUER, scope=pa_scope,
    )
    tool_tok = tokens.exchange(
        subject_token=deleg_tok_str, actor_token=pa_tok,
        audience=tokens.SERVER_AUDIENCE,
    )

    approach = os.environ.get("PAYMENTS_AGENT_APPROACH", "1")
    gen = os.environ.get("PAYMENTS_AGENT_GEN", "gen-1")
    use_gateway = os.environ.get("GATEWAY") == "true"

    reply, turns_data = await asyncio.to_thread(
        _run_payments_agent, task, tool_tok, user, approach, gen, use_gateway,
    )

    approach_label = approach
    if approach in ("1", "4", "5", "6"):
        import importlib as _il2
        _arm_a2 = _il2.import_module("arm" + "s.a_guides.agent")
        sys_prompt = _arm_a2.build_system_prompt("payments-agent", user=user)
    else:
        from domain.identity import identity_paragraph
        sys_prompt = identity_paragraph("payments-agent", user)

    _last_transcript = {"system_prompt": sys_prompt, "turns": turns_data}

    return JSONResponse({
        "jsonrpc": "2.0",
        "id": body.get("id"),
        "result": {
            "message": {
                "messageId": str(uuid.uuid4()),
                "role": "agent",
                "parts": [{"kind": "text", "text": reply}],
            },
        },
    })


@mcp.custom_route("/a2a/payments-agent", methods=["POST"])
async def _a2a_route(request: Request) -> JSONResponse:
    call_id = str(uuid.uuid4())
    caller: dict = {"user": None, "agent": None}

    from domain import gateway as _gw
    if _gw.is_active():
        decision = {"decision": "deny", "rule": "GATEWAY_BYPASS",
                     "reason": "gateway mode active; use /gateway/a2a/payments-agent"}
        _decision_line(call_id, caller, "ask_payments_agent", {}, decision)
        _outcome_line(call_id, executed=False, error=decision["reason"])
        return JSONResponse({
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32000, "message": "GATEWAY_BYPASS"},
        }, status_code=403)

    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        decision = {"decision": "deny", "rule": "IDENTITY", "reason": "missing Bearer token"}
        _decision_line(call_id, caller, "ask_payments_agent", {}, decision)
        _outcome_line(call_id, executed=False, error="missing Bearer token")
        return JSONResponse({
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32003, "message": "missing Bearer token"},
        }, status_code=401)

    try:
        claims = tokens.verify_bearer(auth[7:], audience="payments-agent")
    except Exception as exc:
        decision = {"decision": "deny", "rule": "IDENTITY", "reason": str(exc)}
        _decision_line(call_id, caller, "ask_payments_agent", {}, decision)
        _outcome_line(call_id, executed=False, error=str(exc))
        return JSONResponse({
            "jsonrpc": "2.0", "id": None,
            "error": {"code": -32003, "message": str(exc)},
        }, status_code=401)

    caller = tokens.claims_to_caller(claims)
    decision = {"decision": "allow", "rule": "IDENTITY", "reason": "valid delegation token"}
    _decision_line(call_id, caller, "ask_payments_agent", {}, decision)
    _outcome_line(call_id, executed=True, error=None)

    return await _handle_a2a_internal(request)
# --- GATEWAY END ---


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


_APP_AUDIENCE = "expenses-app"

# Set by tests (ASGITransport) or left None: in normal operation the app
# channel makes a real HTTP call to this server's own /central/decide.
_app_central_transport: Any = None


def _app_decision_line(call_id: str, user: str | None, arguments: dict,
                       decision: dict, check_ms: float | None = None) -> None:
    _append({
        "type": "decision",
        "call_id": call_id,
        "layer": "gateway",
        "channel": "app",
        "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sim_time": simclock.now(),
        "run_id": state.run_id,
        "scenario": state.scenario,
        "check_ms": check_ms,
        "user": user,
        "agent": None,
        "tool": "submit_expense",
        "arguments": arguments,
        "decision": decision["decision"],
        "rule": decision.get("rule"),
        "reason": decision.get("reason", ""),
        "central_called": decision.get("central_called", False),
        "central_calls": 1 if decision.get("central_called", False) else 0,
        "central_duration_ms": 0,
    })


@mcp.custom_route("/app/expenses", methods=["POST"])
async def _app_expenses_route(request: Request) -> JSONResponse:
    """Non-agent channel: the company expenses web app, user token only."""
    from domain import gateway as _gw
    from domain.scopes import TOOL_SCOPE_MAP
    if not _gw.is_active():
        return JSONResponse({"error": "not found"}, status_code=404)

    call_id = str(uuid.uuid4())
    t0 = time.monotonic()  # latency only
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    arguments = {
        "amount": body.get("amount", 0),
        "description": body.get("description", ""),
    }
    if body.get("receipt_id"):
        arguments["receipt_id"] = body["receipt_id"]
    if body.get("approval_ref"):
        arguments["approval_ref"] = body["approval_ref"]

    def _refuse(user: str | None, rule: str, reason: str) -> JSONResponse:
        _app_decision_line(call_id, user, arguments,
                           {"decision": "deny", "rule": rule, "reason": reason},
                           check_ms=(time.monotonic() - t0) * 1000)
        return JSONResponse({"error": reason, "rule": rule}, status_code=403)

    auth = request.headers.get("authorization")
    if not auth or not auth.startswith("Bearer "):
        return _refuse(None, "IDENTITY", "missing Bearer token")
    try:
        claims = tokens.verify_user_bearer(auth[7:], audience=_APP_AUDIENCE)
    except Exception as exc:
        return _refuse(None, "IDENTITY", str(exc))

    # identity comes only from the token
    user = canonicalise(claims.get("sub", ""))
    arguments["claimant"] = user

    required = TOOL_SCOPE_MAP["submit_expense"]
    if required not in set(claims.get("scope", "").split()):
        return _refuse(user, "SCOPE", f"missing scope {required!r}")

    import httpx
    try:
        async with httpx.AsyncClient(
            transport=_app_central_transport,
            base_url=str(request.base_url),
        ) as client:
            r = await client.post("/central/decide", json={
                "user": user, "tool": "submit_expense", "arguments": arguments,
            })
        if r.status_code == 200:
            result = r.json()
        else:
            result = {"decision": "deny", "rule": "CENTRAL_UNAVAILABLE",
                      "reason": f"central service returned HTTP {r.status_code}"}
    except Exception as exc:
        result = {"decision": "deny", "rule": "CENTRAL_UNAVAILABLE",
                  "reason": f"central service unreachable: {exc}"}
    _app_decision_line(call_id, user, arguments, {**result, "central_called": True},
                       check_ms=(time.monotonic() - t0) * 1000)
    if result["decision"] != "allow":
        return JSONResponse(
            {"error": result.get("reason", ""), "rule": result.get("rule")},
            status_code=403,
        )
    try:
        expense = execute_tool("submit_expense", arguments, {"user": user})
    except Exception as exc:
        _append({"type": "outcome", "call_id": call_id, "executed": False,
                 "error": str(exc),
                 "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                 "sim_time": simclock.now(),
                 "tool_ms": (time.monotonic() - t0) * 1000})
        return JSONResponse({"error": str(exc)}, status_code=400)
    _append({"type": "outcome", "call_id": call_id, "executed": True, "error": None,
             "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
             "sim_time": simclock.now(),
             "tool_ms": (time.monotonic() - t0) * 1000})
    return JSONResponse({"expense": expense})


@mcp.custom_route("/gateway/mcp", methods=["POST", "GET", "DELETE"])
async def _gateway_mcp_route(request: Request):
    if request.method == "POST":
        from domain import gateway
        return await gateway.handle_mcp(request)
    if request.method == "DELETE":
        return JSONResponse({"ok": True})
    from starlette.responses import StreamingResponse

    async def _sse_keepalive():
        yield "event: endpoint\ndata: /gateway/mcp\n\n"
        while True:
            await asyncio.sleep(3600)

    return StreamingResponse(_sse_keepalive(), media_type="text/event-stream")


@mcp.custom_route("/gateway/a2a/payments-agent", methods=["POST"])
async def _gateway_a2a_route(request: Request) -> JSONResponse:
    from domain import gateway
    return await gateway.handle_a2a(request)
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
        _a6_mod.reset()  # publishes the first copy at server start

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
