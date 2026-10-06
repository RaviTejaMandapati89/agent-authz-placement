"""Task 8, items 3 and 5 and D5, D6: the budget guard."""
import json

import pytest

from domain.server import _ScriptedModel
from runner import study
from tests.study_scripts import SCRIPTS

_TIMEOUT = 120

# Four real runs, scripted model: S1 and S2, approaches 2 and 5.
_ITEMS = [(5, None, "S1", None, 1), (5, None, "S2", None, 1),
          (2, None, "S1", None, 1), (2, None, "S2", None, 1)]


def kwargs_for(item):
    turns, pay = SCRIPTS[(item[2], item[3])]
    return {"model": _ScriptedModel(turns), "payments_turns": pay}


def _budget(**kw):
    from runner import budget
    return budget.Budget(**kw)


def _rows(out):
    return [json.loads(x) for x in (out / "results.jsonl").read_text().splitlines()]


def test_t8_a_real_model_run_refuses_to_start_without_max_tokens(tmp_path):
    from runner import budget
    with pytest.raises(budget.BudgetRequired, match="max-tokens"):
        study.run_items(_ITEMS, tmp_path, model=None, item_kwargs=None, budget=_budget())
    assert not (tmp_path / "results.jsonl").exists()


def test_t8_a_real_model_run_with_max_runs_only_still_refuses(tmp_path):
    from runner import budget
    with pytest.raises(budget.BudgetRequired):
        study.run_items(_ITEMS, tmp_path, budget=_budget(max_runs=2))


def test_t8_max_runs_stops_cleanly_and_records_how_far_it_got(tmp_path):
    b = _budget(max_runs=2, max_tokens=10**9)
    rows = study.run_items(_ITEMS, tmp_path, item_kwargs=kwargs_for, budget=b, timeout_s=_TIMEOUT)
    assert len(rows) == 2 and len(_rows(tmp_path)) == 2
    rec = json.loads((tmp_path / "budget.json").read_text())
    assert rec["stopped_by"] == "max-runs"
    assert rec["planned"] == 4 and rec["completed"] == 2 and rec["max_runs"] == 2
    assert [tuple(x) for x in rec["not_run"]] == [tuple(i) for i in _ITEMS[2:]]
    assert rec["overshoot_runs"] == 0


def test_t8_max_tokens_stops_cleanly_and_records_the_overshoot(tmp_path):
    b = _budget(max_tokens=1)
    rows = study.run_items(_ITEMS, tmp_path, item_kwargs=kwargs_for, budget=b, timeout_s=_TIMEOUT)
    assert len(rows) == 1                      # the first run crossed the limit; nothing else started
    rec = json.loads((tmp_path / "budget.json").read_text())
    used = rows[0]["model_usage"]["input_tokens"] + rows[0]["model_usage"]["output_tokens"]
    assert used > 1
    assert rec["stopped_by"] == "max-tokens"
    assert rec["tokens_used"] == used and rec["overshoot_tokens"] == used - 1
    assert rec["completed"] == 1 and len(rec["not_run"]) == 3


def test_t8_a_budget_not_reached_runs_everything_and_says_so(tmp_path):
    b = _budget(max_runs=100, max_tokens=10**9)
    rows = study.run_items(_ITEMS, tmp_path, item_kwargs=kwargs_for, budget=b, timeout_s=_TIMEOUT)
    assert len(rows) == 4
    rec = json.loads((tmp_path / "budget.json").read_text())
    assert rec["stopped_by"] is None and rec["not_run"] == []


def test_t8_max_tokens_with_workers_overshoots_by_at_most_workers_minus_one_runs(tmp_path):
    items = _ITEMS + [(4, None, "S1", None, 1), (4, None, "S2", None, 1)]
    b = _budget(max_tokens=1)
    rows = study.run_items(items, tmp_path, workers=2, item_kwargs=kwargs_for, budget=b,
                           timeout_s=_TIMEOUT)
    rec = json.loads((tmp_path / "budget.json").read_text())
    assert rec["stopped_by"] == "max-tokens"
    assert 1 <= len(rows) <= 2
    assert rec["overshoot_runs"] == len(rows) - 1 <= 1
    assert rec["completed"] == len(rows) and rec["completed"] + len(rec["not_run"]) == 6


def test_t8_a_batch_with_nothing_that_runs_needs_no_token_budget(tmp_path):
    rows = study.run_items([(1, None, "S10", None, 1)], tmp_path)    # n/a: no run, no model
    assert rows[0]["verdict"] == "n/a"
