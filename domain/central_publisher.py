"""Central publisher for approach 6 (approved change B2).

Owns the publication schedule (EPOCH + 900*k boundaries), snapshot-taking,
buffering during central-source outages, and delivery to registered receivers.
Called directly from the clock control routes in domain/server.py.

Receivers register via register_snapshot_receiver(fn).  While the central
source is down, snapshots are buffered; the latest is delivered on recovery.
"""
from domain.simclock import _EPOCH

_PUBLICATION_INTERVAL: float = 900.0

_snapshot_version: int = 0
_next_pub_at: float = _EPOCH + _PUBLICATION_INTERVAL
_buffered_snapshot: dict | None = None
_snapshot_receivers: list = []


def register_snapshot_receiver(fn) -> None:
    if fn not in _snapshot_receivers:
        _snapshot_receivers.append(fn)


def deregister_snapshot_receiver(fn) -> None:
    try:
        _snapshot_receivers.remove(fn)
    except ValueError:
        pass


def _capture_snapshot() -> dict:
    global _snapshot_version
    from domain.state import state
    _snapshot_version += 1
    return {
        "expense_limit":    state.expense_limit,
        "approved_vendors": list(state.vendors),
        "version":          _snapshot_version,
    }


def process_clock_advance(now: float) -> None:
    """Called from /control/clock/advance after the simulated clock is updated."""
    global _next_pub_at, _buffered_snapshot
    if not _snapshot_receivers:
        while _next_pub_at <= now:
            _next_pub_at += _PUBLICATION_INTERVAL
        return
    from domain.state import state
    while _next_pub_at <= now:
        snap = _capture_snapshot()
        if not state.directory_down:
            for fn in _snapshot_receivers:
                fn(snap)
        else:
            _buffered_snapshot = snap  # latest wins
        _next_pub_at += _PUBLICATION_INTERVAL


def on_recovery(now: float) -> None:
    """Called from /control/directory-down when the central source comes back up."""
    global _buffered_snapshot
    if _buffered_snapshot is not None:
        for fn in _snapshot_receivers:
            fn(_buffered_snapshot)
        _buffered_snapshot = None


def take_initial_snapshot() -> dict | None:
    """Capture and deliver an immediate snapshot for the plugin's first decision.

    Returns None when the central source is currently down.
    """
    from domain.state import state
    if state.directory_down:
        return None
    snap = _capture_snapshot()
    for fn in _snapshot_receivers:
        fn(snap)
    return snap


def reset() -> None:
    """Reset publisher state; called via the plugin's reset() when the server resets."""
    global _snapshot_version, _next_pub_at, _buffered_snapshot
    from domain import simclock
    _snapshot_version = 0
    _buffered_snapshot = None
    now = simclock.now()
    elapsed = now - _EPOCH
    k = int(elapsed / _PUBLICATION_INTERVAL) + 1
    _next_pub_at = _EPOCH + k * _PUBLICATION_INTERVAL
