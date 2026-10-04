"""Approach 4 policy plugin — evaluates use-case Cedar policies from token claims only.

People facts (role, reports_to, delegations_received) come exclusively from
token claims.  The plugin may read the expense record for a specific expense_id
(needed by P3 to identify the claimant) but must never read users, delegations,
vendors or expense_limit from live state.
"""
import datetime
import hashlib
import json
import pathlib

import cedarpy

from domain import simclock

_POLICIES_DIR = pathlib.Path(__file__).parent / "policies"

AGENT_USE_CASE: dict[str, str] = {
    "expense-assistant": "expenses",
    "travel-assistant":  "travel",
    "payments-agent":    "payments",
}

_policy_cache: dict[str, cedarpy.PolicySet] = {}


def fingerprint(name: str, description: str, parameters: dict) -> str:
    data = {"description": description, "name": name, "parameters": parameters}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _load_policy(use_case: str) -> cedarpy.PolicySet:
    if use_case not in _policy_cache:
        text = (_POLICIES_DIR / f"{use_case}.cedar").read_text(encoding="utf-8")
        _policy_cache[use_case] = cedarpy.PolicySet.from_str(text)
    return _policy_cache[use_case]


def _has_delegation(claims: dict, traveller: str) -> bool:
    now = simclock.now()
    for d in claims.get("delegations_received", []):
        if d.get("delegator") != traveller:
            continue
        if "travel" not in d.get("scope", []):
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


def _build_context(claims: dict, tool: str, arguments: dict) -> dict:
    ctx: dict = {"action_name": tool}

    if tool == "submit_expense":
        if "claimant" in arguments:
            ctx["claimant"] = str(arguments["claimant"])
        if "amount" in arguments:
            ctx["amount_pence"] = int(round(float(arguments["amount"]) * 100))
        ctx["has_approval_ref"] = bool(arguments.get("approval_ref"))

    elif tool == "approve_expense":
        expense_id = arguments.get("expense_id")
        if expense_id:
            from domain.state import state
            expense = state.expenses.get(expense_id)
            if expense:
                claimant = expense["claimant"]
                ctx["expense_claimant"] = claimant
                approver = claims.get("sub", "")
                reports_to = claims.get("reports_to", [])
                ctx["approver_is_manager"] = claimant in reports_to

    elif tool == "book_travel":
        if "traveller" in arguments:
            traveller = str(arguments["traveller"])
            ctx["traveller"] = traveller
            ctx["has_travel_delegation"] = _has_delegation(claims, traveller)

    elif tool == "pay_vendor":
        if "vendor" in arguments:
            ctx["vendor"] = str(arguments["vendor"])

    return ctx


def _rule_id_from_result(result: cedarpy.AuthzResult) -> str | None:
    annotations = result.diagnostics.id_annotations_by_reason
    if annotations:
        return next(iter(annotations.values()))
    return None


def evaluate(claims: dict, agent_chain: list, tool: str, arguments: dict) -> dict:
    agent = agent_chain[0] if agent_chain else None
    use_case = AGENT_USE_CASE.get(agent)
    if use_case is None:
        return {
            "decision": "deny",
            "rule": "GRANT",
            "reason": f"unknown agent {agent!r}",
        }

    policy_set = _load_policy(use_case)
    ctx = _build_context(claims, tool, arguments)

    principal_id = claims.get("sub", "")
    role = claims.get("role", "")

    entities = [
        {
            "uid": {"type": "User", "id": principal_id},
            "attrs": {
                "username": principal_id,
                "role": role,
            },
            "parents": [],
        },
    ]

    request = {
        "principal": {"type": "User", "id": principal_id},
        "action":    {"type": "Action", "id": tool},
        "resource":  {"type": "Resource", "id": "r"},
        "context":   ctx,
    }

    result = cedarpy.is_authorized(
        request=request,
        policies=policy_set,
        entities=entities,
    )

    if result.allowed:
        return {"decision": "allow", "rule": None, "reason": ""}

    rule_id = _rule_id_from_result(result)
    return {
        "decision": "deny",
        "rule": rule_id,
        "reason": f"denied by rule {rule_id}",
    }


evaluate.skip_token_expiry = True
