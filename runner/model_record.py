"""What the harness records about the model service for one run (task 8, item 2).

RecordingModel wraps whatever model an agent is given (the real Bedrock model
or a test's scripted one) and appends one line per request to the run's model
log: the model id the request was sent with, tokens, latency and outcome. A
request sent with any model id other than the pinned one is refused before it
is sent and recorded as a mismatch. Bedrock does not return the id of the model
that answered, so the id sent is the only id there is to check (D2).

The same log is written by the harness's agent and, when MODEL_CALL_LOG is set,
by the payments-agent inside the domain server, so one file holds the run.
"""
import json
import os
import pathlib
import re
import time
from typing import Any

from runner import config as cfg

ENV_VAR = "MODEL_CALL_LOG"
TRANSCRIPT_NAME = "transcript.jsonl"
# A signed token (three dot-separated base64url parts, or a fragment of one) is
# never written to a transcript: the repo is public.
_TOKEN_RE = re.compile(r"eyJ[A-Za-z0-9_\-]*(?:\.[A-Za-z0-9_\-]*){0,2}")
_THROTTLE_CODES = ("ThrottlingException", "throttlingException", "TooManyRequestsException")


class ModelIdMismatch(RuntimeError):
    """A request was about to be sent with a model id other than the pinned one."""

    def __init__(self, sent: str, pinned: str) -> None:
        super().__init__(f"model id mismatch: request sent with {sent!r}, pinned {pinned!r}")
        self.sent, self.pinned = sent, pinned


def real_model():
    """The Bedrock model the agents use: one definition, from runner.config."""
    from strands.models.bedrock import BedrockModel
    return BedrockModel(model_id=cfg.MODEL_ID, region_name=cfg.AWS_REGION,
                        temperature=cfg.TEMPERATURE)


def _append(path: pathlib.Path, line: dict) -> None:
    with pathlib.Path(path).open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


def _append_transcript(path: pathlib.Path, line: dict) -> None:
    text = _TOKEN_RE.sub("[token removed]", json.dumps(line, default=str))
    with pathlib.Path(path).open("a", encoding="utf-8") as fh:
        fh.write(text + "\n")


def _last_message(messages) -> dict | None:
    """The newest message of the request: the user's turn, or the tool results
    the previous response asked for."""
    try:
        return messages[-1] if messages else None
    except (TypeError, KeyError, IndexError):
        return None


class _Response:
    """What one streamed response said: its text, its tool calls, why it stopped."""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.tools: list[dict] = []
        self.stop_reason: str | None = None

    def add(self, event: dict) -> None:
        start = (event.get("contentBlockStart") or {}).get("start") or {}
        if "toolUse" in start:
            self.tools.append({"name": start["toolUse"].get("name"), "_input": []})
        delta = (event.get("contentBlockDelta") or {}).get("delta") or {}
        if "text" in delta:
            self.text.append(delta["text"])
        if "toolUse" in delta and self.tools:
            self.tools[-1]["_input"].append(delta["toolUse"].get("input") or "")
        stop = (event.get("messageStop") or {}).get("stopReason")
        if stop:
            self.stop_reason = stop

    def tool_uses(self) -> list[dict]:
        out = []
        for t in self.tools:
            raw = "".join(t["_input"])
            try:
                arguments = json.loads(raw) if raw else {}
            except ValueError:
                arguments = {"unparsed": raw}
            out.append({"name": t["name"], "input": arguments})
        return out


