"""Task 8, item 2: what the harness records per run about the model service.

The model is the only thing swapped. The agents, the servers and the run
engine are production code.
"""
import http.server
import json
import threading

import pytest

from domain.server import _ScriptedModel
from runner import config as cfg
from runner import study
from tests.study_scripts import SCRIPTS

_TIMEOUT = 120


def _model_record():
    from runner import model_record
    return model_record


class _SentWith(_ScriptedModel):
    """A scripted model that reports the model id its requests are sent with."""

    def __init__(self, turns, model_id):
        super().__init__(turns)
        self._model_id = model_id

    def get_config(self) -> dict:
        return {"model_id": self._model_id}


def _run(approach, sid, model, tmp_path, variant=None):
    turns, pay = SCRIPTS[(sid, variant)]
    return study.run_scenario(approach, sid, variant=variant, out_dir=tmp_path,
                              model=model(turns) if callable(model) else model,
                              payments_turns=pay, timeout_s=_TIMEOUT)


def test_t8_run_row_records_model_calls_tokens_and_the_id_each_request_was_sent_with(tmp_path):
    row = _run(5, "S1", lambda t: _SentWith(t, cfg.MODEL_ID), tmp_path)
    usage = row["model_usage"]
    assert usage["model_calls"] >= 2          # one tool call, then the reply
    assert usage["input_tokens"] == 5 * usage["model_calls"]
    assert usage["output_tokens"] == 5 * usage["model_calls"]
    assert usage["model_ids"] == [cfg.MODEL_ID]
    assert usage["throttled_calls"] == 0 and usage["failed_attempts"] == 0


def test_t8_every_model_call_is_a_line_in_the_runs_own_model_log(tmp_path):
    row = _run(5, "S1", lambda t: _SentWith(t, cfg.MODEL_ID), tmp_path)
    lines = [json.loads(x) for x in
             (__import__("pathlib").Path(row["run_dir"]) / "model_calls.jsonl").read_text().splitlines()]
    calls = [x for x in lines if x["type"] == "model_call"]
    assert len(calls) == row["model_usage"]["model_calls"] > 0
    assert all(c["model_id"] == cfg.MODEL_ID and c["outcome"] == "ok"
               and c["latency_ms"] >= 0 for c in calls)


def test_t8_a_model_id_other_than_the_pinned_one_makes_the_run_an_error_naming_both(tmp_path):
    other = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
    assert other != cfg.MODEL_ID
    row = _run(5, "S1", lambda t: _SentWith(t, other), tmp_path)
    assert row["status"] == "error" and row["verdict"] == "error"
    assert other in row["error_cause"] and cfg.MODEL_ID in row["error_cause"]
    assert row["error_category"] == "model id"
    assert row["model_usage"]["id_mismatches"] == [{"sent": other, "pinned": cfg.MODEL_ID}]


def test_t8_the_payments_agent_inside_the_server_is_counted_in_the_same_run_record(tmp_path):
    row = _run(4, "S15", lambda t: _SentWith(t, cfg.MODEL_ID), tmp_path, variant="control")
    lines = [json.loads(x) for x in
             (__import__("pathlib").Path(row["run_dir"]) / "model_calls.jsonl").read_text().splitlines()]
    sources = {x["source"] for x in lines if x["type"] == "model_call"}
    assert sources == {"harness", "server"}
    assert row["model_usage"]["model_calls"] == sum(1 for x in lines if x["type"] == "model_call")


def test_t8_a_run_with_no_model_call_records_zero_calls_not_a_missing_field(tmp_path):
    turns, pay = SCRIPTS[("S9", None)]
    row = study.run_scenario(4, "S9", out_dir=tmp_path, model=_ScriptedModel(turns),
                             payments_turns=pay, timeout_s=_TIMEOUT)
    assert row["model_usage"]["model_calls"] == 0
    assert row["model_usage"]["input_tokens"] == 0


# ---- throttling and retries, against the real Bedrock model class ----------

class _Throttling(http.server.BaseHTTPRequestHandler):
    hits = 0

    def do_POST(self):
        type(self).hits += 1
        self.rfile.read(int(self.headers.get("content-length", 0)))
        body = json.dumps({"message": "Too many requests"}).encode()
        self.send_response(429)
        self.send_header("x-amzn-errortype", "ThrottlingException")
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def test_t8_throttling_and_botocore_retries_are_recorded_against_the_real_model_class(tmp_path):
    import asyncio

    import boto3
    from botocore.config import Config
    from strands.models.bedrock import BedrockModel
    from strands.types.exceptions import ModelThrottledException

    _Throttling.hits = 0
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Throttling)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        inner = BedrockModel(
            model_id=cfg.MODEL_ID,
            endpoint_url=f"http://127.0.0.1:{srv.server_port}",
            boto_session=boto3.Session(aws_access_key_id="k", aws_secret_access_key="s",
                                       region_name=cfg.AWS_REGION),
            boto_client_config=Config(retries={"max_attempts": 3, "mode": "standard"}))
        mr = _model_record()
        log = tmp_path / "model_calls.jsonl"
        model = mr.RecordingModel(inner, log_path=log, source="harness")

        async def go():
            async for _ in model.stream([{"role": "user", "content": [{"text": "hi"}]}]):
                pass
        with pytest.raises(ModelThrottledException):
            asyncio.run(go())
    finally:
        srv.shutdown()

    usage = mr.usage(log)
    assert _Throttling.hits >= 2                      # botocore really retried
    assert usage["failed_attempts"] == _Throttling.hits   # every real hit is recorded
    assert usage["throttled_attempts"] == _Throttling.hits
    assert usage["throttled_calls"] == 1              # the call Strands saw failing
    assert usage["model_calls"] == 1 and usage["model_ids"] == [cfg.MODEL_ID]
