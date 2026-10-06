"""Approved changes A17 to A23, after the task 7 build.

Each test fails against the code before these changes, except the one labelled
"restored coverage" (A21), which restores a check that an earlier change
weakened. The model is the only thing substituted; servers are real, started
through the runner's domain_server. Nothing here calls Bedrock.
"""
import json
import pathlib

import jwt
import pytest

from domain import simclock, tokens
from domain.server import _ScriptedModel
from runner import grader, scenarios, study, summary
from runner.grader import RunLogs
from tests.study_scripts import SCRIPTS

_REPO = pathlib.Path(__file__).parent.parent
_TIMEOUT = 120


@pytest.fixture(autouse=True)
def _no_bedrock(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a test must not call Bedrock")
    monkeypatch.setattr("strands.models.bedrock.BedrockModel.__init__", boom)


def _run(approach, sid, variant=None, *, gen=None, tmp_path):
    turns, pay = SCRIPTS[(sid, variant)]
    return study.run_scenario(
        approach, sid, variant=variant, gen=gen if gen else (1 if approach == 3 else None),
        out_dir=tmp_path, model=_ScriptedModel(turns), payments_turns=pay,
        timeout_s=_TIMEOUT)


def _logs(row) -> RunLogs:
    d = pathlib.Path(row["run_dir"])
    return grader.load_logs(json.loads((d / "ledger.json").read_text()), d / "decisions.jsonl",
                            d / "hook_decisions.jsonl", d / "issuer.jsonl")


def _fresh(row) -> dict:
    return row["metrics"]["freshness"]


# ---------------------------------------------------------------------------
# A17: S5 probes at +0; the window is two numbers
# ---------------------------------------------------------------------------

def test_a17_s5_at_60_minutes_5_refuses_at_plus_0_6_allows_at_plus_0_and_refuses_by_plus_1(tmp_path):
    f = {a: _fresh(_run(a, "S5", "lifetime-60min", tmp_path=tmp_path)) for a in (4, 5, 6)}
    # approach 5 asks the central service live: refused in the same simulated second
    assert f[5]["last_allowed_s"] is None and f[5]["first_refused_s"] == 0
    # approach 6 receives the revocation 1 simulated second later
    assert f[6]["last_allowed_s"] == 0 and f[6]["first_refused_s"] == 1
    # approach 4 allows until the token expires: refused at exactly exp (3600), allowed at the
    # last probe before it (A32)
    assert f[4]["last_allowed_s"] == 3540 and f[4]["first_refused_s"] == 3600


def test_a17_s5_probe_at_plus_0_is_in_the_scenario_and_is_made(tmp_path):
    assert scenarios.load("S5")["probes"]["offsets"][0] == 0
    row = _run(5, "S5", "lifetime-5min", tmp_path=tmp_path)
    logs = _logs(row)
    rev = next(e for e in logs.ledger if e["action_type"] == "delegation_revoked")
    at_zero = [d for d in logs.decisions if d["tool"] == "book_travel"
               and d["seq"] > rev["seq"] and d["sim_time"] == rev["sim_time"]]
    assert len(at_zero) == 1 and at_zero[0]["decision"] == "deny"


def test_a17_s5_approach_2_probe_at_plus_0_is_refused_by_the_checkpoint(tmp_path):
    row = _run(2, "S5", "lifetime-5min", tmp_path=tmp_path)
    f = _fresh(row)
    assert f["last_allowed_s"] is None and f["first_refused_s"] == 0
    probes = [d for d in _logs(row).hook_decisions if d["source"] == "probe"]
    assert probes and probes[0]["sim_time"] == f["revoked_at"]


def test_a17_freshness_window_reports_last_allowed_and_first_refused_from_the_logs():
    ledger = [
        {"seq": 1, "action_type": "travel_booked", "sim_time": 0.0},
        {"seq": 2, "action_type": "delegation_revoked", "sim_time": 0.0},
        {"seq": 4, "action_type": "travel_booked", "sim_time": 0.0},    # allowed at +0
    ]
    server = [
        {"type": "decision", "layer": "gateway", "seq": 3, "tool": "book_travel",
         "decision": "allow", "sim_time": 0.0},
        {"type": "decision", "layer": "gateway", "seq": 5, "tool": "book_travel",
         "decision": "deny", "rule": "P4", "sim_time": 1.0},
    ]
    out = grader.freshness_window(RunLogs(ledger=ledger, server_log=server))
    assert out["last_allowed_s"] == 0 and out["first_refused_s"] == 1
    assert out["window_s"] == 0


def test_a17_a_refusal_before_the_revocation_is_not_a_probe_refusal():
    ledger = [{"seq": 2, "action_type": "delegation_revoked", "sim_time": 0.0}]
    server = [{"type": "decision", "layer": "gateway", "seq": 1, "tool": "book_travel",
               "decision": "deny", "rule": "P4", "sim_time": 0.0}]
    out = grader.freshness_window(RunLogs(ledger=ledger, server_log=server))
    assert out["first_refused_s"] is None


# ---------------------------------------------------------------------------
# A18: approach 6 publishes its first copy at server start
# ---------------------------------------------------------------------------

def test_a18_first_copy_is_published_by_reset_before_any_decision():
    from arms.approach6 import plugin
    from domain import state
    state.reset()
    plugin.reset()
    assert plugin._snapshot is not None and plugin._snapshot["version"] == 1


def test_a18_a_limit_change_after_start_is_not_in_the_first_copy():
    from arms.approach6 import plugin
    from domain import state
    state.reset()
    plugin.reset()
    state.state.set_expense_limit(100)          # central change; nothing published yet
    claims = {"sub": "alice", "role": "employee", "reports_to": [], "delegations_received": []}
    result = plugin.evaluate(claims, ["expense-assistant"], "submit_expense",
                             {"claimant": "alice", "amount": 400.0, "description": "x"})
    assert result["decision"] == "allow"        # the copy from start still says 500
    assert result["central_copy_version"] == 1


def test_a18_no_scenario_has_a_warm_up_step():
    for sid in scenarios.ids():
        steps = scenarios.load(sid).get("setup") or []
        assert all(s.get("action") != "warm-up" for s in steps), sid
    assert "warm-up" not in (_REPO / "runner" / "study.py").read_text()


def test_a18_s8_approach_6_has_a_copy_without_any_warm_up(tmp_path):
    row = _run(6, "S8", tmp_path=tmp_path)
    assert row["status"] == "ok"
    assert row["metrics"]["failure_behaviour"] == grader.CONTINUED_LAST_COPY


def test_a18_s10_approach_6_agent_is_allowed_by_the_copy_from_start(tmp_path):
    row = _run(6, "S10", tmp_path=tmp_path)
    c = row["metrics"]["consistency"]
    assert c["agree_first"] is False and c["window_s"] == 900


# ---------------------------------------------------------------------------
# A19: S15 premise and the third variant
# ---------------------------------------------------------------------------

def test_a19_s15_file_labels_the_premise_and_keeps_the_current_setup_as_a_variant():
    s = scenarios.load("S15")
    assert s["premise"]["agent"] == "expense-assistant"
    assert "payments:pay" in s["premise"]["scopes"] and "pay_vendor" in s["premise"]["tools"]
    assert set(s["variants"]) == {"refusal", "control", "scope"}
    assert scenarios.resolve(s, "scope").get("premise") is None
    assert scenarios.resolve(s, "refusal")["premise"] == s["premise"]
    assert scenarios.resolve(s, "control")["premise"] == s["premise"]


@pytest.mark.parametrize("approach", [2, 4, 5, 6])
def test_a19_with_the_premise_only_the_person_refuses(approach, tmp_path):
    row = _run(approach, "S15", "refusal", tmp_path=tmp_path)
    assert row["status"] == "ok", row.get("error_cause")
    assert row["verdict"] == "Refused"
    assert row["metrics"]["chains"]["refusing_rule"] == "P7"
    assert row["metrics"]["chains"]["refusing_hop"] == "payments-agent"


def test_a19_the_premise_scopes_are_in_the_first_agents_token_and_only_there(tmp_path):
    held = _run(4, "S15", "refusal", tmp_path=tmp_path)
    without = _run(4, "S15", "scope", tmp_path=tmp_path)

    def first_scopes(row):
        issued = [e for e in _logs(row).issuer_log
                  if e["type"] == "issued" and e.get("sub") == "expense-assistant"]
        return set(issued[0]["scope"]) if issued else set()
    assert "payments:pay" in first_scopes(held)
    assert "payments:pay" not in first_scopes(without)


def test_a19_scope_variant_is_refused_before_the_person_is_asked(tmp_path):
    row = _run(4, "S15", "scope", tmp_path=tmp_path)
    assert row["verdict"] == "Refused"
    assert row["metrics"]["chains"]["refusing_rule"] in ("SCOPE", "GRANT")


def test_a19_control_still_completes_under_the_premise(tmp_path):
    for approach in (2, 4):
        row = _run(approach, "S15", "control", tmp_path=tmp_path)
        assert row["verdict"] == "Completed", (approach, row.get("error_cause"))


def test_a19_the_premise_is_per_run_and_committed_files_are_untouched(tmp_path):
    from domain.grants import AGENT_GRANTS
    cfg = _REPO / "arms/c_hook/config/expense-assistant.yaml"
    before = cfg.read_bytes()
    _run(2, "S15", "refusal", tmp_path=tmp_path)
    assert cfg.read_bytes() == before
    assert "pay_vendor" not in AGENT_GRANTS["expense-assistant"]


# ---------------------------------------------------------------------------
# A20: "not offered" is not "refused"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sid,tool", [("S4", "pay_vendor"), ("S6", "export_all_expenses")])
@pytest.mark.parametrize("approach", [4, 5, 6])
def test_a20_a_gateway_that_never_offers_the_tool_is_recorded_not_offered(sid, tool, approach, tmp_path):
    row = _run(approach, sid, tmp_path=tmp_path)
    assert row["status"] == "ok", row.get("error_cause")
    assert row["verdict"] == grader.NOT_OFFERED and row["violated"] is False
    logs = _logs(row)
    assert logs.tools_lists and all(tool not in e["offered"] for e in logs.tools_lists)
    assert not any(d["tool"] == tool for d in logs.decisions)


def test_a20_a_tool_that_was_offered_and_refused_is_a_refusal():
    scenario = {"tool_under_test": "pay_vendor", "expected": {"outcome": "Refused", "violation": []}}
    offered = RunLogs(server_log=[
        {"type": "tools_list", "offered": ["pay_vendor"]},
        {"type": "decision", "layer": "gateway", "tool": "pay_vendor", "decision": "deny",
         "rule": "P6", "seq": 2}])
    assert grader.grade_run(scenario, offered, 4)["verdict"] == "Refused"
    hidden = RunLogs(server_log=[{"type": "tools_list", "offered": ["read_receipt"]}])
    assert grader.grade_run(scenario, hidden, 4)["verdict"] == grader.NOT_OFFERED
    no_gateway = RunLogs(hook_log=[{"call_id": "c1", "tool": "pay_vendor", "decision": "deny",
                                    "rule": "P6"}])
    assert grader.grade_run(scenario, no_gateway, 2)["verdict"] == "Refused"


def test_a20_summary_reports_not_offered_apart_from_refused(tmp_path):
    offered = _run(4, "S4", tmp_path=tmp_path)
    s = next(x for x in summary.summarise([offered]) if x["scenario"] == "S4")
    assert s["verdicts"] == {grader.NOT_OFFERED: 1} and s["violation_rate"] == 0.0


# ---------------------------------------------------------------------------
# A22: iat and nbf are compared with the simulated clock
# ---------------------------------------------------------------------------

def _obo_token(**kw):
    return tokens._USER_ISSUER.issue(
        "alice", tokens.SERVER_AUDIENCE, ["expenses:submit"], 300,
        extra={"act": {"sub": "expense-assistant"}, **kw.pop("extra", {})}, **kw)


def test_a22_a_token_issued_in_the_future_is_refused():
    now = simclock.now()
    token = _obo_token(now=now + 1000)
    with pytest.raises(jwt.InvalidTokenError):
        tokens.verify_bearer(token)
    simclock.advance(1000)
    assert tokens.verify_bearer(token)["sub"] == "alice"     # valid once the clock reaches iat


def test_a22_a_token_not_yet_valid_is_refused():
    now = int(simclock.now())
    token = _obo_token(extra={"nbf": now + 200})
    with pytest.raises(jwt.InvalidTokenError):
        tokens.verify_bearer(token)
    simclock.advance(200)
    assert tokens.verify_bearer(token)["sub"] == "alice"


# ---------------------------------------------------------------------------
# A23: lines are ordered by a per-process sequence number
# ---------------------------------------------------------------------------

def test_a23_next_seq_is_strictly_increasing():
    a, b, c = simclock.next_seq(), simclock.next_seq(), simclock.next_seq()
    assert a < b < c


def test_a23_server_lines_and_ledger_share_one_sequence(tmp_path):
    row = _run(5, "S5", "lifetime-5min", tmp_path=tmp_path)
    logs = _logs(row)
    d = pathlib.Path(row["run_dir"])
    issuer = logs.issuer_log
    assert logs.ledger and logs.server_log and issuer
    for name, lines in (("ledger", logs.ledger), ("decisions", logs.server_log), ("issuer", issuer)):
        seqs = [e["seq"] for e in lines]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), name
    everything = [e["seq"] for e in logs.ledger + logs.server_log + issuer]
    assert len(set(everything)) == len(everything)
    # the revocation sits between the first booking and the +0 probe, though all
    # three happened in the same simulated second
    rev = next(e for e in logs.ledger if e["action_type"] == "delegation_revoked")
    books = [e for e in logs.decisions if e["tool"] == "book_travel"]
    assert books[0]["seq"] < rev["seq"] < books[1]["seq"]
    assert books[0]["sim_time"] == rev["sim_time"] == books[1]["sim_time"]
    assert (d / "decisions.jsonl").exists()


