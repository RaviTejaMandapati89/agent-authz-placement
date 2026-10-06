"""Shared agent grant table used by the gateway across all approaches."""
import json
import os

AGENT_GRANTS: dict[str, list[str]] = {
    "expense-assistant": ["read_receipt", "submit_expense", "approve_expense", "ask_payments_agent"],
    "travel-assistant":  ["book_travel", "submit_expense", "ask_payments_agent"],
    "payments-agent":    ["pay_vendor", "approve_expense"],
}

# A run may add grants for its scenario premise (S15): GRANT_ADDS is a JSON
# object of agent -> tools. The committed table above is never edited.
for _agent, _tools in json.loads(os.environ.get("GRANT_ADDS", "{}")).items():
    AGENT_GRANTS[_agent] = list(AGENT_GRANTS.get(_agent, [])) + [
        t for t in _tools if t not in AGENT_GRANTS.get(_agent, [])]


def granted(agent: str, tool: str) -> bool:
    """Whether the agent is granted the tool. The one source for which agent is
    wired with which tool (task 8, D13)."""
    return tool in AGENT_GRANTS.get(agent, [])
