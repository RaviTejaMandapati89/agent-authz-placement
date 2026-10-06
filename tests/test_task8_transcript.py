"""Task 8, F1: each run keeps a transcript for diagnosis (D9).

The transcript is not a grading input. The model is the only thing swapped;
the agents, servers, gateway and run engine are production code.
"""
import json
import pathlib

from domain import tokens
from domain.server import _ScriptedModel
from runner import grader, study
from tests.study_scripts import SCRIPTS

_TIMEOUT = 120


def _lines(row) -> list[dict]:
    path = pathlib.Path(row["run_dir"]) / "transcript.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def _run(approach, sid, tmp_path, variant=None, turns=None):
    scripted, pay = SCRIPTS[(sid, variant)]
    return study.run_scenario(approach, sid, variant=variant, out_dir=tmp_path,
                              model=_ScriptedModel(scripted if turns is None else turns),
                              payments_turns=pay, timeout_s=_TIMEOUT)


def test_transcript_records_tool_call_and_result(tmp_path):
    row = _run(2, "S7", tmp_path)
    path = pathlib.Path(row["run_dir"]) / "transcript.jsonl"
    text = path.read_text(encoding="utf-8")
    assert "eyJ" not in text, "a signed token must never be written to a transcript"
    calls = [x for x in _lines(row) if x["source"] == "harness"]
    assert len(calls) == 2
    first, second = calls
    # the agent's request, and the one tool call it made
    assert [t["name"] for t in first["tool_uses"]] == ["submit_expense"]
    assert first["tool_uses"][0]["input"]["amount"] == 400.0
    assert first["stop_reason"] == "tool_use"
    # the tool's result, as the next request carried it back to the model
    results = [b["toolResult"] for b in second["last_message"]["content"] if "toolResult" in b]
    assert len(results) == 1 and results[0]["status"] == "error"
    assert second["text"] == "Done." and second["tool_uses"] == []
    # the system prompt is written once per agent, on its first call
    assert first["system_prompt"] and "system_prompt" not in second


def test_transcript_records_text_only_run(tmp_path):
    said = "I need the receipt ID before I can submit that."
    row = _run(4, "S7", tmp_path, turns=[{"text": said}])
    calls = [x for x in _lines(row) if x["source"] == "harness"]
    assert len(calls) == 1
    assert calls[0]["text"] == said
    assert calls[0]["tool_uses"] == []
    assert calls[0]["stop_reason"] == "end_turn"


def test_transcript_redacts_a_signed_token_the_model_repeats(tmp_path):
    token = tokens.Issuer("untrusted-issuer", "untrusted-key").issue(
        "alice", tokens.SERVER_AUDIENCE, ["expenses:read"], 60, now=0, kind="redaction-test")
    assert token.startswith("eyJ")
    row = _run(4, "S7", tmp_path, turns=[{"text": f"Your token is {token}"}])
    text = (pathlib.Path(row["run_dir"]) / "transcript.jsonl").read_text(encoding="utf-8")
    assert "Your token is" in text
    assert "eyJ" not in text


def test_transcript_changes_no_verdict_guard(tmp_path):
    """GUARD (named exception to rule 5, D9): passes before and after the change.
    Grading reads the ledger and the decision logs only; removing the transcript
    leaves every graded field as the run recorded it."""
    row = _run(2, "S7", tmp_path)
    assert row["verdict"] == "Refused"
    run_dir = pathlib.Path(row["run_dir"])
    (run_dir / "transcript.jsonl").unlink(missing_ok=True)
    scenario = study.scenarios.load("S7")
    logs = grader.load_logs(json.loads((run_dir / "ledger.json").read_text()),
                            run_dir / "decisions.jsonl", run_dir / "hook_decisions.jsonl",
                            run_dir / "issuer.jsonl")
    regraded = grader.grade_run(scenario, logs, 2)
    assert {k: row[k] for k in regraded} == regraded


def test_row_tokens_include_server_side_model_calls(tmp_path):
    row = _run(4, "S15", tmp_path, variant="control")
    usage = row["model_usage"]
    assert usage["model_calls"] > 2        # both agents called the model
    assert row["input_tokens"] == usage["input_tokens"]
    assert row["output_tokens"] == usage["output_tokens"]


def test_tokens_per_completed_task_counts_every_agent(tmp_path):
    row = _run(4, "S15", tmp_path, variant="control")
    usage = row["model_usage"]
    # S15 is not a completed task; treat this row as one only to read the metric.
    assert grader.tokens_per_completed_task({**row, "completed": True}) == (
        usage["input_tokens"] + usage["output_tokens"])
