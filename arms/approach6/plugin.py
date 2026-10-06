"""Approach 6 policy plugin — central policy evaluated locally from published copy.

Person facts come from token claims (as approach 4), except where a revocation
event has been delivered.  The central policy is evaluated against the last
published snapshot of central state (expense limit and approved vendors).
No call to /central/decide is ever made.

Publication schedule: fixed at EPOCH + 900*k (k = 1, 2, 3...).
Snapshots are taken by domain/central_publisher.py when the clock control route
crosses each boundary.  While the central source is down, snapshots are buffered
and the latest is delivered on recovery.  The plugin only RECEIVES copies via
receive_snapshot(); it never reads live state for people-facts or central-policy
data (no access to the four blocked attributes on the state singleton).
"""
import datetime
import os
import pathlib

import cedarpy

from domain import simclock
from domain.simclock import _EPOCH
from domain import central_publisher as _publisher

# A per-run override directory may replace the committed policies; the
# committed files are never edited.
_POLICIES_DIR = pathlib.Path(
    os.environ.get("SHARED_POLICIES_DIR")
    or pathlib.Path(__file__).parent.parent / "shared" / "policies"
)
_CENTRAL_POLICY_PATH = pathlib.Path(__file__).parent.parent.parent / "domain" / "central_policy.cedar"

AGENT_USE_CASE: dict[str, str] = {
    "expense-assistant": "expenses",
    "travel-assistant":  "travel",
    "payments-agent":    "payments",
}

_policy_cache: dict[str, cedarpy.PolicySet] = {}
_central_policy_cache: cedarpy.PolicySet | None = None

# ---------------------------------------------------------------------------
# Module state — reset between tests via reset()
# ---------------------------------------------------------------------------

_snapshot: dict | None = None          # currently delivered snapshot
_snapshot_version: int = 0             # mirrors snap["version"] from last receive_snapshot
_next_pub_at: float = _EPOCH + _publisher._PUBLICATION_INTERVAL  # read-only mirror; set in reset()

_pending_events: list = []
_applied_revocations: set = set()


def receive_snapshot(snap: dict) -> None:
    """Called by central_publisher when a new snapshot is delivered."""
    global _snapshot, _snapshot_version
    _snapshot = snap
    _snapshot_version = snap["version"]


def reset() -> None:
    global _snapshot, _snapshot_version, _next_pub_at
    global _pending_events, _applied_revocations
    _snapshot = None
    _snapshot_version = 0
    _pending_events = []
    _applied_revocations = set()
    _publisher.reset()
    _publisher.register_snapshot_receiver(receive_snapshot)
    _next_pub_at = _publisher._next_pub_at
    # The first copy is published at server start, simulated time 0: the first
    # schedule boundary. No decision has to trigger it.
    _publisher.take_initial_snapshot()


# ---------------------------------------------------------------------------
# Revocation listener — called by server.py when a delegation is revoked
# ---------------------------------------------------------------------------

def push_revocation(
    delegation_id: str,
    delegator: str,
    delegate: str,
    scope: list,
    revoke_sim_time: float,
) -> None:
    _pending_events.append({
        "id":            delegation_id,
        "delegator":     delegator,
        "delegate":      delegate,
        "scope":         list(scope),
        "delivery_time": revoke_sim_time + 1.0,
    })


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------

def _drain_due_events(now: float) -> None:
    remaining = []
    for event in _pending_events:
        if event["delivery_time"] <= now:
            _applied_revocations.add((event["delegator"], event["delegate"]))
        else:
            remaining.append(event)
    _pending_events[:] = remaining


# ---------------------------------------------------------------------------
# Policy loading
# ---------------------------------------------------------------------------

def _load_usecase_policy(use_case: str) -> cedarpy.PolicySet:
    if use_case not in _policy_cache:
        text = (_POLICIES_DIR / f"{use_case}.cedar").read_text(encoding="utf-8")
        _policy_cache[use_case] = cedarpy.PolicySet.from_str(text)
    return _policy_cache[use_case]


def _get_central_policy() -> cedarpy.PolicySet:
    global _central_policy_cache
    if _central_policy_cache is None:
        _central_policy_cache = cedarpy.PolicySet.from_str(
            _CENTRAL_POLICY_PATH.read_text(encoding="utf-8")
        )
    return _central_policy_cache


def _rule_id_from_result(result: cedarpy.AuthzResult) -> str | None:
    annotations = result.diagnostics.id_annotations_by_reason
    if annotations:
        return next(iter(annotations.values()))
    return None


# ---------------------------------------------------------------------------
# Context builders
# ---------------------------------------------------------------------------

def _is_revoked(delegator: str, delegate: str) -> bool:
    return (delegator, delegate) in _applied_revocations


def _has_delegation(claims: dict, traveller: str) -> bool:
    now = simclock.now()
    user = claims.get("sub", "")
    for d in claims.get("delegations_received", []):
        if d.get("delegator") != traveller:
            continue
        if "travel" not in d.get("scope", []):
            continue
        if _is_revoked(traveller, user):
            continue
        exp_raw = d.get("expires") or d.get("exp")
        if exp_raw is None:
            continue
        if isinstance(exp_raw, str):
            exp_ts = datetime.datetime.fromisoformat(
                exp_raw.replace("Z", "+00:00")
            ).timestamp()
        else:
            exp_ts = float(exp_raw)
        if exp_ts > now:
            return True
    return False


