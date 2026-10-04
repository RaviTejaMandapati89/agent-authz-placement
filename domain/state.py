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

    def record(self, action_type: str, **fields) -> None:
        self.ledger.append(
            {
                "timestamp": datetime.datetime.now(timezone.utc).isoformat(),
                "action_type": action_type,
                **fields,
            }
        )


state = State()


def reset() -> None:
    state.__init__()
    simclock.reset()