def test_a23_false_refusals_order_by_sequence_not_by_wall_clock():
    scenario = {"expected": {"legitimate_calls": [
        {"tool": "book_travel", "before_event": {"action_type": "delegation_revoked"}}]}}
    ledger = [{"seq": 10, "action_type": "delegation_revoked", "sim_time": 0.0,
               "timestamp": "2025-01-01T00:00:01"}]
    # written before the revocation (seq 5) but stamped later by a wall clock that stepped
    before = {"type": "decision", "layer": "gateway", "seq": 5, "tool": "book_travel",
              "decision": "deny", "rule": "P4", "timestamp": "2025-01-01T00:00:09"}
    after = {"type": "decision", "layer": "gateway", "seq": 11, "tool": "book_travel",
             "decision": "deny", "rule": "P4", "timestamp": "2025-01-01T00:00:00"}
    out = grader.false_refusals(scenario, RunLogs(ledger=ledger, server_log=[before, after]))
    assert out == {"refused": 1, "attempted": 1}


# ---------------------------------------------------------------------------
# A3 / A25 (3): gateway decision lines merge into their run by run_id
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("approach", [4, 5, 6])
def test_a3_merge_decision_log_finds_the_gateway_calls_of_the_run(approach, tmp_path):
    row = _run(approach, "S1", tmp_path=tmp_path)
    lines = grader.read_jsonl(pathlib.Path(row["run_dir"]) / "decisions.jsonl")
    merged = grader.merge_decision_log(lines, row["run_id"])
    gateway_calls = {d["call_id"] for d in lines if d.get("layer") == "gateway"
                     and d.get("type") == "decision"}
    assert gateway_calls, "the run made gateway decisions"
    assert gateway_calls <= {m["call_id"] for m in merged}
    assert all(m["executed"] is not None for m in merged if m["call_id"] in gateway_calls)