def _build_central_context(snap: dict, claims: dict, tool: str, arguments: dict) -> dict:
    ctx: dict = {"action_name": tool}

    if tool == "submit_expense":
        if "claimant" in arguments:
            ctx["claimant"] = str(arguments["claimant"])
        if "amount" in arguments:
            ctx["amount_pence"] = int(round(float(arguments["amount"]) * 100))
        ctx["has_approval_ref"] = bool(arguments.get("approval_ref"))
        ctx["expense_limit_pence"] = snap["expense_limit"] * 100

    elif tool == "approve_expense":
        expense_id = arguments.get("expense_id")
        if expense_id:
            from domain.state import state
            expense = state.expenses.get(expense_id)
            if expense:
                claimant = expense["claimant"]
                ctx["expense_claimant"] = claimant
                reports_to = claims.get("reports_to", [])
                ctx["approver_is_manager"] = claimant in reports_to

    elif tool == "book_travel":
        if "traveller" in arguments:
            traveller = str(arguments["traveller"])
            ctx["traveller"] = traveller
            ctx["has_travel_delegation"] = _has_delegation(claims, traveller)

    elif tool == "pay_vendor":
        if "vendor" in arguments:
            vendor = str(arguments["vendor"])
            ctx["vendor"] = vendor
            ctx["vendor_is_approved"] = vendor in snap["approved_vendors"]

    return ctx


def _build_uc_context(tool: str, arguments: dict) -> dict:
    ctx: dict = {"action_name": tool}
    if tool == "submit_expense":
        if "claimant" in arguments:
            ctx["claimant"] = str(arguments["claimant"])
        if "amount" in arguments:
            ctx["amount_pence"] = int(round(float(arguments["amount"]) * 100))
        ctx["has_approval_ref"] = bool(arguments.get("approval_ref"))
    elif tool == "pay_vendor":
        if "vendor" in arguments:
            ctx["vendor"] = str(arguments["vendor"])
    return ctx


# ---------------------------------------------------------------------------
# Evaluate
# ---------------------------------------------------------------------------

def evaluate(claims: dict, agent_chain: list, tool: str, arguments: dict) -> dict:
    global _snapshot

    if tool == "ask_payments_agent":
        return {"decision": "allow", "rule": None, "reason": "delegation",
                "central_called": False, "central_duration_ms": 0,
                "central_copy_version": _snapshot_version,
                "revocation_applied": False}

    agent = agent_chain[0] if agent_chain else None
    use_case = AGENT_USE_CASE.get(agent)
    if use_case is None:
        return {
            "decision": "deny",
            "rule": "GRANT",
            "reason": f"unknown agent {agent!r}",
            "central_called": False,
            "central_duration_ms": 0,
        }

    from domain.state import state as _st

    # Drain due revocation events before deciding (only when central source is up).
    if not _st.directory_down:
        _drain_due_events(simclock.now())

    snap = _snapshot
    if snap is None:
        return {
            "decision": "deny",
            "rule": "CENTRAL_UNAVAILABLE",
            "reason": "no published copy available",
            "central_called": False,
            "central_duration_ms": 0,
            "central_copy_version": 0,
            "revocation_applied": False,
        }

    user = claims.get("sub", "")
    role = claims.get("role", "")

    revocation_applied = any(
        _is_revoked(d.get("delegator", ""), user)
        for d in claims.get("delegations_received", [])
    )

    entities = [{
        "uid":     {"type": "User", "id": user},
        "attrs":   {"username": user, "role": role},
        "parents": [],
    }]

    central_ctx = _build_central_context(snap, claims, tool, arguments)
    central_request = {
        "principal": {"type": "User", "id": user},
        "action":    {"type": "Action", "id": tool},
        "resource":  {"type": "Resource", "id": "r"},
        "context":   central_ctx,
    }
    central_result = cedarpy.is_authorized(
        request=central_request,
        policies=_get_central_policy(),
        entities=entities,
    )
    central_rule = _rule_id_from_result(central_result) if not central_result.allowed else None

    uc_ctx = _build_uc_context(tool, arguments)
    uc_request = {
        "principal": {"type": "User", "id": user},
        "action":    {"type": "Action", "id": tool},
        "resource":  {"type": "Resource", "id": "r"},
        "context":   uc_ctx,
    }
    uc_result = cedarpy.is_authorized(
        request=uc_request,
        policies=_load_usecase_policy(use_case),
        entities=entities,
    )
    uc_rule = _rule_id_from_result(uc_result) if not uc_result.allowed else None

    common = {
        "central_called":       False,
        "central_duration_ms":  0,
        "central_copy_version": snap["version"],
        "revocation_applied":   revocation_applied,
    }

    if central_rule is not None:
        return {"decision": "deny", "rule": central_rule,
                "reason": f"denied by rule {central_rule}", **common}

    if uc_rule is not None:
        return {"decision": "deny", "rule": uc_rule,
                "reason": f"denied by rule {uc_rule}", **common}

    return {"decision": "allow", "rule": None, "reason": "", **common}



# Register with the publisher so snapshots are delivered to this plugin.
_publisher.register_snapshot_receiver(receive_snapshot)
