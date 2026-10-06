"""S7 change cost: the places an operator edits for a new expense limit to take
effect, per approach (approved change A6).

One table. Each place records its kind and how to read and patch the limit it
holds, so a test can prove every place holds the current limit and that
changing only those places is enough. Committed files are never edited: a patch
is written to a copy in a per-run directory.
"""
import pathlib
import re
from dataclasses import dataclass

import yaml

from domain.fixtures import EXPENSE_LIMIT

REPO = pathlib.Path(__file__).parent.parent

PROMPT = "prompt"
CONFIG = "config"
POLICY_FILE = "policy file"
CENTRAL_STATE = "central state"

_PENCE_RE = re.compile(r"(context\.amount_pence\s*>\s*)(\d+)")


@dataclass(frozen=True)
class Place:
    path: str   # repo-relative file, or "central state"
    kind: str

    def current(self, root: pathlib.Path = REPO) -> int:
        """The limit this place holds, in pounds."""
        if self.kind == CENTRAL_STATE:
            return EXPENSE_LIMIT
        text = (root / self.path).read_text(encoding="utf-8")
        if self.kind == PROMPT:
            m = re.search(r"£(\d+)", text)
            return int(m.group(1))
        if self.kind == CONFIG:
            return int(yaml.safe_load(text)["expense_limit"])
        if self.kind == POLICY_FILE:
            return int(_PENCE_RE.search(text).group(2)) // 100
        raise ValueError(self.kind)

    def holds(self, limit: int, root: pathlib.Path = REPO) -> bool:
        return self.current(root) == limit

    def patched_text(self, new_limit: int, root: pathlib.Path = REPO) -> str:
        """The file's text with the limit changed (files only)."""
        text = (root / self.path).read_text(encoding="utf-8")
        if self.kind == CONFIG:
            return re.sub(r"(?m)^(expense_limit:\s*)\d+", rf"\g<1>{new_limit}", text)
        if self.kind == POLICY_FILE:
            return _PENCE_RE.sub(rf"\g<1>{new_limit * 100}", text)
        if self.kind == PROMPT:
            return text.replace(f"£{self.current(root)}", f"£{new_limit}")
        raise ValueError(f"{self.kind} is not a file place")


_CONFIGS = (
    Place("arms/c_hook/config/expense-assistant.yaml", CONFIG),
    Place("arms/c_hook/config/travel-assistant.yaml", CONFIG),
)
_POLICIES = (
    Place("arms/approach4/policies/expenses.cedar", POLICY_FILE),
    Place("arms/approach4/policies/travel.cedar", POLICY_FILE),
)

CHANGE_PLACES: dict[int, tuple[Place, ...]] = {
    1: (Place("policy.md", PROMPT),),
    2: _CONFIGS,
    3: _CONFIGS,        # approach 3's generations read the same agent-side config
    4: _POLICIES,
    5: (Place("central state", CENTRAL_STATE),),
    6: (Place("central state", CENTRAL_STATE),),
}


def change_cost(approach: int) -> dict:
    places = CHANGE_PLACES[approach]
    return {"places": len(places), "kinds": [p.kind for p in places],
            "paths": [p.path for p in places]}
