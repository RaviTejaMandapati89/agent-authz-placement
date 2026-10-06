"""Check time for approaches 2 and 3, measured by the harness around the
unedited checkpoint call (approved change A15).

Real time is used here only as a monotonic timer for latency: never for token
issue, expiry or validation.
"""
import json
import pathlib
import time


def timed_hook(hook, log_path: pathlib.Path, run_id: str, approach: int,
               source: str = "agent"):
    """Wrap hook._before_tool_call so each call's duration lands in the hook
    log as a `timing` line. The checkpoint itself is not touched. `source` says
    who made the call: the agent, or the harness's probe after a state change."""
    inner = hook._before_tool_call

    def wrapper(event):
        t0 = time.monotonic()
        try:
            return inner(event)
        finally:
            line = {
                "type": "timing", "kind": "check", "run_id": run_id,
                "approach": approach, "source": source,
                "tool": event.tool_use.get("name"),
                "check_ms": (time.monotonic() - t0) * 1000,
            }
            if source == "probe":
                line["probe_id"] = event.tool_use.get("toolUseId")
            with pathlib.Path(log_path).open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line) + "\n")

    hook._before_tool_call = wrapper
    return hook
