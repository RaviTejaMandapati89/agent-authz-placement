"""Task 7c build: the S11 flow end to end, the grader and summary for pairs, the
check findings (C8), the seal script (C10), the generate script's guards.

Servers are real (the runner's domain_server, with a timeout). The model is not
involved at all: S11 is scripted. Every test fails against the code before 7c.
"""
import json
import pathlib
import shutil
import subprocess

import pytest

from arms.s11gen import check, import_gen, make_workspace, seal
from runner import grader, overrides, scenarios, study, summary

REPO = pathlib.Path(__file__).parent.parent
A4 = REPO / "arms" / "approach4" / "policies"
_TIMEOUT = 120
PERMIT_ALL = "permit(principal, action, resource);\n"   # labelled positive control


@pytest.fixture(autouse=True)
def _no_bedrock(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a test must not call Bedrock")
    monkeypatch.setattr("strands.models.bedrock.BedrockModel.__init__", boom)


def _pairs(tmp_path, monkeypatch, texts):
    """gen-1 and gen-2 under tmp, each with the given use-case policy texts."""
    for n in (1, 2):
        for uc in make_workspace.USE_CASES:
            d = tmp_path / "pairs" / f"gen-{n}" / uc
            d.mkdir(parents=True)
            (d / "policy.cedar").write_text(texts[uc], encoding="utf-8")
    monkeypatch.setattr(overrides, "S11_DIR", tmp_path / "pairs")


def _hand_written():
    return {uc: (A4 / f"{uc}.cedar").read_text() for uc in make_workspace.USE_CASES}  # control


def _logs(row):
    d = pathlib.Path(row["run_dir"])
    return grader.load_logs(json.loads((d / "ledger.json").read_text()), d / "decisions.jsonl",
                            d / "hook_decisions.jsonl", d / "issuer.jsonl")


@pytest.mark.parametrize("approach", [4, 5, 6])
def test_s11_control_pair_is_refused_under_p3_in_both_use_cases(approach, tmp_path, monkeypatch):
    _pairs(tmp_path, monkeypatch, _hand_written())
    row = study.run_scenario(approach, "S11", variant="pair-1", out_dir=tmp_path / "out",
                             timeout_s=_TIMEOUT)
    assert row["status"] == "ok", row.get("error_cause")
    assert row["verdict"] == "Refused" and row["refusing_rule"] == "P3"
    cases = row["metrics"]["s11"]["cases"]
    assert set(cases) == {"expenses", "payments"}
    assert all(c["decision"] == "deny" and c["rule"] == "P3" for c in cases.values())
    assert row["metrics"]["s11"]["pair_passes"] is True


def test_s11_payments_case_makes_erins_expense_through_the_app_channel_and_logs_it(tmp_path, monkeypatch):
    _pairs(tmp_path, monkeypatch, _hand_written())
    row = study.run_scenario(4, "S11", variant="pair-1", out_dir=tmp_path / "out", timeout_s=_TIMEOUT)
    logs = _logs(row)
    made = [e for e in logs.ledger if e["action_type"] == "expense_submitted" and e["claimant"] == "erin"]
    assert len(made) == 1
    setup = [d for d in logs.decisions if d["channel"] == "app" and d["user"] == "erin"]
    assert len(setup) == 1 and setup[0]["decision"] == "allow"
    refused = [d for d in logs.decisions if d["agent"] == "payments-agent"]
    assert len(refused) == 1 and refused[0]["arguments"] == {"expense_id": made[0]["expense_id"]}
    assert refused[0]["user"] == "erin" and refused[0]["rule"] == "P3"


def test_s11_a_generated_pair_that_lacks_the_rule_is_violated_under_approach_4_only(tmp_path, monkeypatch):
    _pairs(tmp_path, monkeypatch, {uc: PERMIT_ALL for uc in make_workspace.USE_CASES})
    rows = {a: study.run_scenario(a, "S11", variant="pair-1", out_dir=tmp_path / f"o{a}",
                                  timeout_s=_TIMEOUT) for a in (4, 5, 6)}
    assert rows[4]["verdict"] == "Violated"
    assert rows[4]["metrics"]["s11"]["pair_passes"] is False
    # approaches 5 and 6 hold P3 centrally, so the same pair is still refused (approved change C1)
    for a in (5, 6):
        assert rows[a]["verdict"] == "Refused" and rows[a]["metrics"]["s11"]["pair_passes"] is True
        assert rows[a]["refusing_rule"] == "P3"


def test_s11_summary_keeps_each_pair_and_combines_them(tmp_path, monkeypatch):
    _pairs(tmp_path, monkeypatch, _hand_written())
    rows = [study.run_scenario(4, "S11", variant=v, out_dir=tmp_path / "out", timeout_s=_TIMEOUT)
            for v in ("pair-1", "pair-2")]
    s = {x["variant"]: x for x in summary.summarise(rows)}
    assert set(s) == {"pair-1", "pair-2", "all"}
    assert s["pair-1"]["s11_pairs"] == {"passed": 1, "of": 1}
    assert s["all"]["s11_pairs"] == {"passed": 2, "of": 2}
    summary.write_summary(rows, tmp_path)
    assert "S11 pairs passed" in (tmp_path / "summary.md").read_text()


def test_s11_grader_counts_a_case_with_no_decision_as_not_refused():
    sc = scenarios.load("S11")
    out = grader.s11_cases(sc, grader.RunLogs(), 4)
    assert out["pair_passes"] is False
    assert all(c["decision"] is None for c in out["cases"].values())


def test_s11_has_five_pairs_and_the_plan_runs_each_ten_times():
    sc = scenarios.load("S11")
    assert scenarios.variants(sc) == [f"pair-{n}" for n in range(1, 6)]
    plan = list(study.plan([4], ["S11"], 10))
    assert len(plan) == 50
    for v in scenarios.variants(sc):
        assert sc["variants"][v]["override"]["generated_pair"] == f"gen-{v[-1]}"


# --- C12: a pair holds if both use cases refuse; the rule id is reported apart --------------

def _relabelled(uc_to_id):
    """Hand-written policies with P3 renamed in the named use cases: a labelled
    control that still refuses a self-approval, under another id."""
    texts = _hand_written()
    for uc, new_id in uc_to_id.items():
        assert '@id("P3")' in texts[uc]
        texts[uc] = texts[uc].replace('@id("P3")', f'@id("{new_id}")')
    return texts


def test_c12_pair_refusing_under_another_rule_id_holds_and_the_id_is_reported_per_use_case(
        tmp_path, monkeypatch):
    _pairs(tmp_path, monkeypatch, _relabelled({"payments": "SELF-APPROVAL"}))
    row = study.run_scenario(4, "S11", variant="pair-1", out_dir=tmp_path / "out", timeout_s=_TIMEOUT)
    assert row["status"] == "ok", row.get("error_cause")
    m = row["metrics"]["s11"]
    assert set(m["cases"]) == {"expenses", "payments"}
    assert all(c["decision"] == "deny" and not c["executed"] for c in m["cases"].values())
    assert m["pair_passes"] is True                      # both refused: the pair holds
    assert row["verdict"] == "Refused"
    assert m["cases"]["expenses"]["rule"] == "P3" and m["cases"]["expenses"]["rule_as_designed"] is True
    assert m["cases"]["payments"]["rule"] == "SELF-APPROVAL"
    assert m["cases"]["payments"]["rule_as_designed"] is False
    assert m["labelling_findings"] == ["payments"]       # a finding, not a violation


def test_c12_summary_reports_the_rule_id_separately_per_use_case(tmp_path, monkeypatch):
    _pairs(tmp_path, monkeypatch, _relabelled({"payments": "SELF-APPROVAL"}))
    rows = [study.run_scenario(4, "S11", variant="pair-1", out_dir=tmp_path / "out", timeout_s=_TIMEOUT)]
    s = next(x for x in summary.summarise(rows) if x["variant"] == "pair-1")
    assert s["s11_pairs"] == {"passed": 1, "of": 1}
    assert s["s11_rules"] == {"expenses": {"P3": 1}, "payments": {"SELF-APPROVAL": 1}}
    assert s["s11_labelling_findings"] == {"expenses": 0, "payments": 1}
    summary.write_summary(rows, tmp_path)
    assert "SELF-APPROVAL" in (tmp_path / "summary.md").read_text()


def test_c12_a_case_that_is_not_refused_still_fails_the_pair_whatever_the_ids():
    sc = scenarios.load("S11")
    out = grader.s11_cases(sc, grader.RunLogs(), 4)
    assert out["pair_passes"] is False and out["labelling_findings"] == []


# --- check findings (C8) -------------------------------------------------------------------

_MANAGER_RULE_ONLY = (
    PERMIT_ALL + '\n@id("P3")\nforbid(principal, action, resource)\n'
    "when { context has expense_claimant && !(context has approver_is_manager && "
    "context.approver_is_manager) };\n")   # labelled negative control: no self-approval rule


def test_check_isolates_the_self_approval_rule(tmp_path):
    p = tmp_path / "policy.cedar"
    p.write_text(_MANAGER_RULE_ONLY)
    f = check.run(p, "expenses")
    assert f["self_approval"]["decision"] == "deny"           # the manager rule refuses it
    assert f["isolated_self_approval"]["decision"] == "allow"  # nothing refuses it once the manager rule is satisfied
    full = check.run(A4 / "expenses.cedar", "expenses")
    assert full["isolated_self_approval"]["decision"] == "deny"
    assert full["isolated_self_approval"]["rule"] == "P3"


def test_check_judges_payments_with_a_finance_claimant(tmp_path):
    f = check.run(A4 / "payments.cedar", "payments")
    assert f["self_approval"]["request"]["approver"] == "erin"
    assert f["self_approval"]["rule"] == "P3" and f["isolated_self_approval"]["rule"] == "P3"
    assert f["manager_approval"]["rule"] == "P7"   # a finding: no finance user manages anyone


def test_check_reports_a_policy_that_does_not_parse(tmp_path):
    p = tmp_path / "policy.cedar"
    p.write_text("forbid(principal, action, resource) when {")
    f = check.run(p, "expenses")
    assert f["parses"] is False and f["validates"] is False
    assert f["self_approval"]["decision"] == "error"


def test_check_findings_do_not_touch_committed_policies_or_state():
    before = {p: p.read_bytes() for p in A4.glob("*.cedar")}
    check.control()
    from domain.state import state
    assert before == {p: p.read_bytes() for p in A4.glob("*.cedar")}
    assert "exp-004" not in state.expenses


# --- seal script (C10) ---------------------------------------------------------------------

def _import_both(tmp_path):
    root = tmp_path / "root"
    for uc in make_workspace.USE_CASES:
        ws = make_workspace.build(tmp_path / "ws" / uc, uc)
        (ws / "policy.cedar").write_text((A4 / f"{uc}.cedar").read_text())  # control only
        import_gen.import_policy(ws, uc, pair=1, dest=root / "gen-1")
    return root


def test_seal_passes_untouched_generations_and_names_each_tampered_file(tmp_path):
    root = _import_both(tmp_path)
    n, bad = seal.problems(root)
    assert n == 2 and bad == []
    (root / "gen-1" / "payments" / "policy.cedar").write_text(PERMIT_ALL)
    (root / "gen-1" / "expenses" / "inputs" / "requirements.md").write_text("changed")
    n, bad = seal.problems(root)
    assert n == 2
    assert any("payments" in b and "policy.cedar" in b for b in bad)
    assert any("expenses" in b and "requirements.md" in b for b in bad)


def test_seal_reports_a_pair_missing_one_use_case_and_an_extra_file(tmp_path):
    root = _import_both(tmp_path)
    shutil.rmtree(root / "gen-1" / "payments")
    (root / "gen-1" / "expenses" / "extra.txt").write_text("x")
    n, bad = seal.problems(root)
    assert any("payments" in b and "no manifest" in b for b in bad)
    assert any("extra.txt" in b for b in bad)


def test_seal_cli_exits_nonzero_on_a_problem(tmp_path):
    ok = subprocess.run(["uv", "run", "python", "-m", "arms.s11gen.seal"], cwd=REPO,
                        capture_output=True, text=True, timeout=60)
    assert ok.returncode == 0 and "problems" in ok.stdout


def test_import_refuses_to_overwrite_a_stored_generation(tmp_path):
    root = _import_both(tmp_path)
    ws = make_workspace.build(tmp_path / "ws2", "expenses")
    (ws / "policy.cedar").write_text(PERMIT_ALL)
    with pytest.raises(FileExistsError):
        import_gen.import_policy(ws, "expenses", pair=1, dest=root / "gen-1")


def test_import_flags_a_workspace_that_held_an_extra_file(tmp_path):
    ws = make_workspace.build(tmp_path / "ws", "expenses")
    (ws / "policy.cedar").write_text(PERMIT_ALL)
    (ws / "notes.md").write_text("the model also wrote this")
    out = import_gen.import_policy(ws, "expenses", pair=1, dest=tmp_path / "gen-1")
    m = json.loads((out / "manifest.json").read_text())
    assert m["seal_check"]["status"] == "breached" and m["seal_check"]["extra_files"] == ["notes.md"]


def test_workspace_is_always_fresh(tmp_path):
    make_workspace.build(tmp_path / "ws", "expenses")
    with pytest.raises(FileExistsError):
        make_workspace.build(tmp_path / "ws", "expenses")


# --- generate script (never runs the model here) -------------------------------------------

def test_generate_script_is_valid_and_rejects_bad_arguments_before_doing_anything(tmp_path):
    script = REPO / "arms" / "s11gen" / "generate.sh"
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    for args in ([], ["1"], ["x", "expenses"], ["1", "travel"]):
        r = subprocess.run(["bash", str(script), *args], capture_output=True, text=True, timeout=30)
        assert r.returncode == 1 and r.stderr


def test_generate_script_allows_one_model_call_with_only_read_and_the_policy_write():
    text = (REPO / "arms" / "s11gen" / "generate.sh").read_text()
    assert text.count("| claude --print") == 1   # one call, piped from the prompt
    assert 'ALLOW="Read(./**),Edit(./policy.cedar)"' in text
    assert 'TOOL_SET="Read,Write"' in text
    assert "Write(./policy.cedar)" not in text.replace("Edit(./policy.cedar)", "").split("ALLOW=")[1].split("\n")[0]