class RecordingModel:
    """A Strands-compatible model that records each request and delegates it.

    Pass the model, or `factory` to build it on first use (a run that never
    calls the model then never builds one)."""

    def __init__(self, inner: Any = None, *, factory=None, log_path, source: str,
                 pinned: str = cfg.MODEL_ID) -> None:
        self._inner_model = inner
        self._factory = factory
        self._log = pathlib.Path(log_path)
        self._source = source
        self._pinned = pinned
        self._hooked = False
        self._transcript = self._log.with_name(TRANSCRIPT_NAME)
        self._calls = 0

    @property
    def inner(self) -> Any:
        if self._inner_model is None:
            self._inner_model = self._factory()
        if not self._hooked:
            self._hooked = True
            self._watch_attempts(self._inner_model)
        return self._inner_model

    def _watch_attempts(self, model: Any) -> None:
        """Record every failed HTTP attempt botocore makes, including the ones
        it retries inside a single request. First in line, so the retry
        handler's own answer does not stop ours being called."""
        events = getattr(getattr(getattr(model, "client", None), "meta", None), "events", None)
        if events is None:
            return

        def on_needs_retry(attempts=None, response=None, caught_exception=None, **_):
            code = None
            if caught_exception is not None:
                code = type(caught_exception).__name__
            elif response is not None:
                code = ((response[1] or {}).get("Error") or {}).get("Code")
            if code is not None:
                _append(self._log, {"type": "bedrock_attempt", "source": self._source,
                                    "pid": os.getpid(), "attempt": attempts, "code": code,
                                    "throttled": code in _THROTTLE_CODES, "ts": time.time()})

        events.register_first("needs-retry.bedrock-runtime", on_needs_retry)

    # Everything but stream is the wrapped model's (config, token counts, the
    # `stateful` flag the agents read).
    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.inner, name)

    def _sent_with(self) -> str | None:
        try:
            return (self.inner.get_config() or {}).get("model_id")
        except Exception:
            return None

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        inner = self.inner
        sent = self._sent_with()
        line = {"type": "model_call", "source": self._source, "pid": os.getpid(),
                "ts": time.time(), "model_id": sent, "pinned": self._pinned,
                "input_tokens": 0, "output_tokens": 0, "outcome": "ok"}
        if sent is not None and sent != self._pinned:
            line.update(outcome="id_mismatch", latency_ms=0)
            _append(self._log, line)
            raise ModelIdMismatch(sent, self._pinned)
        response = _Response()
        self._calls += 1
        turn = {"type": "transcript", "source": self._source, "pid": os.getpid(),
                "call": self._calls, "ts": line["ts"], "last_message": _last_message(messages)}
        if self._calls == 1:
            turn["system_prompt"] = system_prompt
        t0 = time.monotonic()
        try:
            async for event in inner.stream(messages, tool_specs, system_prompt, **kwargs):
                if isinstance(event, dict):
                    response.add(event)
                usage = (event.get("metadata") or {}).get("usage") if isinstance(event, dict) else None
                if usage:
                    line["input_tokens"] = usage.get("inputTokens") or 0
                    line["output_tokens"] = usage.get("outputTokens") or 0
                yield event
        except GeneratorExit:
            raise
        except BaseException as exc:
            throttled = type(exc).__name__ == "ModelThrottledException"
            line.update(outcome="throttled" if throttled else "error",
                        error_type=type(exc).__name__, error=str(exc)[:300])
            raise
        finally:
            line["latency_ms"] = round((time.monotonic() - t0) * 1000, 1)
            _append(self._log, line)
            turn.update(text="".join(response.text), tool_uses=response.tool_uses(),
                        stop_reason=response.stop_reason, outcome=line["outcome"])
            _append_transcript(self._transcript, turn)

    stream.__wrapped__ = True


def read_log(path: pathlib.Path) -> list[dict]:
    p = pathlib.Path(path)
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]


def usage(path: pathlib.Path) -> dict:
    """The run's model usage, from its model log. Zeros when nothing was called."""
    lines = read_log(path)
    every = [x for x in lines if x["type"] == "model_call"]
    calls = [c for c in every if c["outcome"] != "id_mismatch"]   # requests actually sent
    attempts = [x for x in lines if x["type"] == "bedrock_attempt"]
    return {
        "model_calls": len(calls),
        "input_tokens": sum(c["input_tokens"] for c in calls),
        "output_tokens": sum(c["output_tokens"] for c in calls),
        "model_ids": sorted({c["model_id"] for c in calls if c["model_id"]}),
        "throttled_calls": sum(1 for c in calls if c["outcome"] == "throttled"),
        "failed_calls": sum(1 for c in calls if c["outcome"] in ("throttled", "error")),
        "failed_attempts": len(attempts),
        "throttled_attempts": sum(1 for a in attempts if a["throttled"]),
        "id_mismatches": [{"sent": c["model_id"], "pinned": c["pinned"]}
                          for c in every if c["outcome"] == "id_mismatch"],
    }
