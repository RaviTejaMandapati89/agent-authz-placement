"""Task 7: summaries per approach and scenario; errors excluded from every
metric; blocked is never n/a; approach 3 per generation and combined."""
import json

from runner import approaches, summary


def _row(approach=5, scenario="S1", variant=None, gen=None, status="ok", verdict="Completed",
         violated=False, legit=True, check=None, tool=None, **extra):
    row = {"run_id": "r", "approach": approach, "approach_label": approaches.label(approach),
           "generation": gen, "scenario": scenario, "variant": variant, "run_num": 1,
           "status": status, "verdict": verdict, "violated": violated,
           "legitimate_completed": legit, "completed": legit is True,
           "input_tokens": 10, "output_tokens": 5,
           "metrics": {"false_refusals": {"refused": 0, "attempted": 1},
                       "audit": {"complete": 1, "total": 1, "share": 1.0},
                       "latency": {"check_ms": check, "tool_ms": tool},
                       "central_calls_per_decision": 0.0}}
    row.update(extra)
    return row


def test_errors_are_excluded_from_every_metric():
    rows = [_row(violated=False, check=[1.0], tool=[1.0]),
            _row(violated=True, verdict="Violated", legit=False, check=[1000.0], tool=[1000.0]),
            _row(status="error", verdict="error", violated=None, legit=None,
                 error_cause="ClientError: x", error_category="model service",
                 metrics=None)]
    # the error row has no metrics at all, as the engine writes it
    rows[2].pop("metrics")
    s = next(x for x in summary.summarise(rows) if x["scenario"] == "S1")
    assert s["runs"] == 3 and s["errors"] == 1 and s["graded"] == 2
    assert s["violation_rate"] == 0.5
    assert s["error_causes"] == ["ClientError: x"]
    assert s["latency_ms"]["check"]["median"] == 500.5


def test_blocked_is_reported_as_blocked_never_na_or_a_verdict():
    rows = [{"approach": 5, "approach_label": approaches.label(5), "generation": None,
             "scenario": "S11", "variant": None, "status": "blocked", "verdict": "blocked"}]
    s = summary.summarise(rows)[0]
    assert s["status"] == "blocked" and s["verdict"] == "blocked"


def test_not_applicable_is_reported_as_na():
    rows = [{"approach": 1, "approach_label": approaches.label(1), "generation": None,
             "scenario": "S10", "variant": None, "status": "n/a", "verdict": "n/a"}]
    assert summary.summarise(rows)[0]["verdict"] == "n/a"


def test_approach_3_is_summarised_per_generation_and_combined():
    rows = [_row(approach=3, gen=g) for g in (1, 2, 3, 4, 5) for _ in range(2)]
    out = [s for s in summary.summarise(rows) if s["approach"] == 3]
    gens = [s["generation"] for s in out]
    assert sorted(g for g in gens if g != "all") == [1, 2, 3, 4, 5]
    combined = next(s for s in out if s["generation"] == "all")
    assert combined["runs"] == 10


def test_variants_are_summarised_separately():
    rows = [_row(scenario="S5", variant="lifetime-5min"), _row(scenario="S5", variant="lifetime-60min")]
    assert {s["variant"] for s in summary.summarise(rows)} == {"lifetime-5min", "lifetime-60min"}


def test_s10_window_that_never_closes_is_reported_as_not_closed_within_horizon():
    cons = {"agree_first": False, "window_s": None, "closed": False, "instants": 64}
    row = _row(approach=4, scenario="S10", verdict="Violated", violated=True, legit=None)
    row["metrics"]["consistency"] = cons
    row["metrics"]["change_cost"] = {"places": 2, "kinds": ["policy file"] * 2, "paths": []}
    s = summary.summarise([row])[0]
    assert s["consistency"]["window_s"] == "not closed within horizon"


def test_summary_files_are_written(tmp_path):
    rows = [_row()]
    summary.write_summary(rows, tmp_path)
    assert json.loads((tmp_path / "summary.json").read_text())[0]["scenario"] == "S1"
    assert "S1" in (tmp_path / "summary.md").read_text()


def test_a27_s5_summary_reports_the_window_bounds_beside_the_verdict():
    fresh = {"window_s": 0.0, "last_allowed_s": 0.0, "first_refused_s": 1.0}
    row = _row(approach=6, scenario="S5", variant="lifetime-60min", verdict="Violated",
               violated=True, legit=True)
    row["metrics"]["freshness"] = fresh
    s = next(x for x in summary.summarise([row]) if x["scenario"] == "S5")
    assert s["verdicts"] == {"Violated": 1}
    assert s["freshness_bounds_s"] == {"last_allowed": 0.0, "first_refused": 1.0}
