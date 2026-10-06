import copy
import datetime
from datetime import timezone

from domain import simclock
from domain.fixtures import (
    APPROVED_VENDORS,
    DELEGATIONS,
    EXPENSE_LIMIT,
    PENDING_EXPENSES,
    RECEIPTS,
    USERS,
)


class State:
    def __init__(self) -> None:
        self.users: dict = copy.deepcopy(USERS)
        self.delegations: list = copy.deepcopy(DELEGATIONS)
        self.vendors: list = list(APPROVED_VENDORS)
        self.receipts: dict = {r["id"]: copy.deepcopy(r) for r in RECEIPTS}
        self.expenses: dict = {e["id"]: copy.deepcopy(e) for e in PENDING_EXPENSES}
        self.expense_limit: int = EXPENSE_LIMIT
        self.directory_down: bool = False
        self.pdp_down: bool = False
        self.central_decide_calls: int = 0
        self.run_id: str | None = None
        self.scenario: str | None = None
        self.arm: str | None = None
        self.ledger: list = []

    def revoke_delegation(self, delegation_id: str) -> None:
        delegator = ""
        delegate = ""
        scope: list = []
        for d in self.delegations:
            if d["id"] == delegation_id:
                d["active"] = False
                delegator = d.get("delegator", "")
                delegate = d.get("delegate", "")
                scope = list(d.get("scope", []))
                break
        sim_now = simclock.now()
        if delegator or delegate:
            self.record(
                "delegation_revoked",
                delegation_id=delegation_id, delegator=delegator, delegate=delegate,
            )
        from domain import server as _server
        for listener in _server._revocation_listeners:
            listener(delegation_id, delegator, delegate, scope, sim_now)

    def set_expense_limit(self, limit: int) -> None:
        """The central limit change. The ledger records it, not the caller."""
        previous = self.expense_limit
        self.expense_limit = int(limit)
        self.record("limit_changed", previous=previous, limit=self.expense_limit)

    def record(self, action_type: str, **fields) -> None:
        self.ledger.append(
            {
                "seq": simclock.next_seq(),
                "timestamp": datetime.datetime.now(timezone.utc).isoformat(),
                "sim_time": simclock.now(),
                "action_type": action_type,
                **fields,
            }
        )


state = State()


def reset() -> None:
    state.__init__()
    simclock.reset()
