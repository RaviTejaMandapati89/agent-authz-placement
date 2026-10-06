"""The concurrency probe (task 8, item 4, D3).

One fixed real-model subset of 16 runs, at 1, 2, 4 and 8 workers, recording
runs per minute, throttling events and errors at each level. It stops raising
the level at the first throttling or error (that level is recorded; none above
it runs). The subset (D8) is S1, S3, S7 and S15 (refusal variant) through
approaches 1, 2, 4 and 5: both agent-side and gateway deployments, with and
without the central service. S15 is the heaviest run (two agents, server-side
model calls), so a lighter subset would overstate throughput.

Usage (the owner runs it; it calls Bedrock):
  uv run python -m runner.probe --max-tokens N [--out DIR]
"""
import argparse
import datetime
import json
import pathlib
import sys
import time

from runner import budget as budget_mod
from runner import study

REPO = pathlib.Path(__file__).parent.parent
LEVELS = (1, 2, 4, 8)
PROBE_SUBSET = tuple((a, sid, variant)
                     for sid, variant in (("S1", None), ("S3", None), ("S7", None),
                                          ("S15", "refusal"))
                     for a in (1, 2, 4, 5))


def subset_items(subset=PROBE_SUBSET) -> list[tuple]:
    return [(a, None, sid, variant, 1) for a, sid, variant in subset]


def _level(workers: int, rows: list[dict], wall_s: float) -> dict:
    usage = [r.get("model_usage") or {} for r in rows]
    return {
        "workers": workers, "runs": len(rows), "wall_s": round(wall_s, 2),
        "runs_per_min": round(len(rows) / wall_s * 60, 2) if wall_s else 0.0,
        "throttled": sum(u.get("throttled_calls", 0) + u.get("throttled_attempts", 0)
                         for u in usage),
        "failed_attempts": sum(u.get("failed_attempts", 0) for u in usage),
        "errors": sum(1 for r in rows if r["status"] == "error"),
        "error_causes": sorted({r["error_cause"] for r in rows if r["status"] == "error"}),
        "tokens": sum(u.get("input_tokens", 0) + u.get("output_tokens", 0) for u in usage),
    }


def run_probe(out_dir, *, subset=PROBE_SUBSET, levels=LEVELS, budget=None, model=None,
              item_kwargs=None, git_sha="unknown", dirty=False, timeout_s=600.0,
              progress=print) -> dict:
    out_dir = pathlib.Path(out_dir)
    items = subset_items(subset)
    done: list[dict] = []
    stopped_at = None
    for workers in levels:
        t0 = time.monotonic()
        rows = study.run_items(items, out_dir / f"level-{workers}", workers=workers,
                               budget=budget, model=model, item_kwargs=item_kwargs,
                               git_sha=git_sha, dirty=dirty, timeout_s=timeout_s,
                               progress=progress)
        lv = _level(workers, rows, time.monotonic() - t0)
        done.append(lv)
        progress(f"level {workers}: {lv['runs']} runs, {lv['runs_per_min']} runs/min, "
                 f"throttled {lv['throttled']}, errors {lv['errors']}")
        reason = ("throttling" if lv["throttled"] else "error" if lv["errors"]
                  else "budget" if budget is not None and budget.stopped_by else None)
        if reason:
            stopped_at = {"workers": workers, "reason": reason}
            break
    clean = [lv["workers"] for lv in done if not (lv["throttled"] or lv["errors"])
             and (stopped_at is None or lv["workers"] != stopped_at["workers"])]
    res = {"subset": [list(s) for s in subset], "levels": done, "stopped_at": stopped_at,
           "safe_level": max(clean) if clean else None}
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "probe.json").write_text(json.dumps(res, indent=2))
    return res


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Concurrency probe: 16 runs at 1, 2, 4, 8 workers")
    p.add_argument("--out", default=None, help="results directory (default results/probe-<date>)")
    p.add_argument("--levels", default=",".join(map(str, LEVELS)))
    p.add_argument("--max-runs", type=int, default=None)
    p.add_argument("--max-tokens", type=int, default=None,
                   help="token budget for the whole probe (required)")
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    if args.max_tokens is None:
        print("refusing to start: this calls the real model, so --max-tokens is required (D6)",
              file=sys.stderr)
        raise SystemExit(2)
    from runner.run import _git_info
    out = pathlib.Path(args.out) if args.out else \
        REPO / "results" / f"probe-{datetime.date.today().isoformat()}"
    levels = tuple(int(x) for x in args.levels.split(","))
    git_sha, dirty = _git_info()
    print(f"probe: {len(PROBE_SUBSET)} runs per level, levels {levels}, out {out}")
    res = run_probe(out, levels=levels, git_sha=git_sha, dirty=dirty,
                    budget=budget_mod.Budget(max_runs=args.max_runs, max_tokens=args.max_tokens))
    print(json.dumps({k: res[k] for k in ("stopped_at", "safe_level")}))
    print(f"report: uv run python -m runner.smoke_report <smoke dir> --probe {out / 'probe.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