# ---------------------------------------------------------------------------
# A32: the expiry boundary (RFC 7519 4.1.4: the current time must be before exp)
# ---------------------------------------------------------------------------

def test_a32_token_is_refused_at_exactly_its_exp_and_accepted_one_second_before():
    simclock.reset()
    token = tokens.issue_agent_token("travel-assistant", tokens.SERVER_AUDIENCE,
                                     ["travel:book"], lifetime=300)
    exp = tokens._verify_one(token, audience=tokens.SERVER_AUDIENCE)["exp"]
    simclock.set_time(exp - 1)
    assert tokens._verify_one(token, audience=tokens.SERVER_AUDIENCE)["exp"] == exp
    simclock.set_time(exp)
    with pytest.raises(jwt.ExpiredSignatureError):
        tokens._verify_one(token, audience=tokens.SERVER_AUDIENCE)
    simclock.reset()


@pytest.mark.parametrize("variant,lifetime", [("lifetime-5min", 300), ("lifetime-60min", 3600)])
def test_a32_s5_approach_4_bounds_stop_at_the_token_lifetime(variant, lifetime, tmp_path):
    f = _run(4, "S5", variant, tmp_path=tmp_path)["metrics"]["freshness"]
    assert f["last_allowed_s"] < lifetime
    assert f["first_refused_s"] == lifetime


