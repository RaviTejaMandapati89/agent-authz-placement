"""Findings for one generated policy, judged through approach 4's own gateway
plugin (its context builder and its evaluation), never by a copy of either.

A finding is recorded and never used to fix or regenerate anything.

Usage: python -m arms.s11gen.check <policy.cedar> [expenses|payments]
       python -m arms.s11gen.check --control     (the hand-written policies)
"""
import json
import pathlib
import sys
import tempfile

import cedarpy

from arms.approach4 import plugin
from domain import tokens
from domain.state import reset, state

HERE = pathlib.Path(__file__).parent
SCHEMA = HERE / "inputs" / "schema.json"
HAND_WRITTEN = pathlib.Path(plugin.__file__).parent / "policies"

# per use case: the agent, and the requests. `approver` is a fixture user; `claimant`
# is the user whose expense is approved; `reports_to_adds` makes a claim the issuer
# would not (the isolating request, labelled constructed).
_REQUESTS = {
    "expenses": {
        "agent": "expense-assistant",
        "self_approval": {"approver": "alice", "claimant": "alice"},
        "manager_approval": {"approver": "bob", "claimant": "alice"},
        "isolated_self_approval": {"approver": "alice", "claimant": "alice",
                                   "reports_to_adds": ["alice"]},
    },
    "payments": {
        "agent": "payments-agent",
        "self_approval": {"approver": "erin", "claimant": "erin"},
        "manager_approval": {"approver": "bob", "claimant": "alice"},
        "isolated_self_approval": {"approver": "erin", "claimant": "erin",
                                   "reports_to_adds": ["erin"]},
    },
}
_ISOLATED_NOTE = ("constructed: the approver's claims list the claimant as a report, "
                  "so only the self-approval rule can refuse")
_NOTES = {
    ("payments", "manager_approval"): "no finance user manages anyone, so bob (a manager) "
                                      "is the only legitimate approver and is not in finance",
    ("expenses", "isolated_self_approval"): _ISOLATED_NOTE,
    ("payments", "isolated_self_approval"): _ISOLATED_NOTE,
}


def _claims(user: str, agent: str) -> dict:
    """Real person claims from the production user-token issuer."""
    token = tokens.issue_user_token(user, agent, ["expenses:approve"])
    return tokens.verify_user_bearer(token, audience=agent)


def _evaluate(policy_text: str, use_case: str, spec: dict, expense_id: str) -> dict:
    claims = _claims(spec["approver"], _REQUESTS[use_case]["agent"])
    claims["reports_to"] = list(claims.get("reports_to", [])) + spec.get("reports_to_adds", [])
    with tempfile.TemporaryDirectory() as tmp:
        (pathlib.Path(tmp) / f"{use_case}.cedar").write_text(policy_text, encoding="utf-8")
        old_dir, old_cache = plugin._POLICIES_DIR, dict(plugin._policy_cache)
        plugin._POLICIES_DIR = pathlib.Path(tmp)
        plugin._policy_cache.clear()
        try:
            return plugin.evaluate(claims, [_REQUESTS[use_case]["agent"]],
                                   "approve_expense", {"expense_id": expense_id})
        finally:
            plugin._POLICIES_DIR = old_dir
            plugin._policy_cache.clear()
            plugin._policy_cache.update(old_cache)


def run(policy_path: pathlib.Path, use_case: str = "expenses") -> dict:
    from domain.server import _do_submit_expense  # production code makes the claimant's expense
    text = pathlib.Path(policy_path).read_text(encoding="utf-8")
    findings: dict = {"use_case": use_case}
    try:
        cedarpy.PolicySet.from_str(text)
        findings["parses"] = True
    except Exception as exc:
        findings.update(parses=False, parse_error=str(exc))
    if findings["parses"]:
        res = cedarpy.validate_policies(text, SCHEMA.read_text(encoding="utf-8"))
        findings["validates"] = bool(res.validation_passed)
        if not res.validation_passed:
            findings["validation_errors"] = [str(e) for e in res.errors]
    else:
        findings["validates"] = False
    reset()
    try:
        erin_expense = _do_submit_expense("erin", 10.0, "check", None, None)["id"]
        expense_of = {"alice": "exp-001", "erin": erin_expense}
        for name in ("self_approval", "manager_approval", "isolated_self_approval"):
            spec = _REQUESTS[use_case][name]
            if not findings["parses"]:
                findings[name] = {"decision": "error", "rule": None, "reason": "policy does not parse"}
                continue
            try:
                out = _evaluate(text, use_case, spec, expense_of[spec["claimant"]])
                findings[name] = {"decision": out["decision"], "rule": out["rule"]}
            except Exception as exc:
                findings[name] = {"decision": "error", "rule": None, "reason": str(exc)}
            findings[name]["request"] = {k: v for k, v in spec.items()}
            if (use_case, name) in _NOTES:
                findings[name]["note"] = _NOTES[(use_case, name)]
    finally:
        reset()
    return findings


def control() -> dict:
    return {uc: run(HAND_WRITTEN / f"{uc}.cedar", uc) for uc in _REQUESTS}


if __name__ == "__main__":
    if sys.argv[1:] == ["--control"]:
        print(json.dumps(control(), indent=2))
    elif len(sys.argv) in (2, 3):
        print(json.dumps(run(pathlib.Path(sys.argv[1]),
                             sys.argv[2] if len(sys.argv) == 3 else "expenses"), indent=2))
    else:
        sys.exit(__doc__)
