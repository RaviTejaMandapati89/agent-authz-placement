"""
Arm D policy enforcement point.

Installed when the domain server starts with ENFORCEMENT=cedar.
Replaces domain.server.authorise for the duration of the process.
Only state.pdp_down raises; all other missing-data cases omit the relevant
context fields and let Cedar decide.
"""
import datetime
import hashlib
import json
import pathlib
from typing import Any

import cedarpy

from domain.identity import canonicalise

_DIR = pathlib.Path(__file__).parent
_POLICY_PATH = _DIR / "policy.cedar"
_SCHEMA_PATH  = _DIR / "schema.json"
_REVIEWED_TOOLS_PATH = _DIR / "reviewed_tools.json"
_AGENTS_PATH  = _DIR / "agents.json"

_committed_policy: str = ""
_policy_text:      str = ""
_policy_set:       cedarpy.PolicySet | None = None
_reviewed_tools:   dict[str, str] = {}
_agents:           dict = {}

# ---------------------------------------------------------------------------
# fingerprint — D-specific: hashes (name, description, parameters)
# ---------------------------------------------------------------------------

def fingerprint(name: str, description: str, parameters: dict) -> str:
    """SHA-256 of the tool's name, description, and parameters dict as stored
    in the server registry.  Different from arm C's fingerprint, which wraps
    parameters in {\"json\": ...}.  Used by both the PEP and write_reviewed_tools."""
    data = {"description": description, "name": name, "parameters": parameters}
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


# ---------------------------------------------------------------------------
# policy management
# ---------------------------------------------------------------------------

def _parse_policy(text: str) -> cedarpy.PolicySet:
    return cedarpy.PolicySet.from_str(text)


def _load_state() -> None:
    """Populate module-level policy/config state from disk.  Does not touch domain.server."""
    global _committed_policy, _policy_text, _policy_set, _reviewed_tools, _agents
    _committed_policy = _POLICY_PATH.read_text(encoding="utf-8")
    _policy_text      = _committed_policy
    _policy_set       = _parse_policy(_policy_text)
    _reviewed_tools   = json.loads(_REVIEWED_TOOLS_PATH.read_text(encoding="utf-8"))
    _agents           = json.loads(_AGENTS_PATH.read_text(encoding="utf-8"))


def install() -> None:
    """Load state from disk and wire domain.server.authorise to the Cedar PEP."""
    _load_state()
    import domain.server as _server
    _server.authorise = _authorise


def reset_policy() -> None:
    """Restore the committed policy text and re-parse.  Called by /control/reset."""
    global _policy_text, _policy_set
    _policy_text = _committed_policy
    _policy_set  = _parse_policy(_policy_text)


def apply_in_memory_limit(limit: int) -> None:
    """Replace the P2 limit literal in the in-memory policy.

    The committed policy.cedar is never written.  Asserts that the literal
    50000 appears exactly once so a double-call or a corrupt policy is caught.
    """
    global _policy_text, _policy_set
    count = _policy_text.count("50000")
    assert count == 1, f"expected exactly 1 occurrence of 50000 in policy text, found {count}"
    _policy_text = _policy_text.replace("50000", str(limit * 100))
    _policy_set  = _parse_policy(_policy_text)


# ---------------------------------------------------------------------------
# entity and context builders
# ---------------------------------------------------------------------------

def _live_tool_reviewed(tool_name: str) -> bool:
    from domain.server import mcp
    tool = mcp._tool_manager.get_tool(tool_name)
    if tool is None:
        return False
    expected = _reviewed_tools.get(tool_name)
    if expected is None:
        return False
    return fingerprint(tool.name, tool.description, tool.parameters) == expected