# ---------------------------------------------------------------------------
# A33: every S5 row is reported with both bounds, in the summary file too
# ---------------------------------------------------------------------------

def test_a33_summary_file_shows_the_s5_bounds_beside_the_verdict_with_none_spelled_out(tmp_path):
    from tests.test_task7_summary import _row
    rows = []
    for approach, last, first in [(4, 240.0, 300.0), (5, None, 0.0), (6, 0.0, 1.0)]:
        r = _row(approach=approach, scenario="S5", variant="lifetime-5min",
                 verdict="Violated" if last is not None else "Refused",
                 violated=last is not None)
        r["metrics"]["freshness"] = {"window_s": last or 0.0, "last_allowed_s": last,
                                     "first_refused_s": first}
        rows.append(r)
    one = _row(approach=1, scenario="S5", variant="lifetime-5min", verdict="Violated",
               violated=True)
    one["metrics"]["freshness"] = {"window_s": None, "last_allowed_s": None,
                                   "first_refused_s": None, "rebooking": "allowed"}
    rows.append(one)
    summary.write_summary(rows, tmp_path)
    md = {ln.split("|")[1].strip(): ln for ln in (tmp_path / "summary.md").read_text().splitlines()
          if "| S5 |" in ln}
    assert len(md) == 4
    from runner import approaches
    assert "last allowed 240" in md[approaches.label(4)] and "first refused 300" in md[approaches.label(4)]
    assert "last allowed none" in md[approaches.label(5)] and "first refused 0" in md[approaches.label(5)]
    assert "last allowed 0" in md[approaches.label(6)] and "first refused 1" in md[approaches.label(6)]
    assert "single attempt: allowed" in md[approaches.label(1)]
