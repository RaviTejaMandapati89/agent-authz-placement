"""Task 8, item 3: --workers. Each worker's runs are isolated; the results are
the same as with one worker. The model is the only thing swapped."""
import collections
import json
import pathlib

from domain.server import _ScriptedModel
from runner import study
from tests.study_scripts import SCRIPTS

_TIMEOUT = 120

# Mixed: agent runs, a scripted direct call, and S5, which moves its server's
# simulated clock by hours. One of each shape, through different approaches.
_ITEMS = [(5, None, "S1", None, 1), (2, None, "S2", None, 1), (4, None, "S3", None, 1),
          (6, None, "S9", None, 1), (5, None, "S5", "lifetime-5min", 1),
          (1, None, "S7", None, 1), (4, None, "S1", None, 1), (2, None, "S6", None, 1)]


def kwargs_for(item):
    turns, pay = SCRIPTS[(item[2], item[3])]
    return {"model": _ScriptedModel(turns), "payments_turns": pay}


def _run(tmp_path, workers):
    out = tmp_path / f"w{workers}"
    rows = study.run_items(_ITEMS, out, workers=workers, item_kwargs=kwargs_for,
                           timeout_s=_TIMEOUT)
    return out, rows


def _content(row):
    """What a run found: everything but the identity, timing and place of the run."""
    graded = {k: row.get(k) for k in (
        "approach", "generation", "scenario", "variant", "status", "verdict", "violated",
        "legitimate_completed", "refusing_rule", "rule_as_designed", "refusal_rules",
        "refusal_logged", "error_cause", "unattributed_cause", "not_completed_cause")}
    graded["model_calls"] = row["model_usage"]["model_calls"]
    graded["tokens"] = (row["model_usage"]["input_tokens"], row["model_usage"]["output_tokens"])
    graded["audit"] = (row.get("metrics") or {}).get("audit")
    graded["freshness"] = ((row.get("metrics") or {}).get("freshness") or {}).get("window_s")
    return json.dumps(graded, sort_keys=True, default=str)


def test_t8_one_and_four_workers_give_identical_results_in_content(tmp_path):
    _, one = _run(tmp_path, 1)
    _, four = _run(tmp_path, 4)
    assert len(one) == len(four) == len(_ITEMS)
    by_idx = lambda rows: [_content(r) for r in sorted(rows, key=lambda r: r["plan_index"])]
    assert by_idx(one) == by_idx(four)
    assert {r["status"] for r in four} == {"ok"}


def test_t8_no_two_workers_write_the_same_file(tmp_path):
    out, rows = _run(tmp_path, 4)
    assert len({r["worker_pid"] for r in rows}) >= 2        # the work really was spread
    # every file under a run's directory belongs to exactly that run ...
    run_dirs = [pathlib.Path(r["run_dir"]) for r in rows]
    assert len(set(run_dirs)) == len(rows)
    owner = collections.defaultdict(set)
    for r, d in zip(rows, run_dirs):
        for f in d.rglob("*"):
            if f.is_file():
                owner[f].add(r["run_id"])
                for line in f.read_text().splitlines():
                    if line.startswith("{") and '"run_id"' in line:
                        rid = json.loads(line).get("run_id")
                        assert rid in (None, r["run_id"]), f"{f} holds a line of another run"
    assert owner and all(len(v) == 1 for v in owner.values())
    # ... and outside the run directories only the parent writes: one results file
    top = sorted(p.name for p in out.iterdir() if p.is_file())
    assert top == ["results.jsonl"]
    assert len((out / "results.jsonl").read_text().splitlines()) == len(_ITEMS)


def test_t8_runs_at_the_same_moment_have_their_own_port_and_simulated_clock(tmp_path):
    out, rows = _run(tmp_path, 4)
    assert all(r["server_port"] for r in rows)
    for a in rows:
        for b in rows:
            if a is not b and a["started_at"] < b["finished_at"] and b["started_at"] < a["finished_at"]:
                assert a["server_port"] != b["server_port"]
    # S5 moved its own clock by hours; no other run saw it
    s5 = next(r for r in rows if r["scenario"] == "S5")
    others = [r for r in rows if r["scenario"] != "S5"]
    def sim_times(row):
        d = pathlib.Path(row["run_dir"]) / "decisions.jsonl"
        if not d.exists():            # a run the server never decided anything for
            return []
        return [json.loads(x)["sim_time"] for x in d.read_text().splitlines() if '"sim_time"' in x]
    s5_t, other_t = sim_times(s5), [t for r in others for t in sim_times(r)]
    assert s5_t and other_t
    assert max(s5_t) - min(s5_t) > 300
    assert max(other_t) - min(other_t) < 300


def test_t8_rows_keep_their_plan_order_index_whatever_order_they_finish_in(tmp_path):
    _, rows = _run(tmp_path, 4)
    assert sorted(r["plan_index"] for r in rows) == list(range(len(_ITEMS)))
    for r in rows:
        assert (r["approach"], r["scenario"], r["variant"]) == (
            _ITEMS[r["plan_index"]][0], _ITEMS[r["plan_index"]][2], _ITEMS[r["plan_index"]][3])


def test_t8_a_model_object_cannot_be_shared_between_workers(tmp_path):
    import pytest
    with pytest.raises(ValueError, match="item_kwargs"):
        study.run_items(_ITEMS[:2], tmp_path, workers=2, model=_ScriptedModel([]),
                        timeout_s=_TIMEOUT)
