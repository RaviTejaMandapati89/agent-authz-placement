"""One frozen simulated clock owned by the server process.

Starts at a fixed epoch; moves only through set_time/advance/reset.
Never reads real time. All authorisation expiry checks use now().
"""
import itertools
import threading

_EPOCH: float = 1_735_689_600.0  # 2025-01-01T00:00:00Z

_lock = threading.Lock()
_sim_time: float = _EPOCH
_seq = itertools.count(1)


def now() -> float:
    with _lock:
        return _sim_time


def set_time(ts: float) -> None:
    global _sim_time
    with _lock:
        _sim_time = float(ts)


def advance(seconds: float) -> None:
    global _sim_time
    with _lock:
        _sim_time += float(seconds)


def reset() -> None:
    global _sim_time
    with _lock:
        _sim_time = _EPOCH


def next_seq() -> int:
    """One per-process sequence number for every log line the server process
    writes (ledger entries, decision and outcome lines, issuer lines). Lines are
    ordered by it, never by wall-clock time. Never reset, so it stays monotonic
    across /control/reset."""
    with _lock:
        return next(_seq)
