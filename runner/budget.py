"""The budget guard (task 8, item 5, D5, D6).

--max-runs and --max-tokens stop a batch cleanly: nothing new starts once a
limit is reached, runs already started finish, and budget.json records how far
the batch got. With N workers up to N-1 runs may still be in flight when the
token limit is crossed, so tokens may overshoot; the overshoot is recorded.
"""
import json
import pathlib


class BudgetRequired(Exception):
    """A run that calls the real model was asked to start without --max-tokens (D6)."""


class Budget:
    def __init__(self, max_runs: int | None = None, max_tokens: int | None = None) -> None:
        self.max_runs, self.max_tokens = max_runs, max_tokens
        self.started = 0
        self.completed = 0
        self.tokens_used = 0
        self.stopped_by: str | None = None
        self.overshoot_runs = 0
        self._crossed = False

    def _reached(self) -> str | None:
        if self.max_runs is not None and self.started >= self.max_runs:
            return "max-runs"
        if self.max_tokens is not None and self.tokens_used >= self.max_tokens:
            return "max-tokens"
        return None

    def allow_start(self) -> bool:
        reason = self._reached()
        if reason:
            self.stopped_by = self.stopped_by or reason
            return False
        return True

    def start(self) -> None:
        self.started += 1

    def finish(self, row: dict) -> None:
        """Record a finished run. A run that finishes after the token limit was
        already crossed by another run is an overshoot."""
        if self._crossed:
            self.overshoot_runs += 1
        self.completed += 1
        u = row.get("model_usage") or {}
        self.tokens_used += (u.get("input_tokens") or 0) + (u.get("output_tokens") or 0)
        if self.max_tokens is not None and self.tokens_used >= self.max_tokens:
            self._crossed = True

    def report(self, planned: int, not_run: list) -> dict:
        over = (max(0, self.tokens_used - self.max_tokens)
                if self.max_tokens is not None else 0)
        return {"max_runs": self.max_runs, "max_tokens": self.max_tokens,
                "planned": planned, "started": self.started, "completed": self.completed,
                "tokens_used": self.tokens_used, "stopped_by": self.stopped_by,
                "overshoot_runs": self.overshoot_runs, "overshoot_tokens": over,
                "not_run": [list(x) for x in not_run]}

    def write(self, out_dir: pathlib.Path, planned: int, not_run: list) -> dict:
        rec = self.report(planned, not_run)
        pathlib.Path(out_dir).mkdir(parents=True, exist_ok=True)
        (pathlib.Path(out_dir) / "budget.json").write_text(json.dumps(rec, indent=2))
        return rec
