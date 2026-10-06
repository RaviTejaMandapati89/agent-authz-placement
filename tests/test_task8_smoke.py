"""Task 8, item 1 and D6, D7: the smoke matrix and its plan check."""
import json
import subprocess

import pytest

from runner import approaches, scenarios

_REPO = __import__("pathlib").Path(__file__).parent.parent


def _smoke():
    from runner import smoke
    return smoke


@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    """Three scenarios in a temporary directory, with a hand-counted plan:
    S98 runs on 3 and 5 with two variants: approach 3 once per generation and
    variant (2 x 5) plus approach 5 once per variant (2) = 12. S97 has no
    variants and applies to 1, 2 and 4: 3. S99 is blocked: 0. Total 15."""
    d = tmp_path / "scenarios"
    d.mkdir()
    common = "setup: []\nexpected: {outcome: Refused}\n"
    (d / "S98.yaml").write_text(
        "id: S98\ntitle: t\napproaches: [3, 5]\nvariants: {a: {}, b: {}}\n" + common)
    (d / "S97.yaml").write_text("id: S97\ntitle: t\napproaches: [1, 2, 4]\n" + common)
    (d / "S99.yaml").write_text(
        "id: S99\ntitle: t\nstatus: blocked\nblocked_reason: x\napproaches: [4, 5, 6]\n" + common)
    monkeypatch.setattr(scenarios, "SCENARIOS_DIR", d)
    return d


def test_t8_smoke_plan_is_every_scenario_variant_and_approach_once_and_3_per_generation(synthetic):
    items = _smoke().smoke_items()
    assert len(items) == 15
    assert len(set(items)) == 15
    assert sum(1 for i in items if i[0] == 3) == 10
    assert not any(i[2] == "S99" for i in items)
    assert {i[4] for i in items} == {1}


def test_t8_the_expected_count_comes_from_the_scenario_files_not_from_the_plan(synthetic):
    assert _smoke().expected_run_count() == 15


def test_t8_the_real_scenario_files_plan_matches_the_count_and_includes_s11_pairs_per_approach():
    smoke = _smoke()
    items = smoke.smoke_items()
    assert len(items) == smoke.expected_run_count() > 0
    s11 = [i for i in items if i[2] == "S11"]
    assert len(s11) == 15                                    # D1: 5 pairs x approaches 4, 5, 6
    gens = {i[1] for i in items if i[0] == 3}
    assert gens == set(approaches.GENERATIONS)


def test_t8_smoke_prints_the_planned_count_before_anything_runs(synthetic, tmp_path, capsys):
    rc = _smoke().main(["--dry-run", "--out", str(tmp_path / "o")])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.splitlines()[0] == "planned runs: 15"
    assert not (tmp_path / "o" / "results.jsonl").exists()


def test_t8_smoke_refuses_to_start_when_the_plan_differs_from_the_scenario_files(
        synthetic, tmp_path, monkeypatch, capsys):
    from runner import study
    real = study.plan
    monkeypatch.setattr(study, "plan", lambda *a, **k: list(real(*a, **k))[1:])   # one run lost
    with pytest.raises(SystemExit) as e:
        _smoke().main(["--out", str(tmp_path / "o"), "--max-tokens", "1000"])
    assert e.value.code not in (0, None)
    err = capsys.readouterr()
    assert "14" in err.out + err.err and "15" in err.out + err.err
    assert not (tmp_path / "o" / "results.jsonl").exists()


def test_t8_smoke_defaults_to_one_worker():
    assert _smoke().parse_args(["--max-tokens", "5"]).workers == 1


def test_t8_smoke_with_the_real_model_refuses_without_max_tokens(synthetic, tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        _smoke().main(["--out", str(tmp_path / "o")])
    assert e.value.code not in (0, None)
    assert "max-tokens" in capsys.readouterr().err
    assert not (tmp_path / "o" / "results.jsonl").exists()


def test_t8_smoke_output_goes_to_a_dated_directory_that_git_ignores():
    import datetime
    d = _smoke().default_out_dir(datetime.date(2026, 10, 6))
    assert d == _REPO / "results" / "smoke-2026-10-06"
    r = subprocess.run(["git", "check-ignore", "-q", str(d / "results.jsonl")], cwd=_REPO)
    assert r.returncode == 0


def test_t8_smoke_writes_its_plan_beside_the_results_so_the_report_knows_what_was_planned(
        synthetic, tmp_path):
    out = tmp_path / "o"
    _smoke().main(["--dry-run", "--out", str(out)])
    plan = json.loads((out / "plan.json").read_text())
    assert len(plan) == 15
