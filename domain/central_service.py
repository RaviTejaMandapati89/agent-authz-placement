"""Central decision service — evaluates domain/central_policy.cedar with live state."""
import datetime
import pathlib

import cedarpy

from domain import simclock
from domain.state import state

_POLICY_PATH = pathlib.Path(__file__).parent / "central_policy.cedar"
_policy: cedarpy.PolicySet | None = None


def _get_policy() -> cedarpy.PolicySet:
    global _policy
    if _policy is None:
        _policy = cedarpy.PolicySet.from_str(_POLICY_PATH.read_text(encoding="utf-8"))
    return _policy


def _has_live_delegation(user: str, traveller: str, scope: str) -> bool:
    now = simclock.now()
    for d in state.delegations:
        if not d.get("active", True):
            continue
        if d.get("delegator") != traveller:
            continue
        if d.get("delegate") != user:
            continue
        if scope not in d.get("scope", []):
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


def _rule_id_from_result(result: cedarpy.AuthzResult) -> str | None:
    annotations = result.diagnostics.id_annotations_by_reason
    if annotations:
        return next(iter(annotations.values()))
    return None


def decide(user: str, tool: str, arguments: dict) -> dict:
    if state.directory_down:
        return {
            "decision": "deny",
            "rule": "CENTRAL_UNAVAILABLE",
            "reason": "central policy service is unavailable",
        }

    user_info = state.users.get(user, {})
    role = user_info.get("role", "")

    ctx: dict = {"action_name": tool}

    if tool == "submit_expense":
        claimant = str(arguments.get("claimant", ""))
        ctx["claimant"] = claimant
        ctx["amount_pence"] = int(round(float(arguments.get("amount", 0)) * 100))
        ctx["has_approval_ref"] = bool(arguments.get("approval_ref"))
        ctx["expense_limit_pence"] = state.expense_limit * 100

    elif tool == "approve_expense":
        expense_id = arguments.get("expense_id")
        if expense_id:
            expense = state.expenses.get(expense_id)
            if expense:
                claimant = expense["claimant"]
                ctx["expense_claimant"] = claimant
                claimant_info = state.users.get(claimant, {})
                ctx["approver_is_manager"] = (claimant_info.get("manager") == user)

    elif tool == "book_travel":
        traveller = str(arguments.get("traveller", ""))
        ctx["traveller"] = traveller
        ctx["has_travel_delegation"] = _has_live_delegation(user, traveller, "travel")

    elif tool == "pay_vendor":
        vendor = str(arguments.get("vendor", ""))
        ctx["vendor"] = vendor
        ctx["vendor_is_approved"] = vendor in state.vendors

    policy = _get_policy()
    entities = [{
        "uid": {"type": "User", "id": user},
        "attrs": {"username": user, "role": role},
        "parents": [],
    }]
    request = {
        "principal": {"type": "User", "id": user},
        "action":    {"type": "Action", "id": tool},
        "resource":  {"type": "Resource", "id": "r"},
        "context":   ctx,
    }

    result = cedarpy.is_authorized(request=request, policies=policy, entities=entities)

    if result.allowed:
        return {"decision": "allow", "rule": None, "reason": ""}

    rule_id = _rule_id_from_result(result)
    return {
        "decision": "deny",
        "rule": rule_id,
        "reason": f"denied by rule {rule_id}",
    }
