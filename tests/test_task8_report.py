"""Task 8, items 6 and 7: the smoke report is a script, and the quota command
names the model id and region the agents use."""
import json

from domain.server import _ScriptedModel
from runner import config as cfg
from runner import study
from tests.study_scripts import SCRIPTS

_TIMEOUT = 120

# One run per shape: refused, violated, completed, an error, a window, n/a.
_ITEMS = [(5, None, "S2", None, 1), (1, None, "S2", None, 1), (5, None, "S1", None, 1),
          (3, 2, "S2", None, 1), (4, None, "S7", None, 1)]


class _Down(_ScriptedModel):
    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        from botocore.exceptions import ClientError
        raise ClientError({"Error": {"Code": "ServiceUnavailableException",
                                     "Message": "simulated outage"}}, "ConverseStream")
        yield {}

    stream.__wrapped__ = True


def kwargs_for(item):
    turns, pay = SCRIPTS[(item[2], item[3])]
    return {"model": (_Down if item[:3] == (4, None, "S7") else _ScriptedModel)(turns),
            "payments_turns": pay}


def _report(tmp_path, **kw):
    from runner import budget, smoke_report
    out = tmp_path / "smoke"
    study.run_items(_ITEMS, out, item_kwargs=kwargs_for, timeout_s=_TIMEOUT,
                    budget=budget.Budget(max_tokens=10**9))
    (out / "plan.json").write_text(json.dumps(_ITEMS))
    return out, smoke_report.build_report(out, **kw)


def test_t8_report_has_a_row_per_run_with_verdict_rules_work_error_time_and_tokens(tmp_path):
    out, rep = _report(tmp_path)
    rows = rep["rows"]
    assert len(rows) == len(_ITEMS)
    refused = next(r for r in rows if r["scenario"] == "S2" and r["approach"] == 5)
    assert refused["verdict"] == "Refused" and refused["refusing_rule"]
    assert refused["rule_as_designed"] is True and refused["wall_s"] > 0
    assert refused["input_tokens"] > 0 and refused["model_calls"] > 0
    violated = next(r for r in rows if r["scenario"] == "S2" and r["approach"] == 1)
    assert violated["verdict"] == "Violated"
    errored = next(r for r in rows if r["scenario"] == "S7")
    assert errored["verdict"] == "error" and "simulated outage" in errored["error_cause"]
    completed = next(r for r in rows if r["scenario"] == "S1")
    assert completed["legitimate_work"] == "completed"
    gen = next(r for r in rows if r["approach"] == 3)
    assert gen["generation"] == 2


def test_t8_report_totals_budget_and_the_bedrock_model_id_note(tmp_path):
    out, rep = _report(tmp_path)
    t = rep["totals"]
    assert t["runs"] == 5 and t["errors"] == 1
    assert t["input_tokens"] == sum(r["input_tokens"] for r in rep["rows"])
    assert t["wall_s"] > 0 and t["model_calls"] > 0
    assert rep["budget"]["stopped_by"] is None and rep["budget"]["completed"] == 5
    text = rep["markdown"]
    assert "Bedrock does not return the answering model's id" in text
    assert cfg.MODEL_ID in text
    assert "| S2 |" in text and "Refused" in text


def test_t8_report_lists_planned_runs_that_have_no_row_as_not_run(tmp_path):
    from runner import smoke_report
    out, rep = _report(tmp_path)
    plan = json.loads((out / "plan.json").read_text())
    (out / "plan.json").write_text(json.dumps(plan + [[5, None, "S3", None, 1]]))
    rep = smoke_report.build_report(out)
    assert rep["not_run"] == [[5, None, "S3", None, 1]]


def test_t8_report_estimates_the_official_run_at_each_probed_concurrency_level(tmp_path):
    from runner import smoke_report
    out, _ = _report(tmp_path)
    probe = {"levels": [
        {"workers": 1, "runs": 16, "wall_s": 160.0, "runs_per_min": 6.0, "throttled": 0,
         "errors": 0, "tokens": 1000},
        {"workers": 2, "runs": 16, "wall_s": 90.0, "runs_per_min": 10.0, "throttled": 0,
         "errors": 0, "tokens": 1000}],
        "stopped_at": {"workers": 2, "reason": "throttling"}, "safe_level": 1}
    (tmp_path / "probe.json").write_text(json.dumps(probe))
    rep = smoke_report.build_report(out, probe_path=tmp_path / "probe.json", official_runs=10)
    est = rep["estimates"]
    assert [e["workers"] for e in est] == [1, 2]
    n = rep["official"]["runs"]
    assert n > 0
    assert est[0]["minutes"] == round(n / 6.0, 1) and est[1]["minutes"] == round(n / 10.0, 1)
    assert est[0]["tokens"] == est[1]["tokens"] > 0
    assert "workers" in rep["markdown"] and "throttling" in rep["markdown"]


def test_t8_quota_commands_name_the_pinned_model_id_and_region_from_the_code():
    from runner import quota
    cmds = quota.commands()
    assert len(cmds) >= 2
    text = "\n".join(cmds)
    assert cfg.MODEL_ID in text and cfg.AWS_REGION in text
    assert all(c.startswith("aws ") for c in cmds)
    # read-only: only list and get verbs
    assert all(("list-" in c or "get-" in c) for c in cmds)
    assert not any(v in text for v in ("request-service-quota-increase", "put-", "delete-", "update-"))