def _build_entities(user: str, agent: str) -> list[dict]:
    from domain.state import state

    included: set[str] = set()
    entities: list[dict] = []

    def _add_user(username: str) -> None:
        if username in included:
            return
        included.add(username)
        info = state.users.get(username, {})
        attrs: dict[str, Any] = {
            "username": username,
            "role": info.get("role", ""),
        }
        manager_name = info.get("manager")
        if manager_name:
            attrs["manager"] = {
                "__entity": {"type": "ExpApp::User", "id": manager_name}
            }
        entities.append({
            "uid":     {"type": "ExpApp::User", "id": username},
            "attrs":   attrs,
            "parents": [],
        })
        if manager_name:
            _add_user(manager_name)

    _add_user(user)

    agent_cfg = _agents.get(agent, {"acts_for": "any", "allowed_tools": []})
    entities.append({
        "uid":   {"type": "ExpApp::Agent", "id": agent},
        "attrs": {
            "acts_for":     agent_cfg["acts_for"],
            "allowed_tools": list(agent_cfg["allowed_tools"]),
        },
        "parents": [],
    })

    return entities


def _build_context(tool_name: str, arguments: dict, user: str) -> dict:
    from domain.state import state

    ctx: dict[str, Any] = {
        "action_name":  tool_name,
        "tool_reviewed": _live_tool_reviewed(tool_name),
    }

    if tool_name == "submit_expense":
        claimant = canonicalise(arguments.get("claimant", ""))
        amount   = arguments.get("amount", 0)
        ctx["claimant"]         = claimant
        ctx["amount_pence"]     = round(amount * 100)
        ctx["has_approval_ref"] = bool(arguments.get("approval_ref"))

    elif tool_name == "approve_expense":
        expense_id = arguments.get("expense_id", "")
        expense    = state.expenses.get(expense_id)
        if expense is not None:
            claimant = expense.get("claimant", "")
            ctx["expense_claimant"] = claimant
            claimant_info = state.users.get(claimant)
            if claimant_info is not None:
                mgr = claimant_info.get("manager")
                if mgr is not None:
                    ctx["expense_claimant_manager"] = mgr

    elif tool_name == "book_travel":
        from domain import simclock as _simclock
        traveller = canonicalise(arguments.get("traveller", ""))
        ctx["traveller"] = traveller
        now        = datetime.datetime.fromtimestamp(_simclock.now(), tz=datetime.timezone.utc)
        delegators: set[str] = set()
        for d in state.delegations:
            if (
                d.get("delegate") == user
                and d.get("active") is True
                and "travel" in d.get("scope", [])
            ):
                raw = d.get("expires", "")
                try:
                    exp = datetime.datetime.fromisoformat(
                        raw[:-1] + "+00:00" if raw.endswith("Z") else raw
                    )
                    if exp > now:
                        delegators.add(d["delegator"])
                except ValueError:
                    pass
        ctx["travel_delegators"] = list(delegators)

    elif tool_name == "pay_vendor":
        vendor = canonicalise(arguments.get("vendor", ""))
        ctx["vendor"]          = vendor
        ctx["vendor_approved"] = vendor in state.vendors

    return ctx


# ---------------------------------------------------------------------------
# decision extraction
# ---------------------------------------------------------------------------

def _extract_decision(result: cedarpy.AuthzResult) -> dict:
    annotations = result.diagnostics.id_annotations_by_reason
    if result.allowed:
        rule = next(iter(annotations.values()), "P7")
        return {"decision": "allow", "rule": rule, "reason": "permitted"}
    if annotations:
        rule   = next(iter(annotations.values()))
        reason = f"denied by rule {rule}"
    else:
        rule   = "P7"
        reason = "no permit"
    return {"decision": "deny", "rule": rule, "reason": reason}


# ---------------------------------------------------------------------------
# authorise — called by domain.server._run_tool on every tool call
# ---------------------------------------------------------------------------

def _authorise(caller: dict, tool_name: str, arguments: dict) -> dict:
    from domain.state import state

    if state.pdp_down:
        raise RuntimeError("PDP unavailable")

    user  = caller.get("user",  "")
    agent = caller.get("agent", "")

    entities = _build_entities(user, agent)
    context  = _build_context(tool_name, arguments, user)

    request = {
        "principal": {"type": "ExpApp::User",  "id": user},
        "action":    "ExpApp::Action::\"call_tool\"",
        "resource":  {"type": "ExpApp::Agent", "id": agent},
        "context":   context,
    }

    result = cedarpy.is_authorized(
        request=request,
        policies=_policy_set,
        entities=entities,
    )
    return _extract_decision(result)
