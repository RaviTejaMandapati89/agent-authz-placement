"""One frozen simulated clock owned by the server process.

Starts at a fixed epoch; moves only through set_time/advance/reset.
Never reads real time. All authorisation expiry checks use now().
"""
import threading

_EPOCH: float = 1_735_689_600.0  # 2025-01-01T00:00:00Z

_lock = threading.Lock()
_sim_time: float = _EPOCH


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
