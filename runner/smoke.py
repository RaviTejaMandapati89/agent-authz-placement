"""The smoke matrix (task 8, item 1): every scenario and variant, for every
approach it applies to, once; approach 3 once per generation; S11 once per pair.

Usage (the owner runs it; it calls Bedrock):
  uv run python -m runner.smoke --max-tokens N [--workers 1] [--max-runs N] [--out DIR]
  uv run python -m runner.smoke --dry-run        # prints the plan, calls nothing

The plan is checked against a count taken straight from the scenario files; the
run refuses to start if they differ.
"""
import argparse
import datetime
import json
import pathlib
import sys

from runner import approaches as ap
from runner import budget as budget_mod
from runner import scenarios, study

REPO = pathlib.Path(__file__).parent.parent


def smoke_items() -> list[tuple]:
    """(approach, generation, scenario, variant, run number) for every run."""
    return [i for i in study.plan(ap.IDS, scenarios.ids(), 1) if study.is_run(i)]


def expected_run_count() -> int:
    """The same count, from the scenario files alone: variants x the approaches a
    scenario lists, with approach 3 once per generation."""
    total = 0
    for sid in scenarios.ids():
        raw = scenarios.load(sid)
        if scenarios.status(raw) == "blocked":
            continue
        for a in raw.get("approaches") or []:
            if a in ap.IDS:
                total += len(scenarios.variants(raw)) * (len(ap.GENERATIONS) if a == 3 else 1)
    return total


def default_out_dir(day: datetime.date) -> pathlib.Path:
    return REPO / "results" / f"smoke-{day.isoformat()}"


def parse_args(argv=None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Smoke matrix: one run of every scenario cell")
    p.add_argument("--out", default=None, help="results directory (default results/smoke-<date>)")
    p.add_argument("--workers", type=int, default=1, help="parallel runs (default 1)")
    p.add_argument("--max-runs", type=int, default=None, help="stop cleanly after this many runs")
    p.add_argument("--max-tokens", type=int, default=None,
                   help="stop cleanly once this many tokens are used (required)")
    p.add_argument("--dry-run", action="store_true", help="print the plan and stop")
    return p.parse_args(argv)


def _git_info():
    from runner.run import _git_info
    return _git_info()


def main(argv=None) -> int:
    args = parse_args(argv)
    items = smoke_items()
    print(f"planned runs: {len(items)}")
    expected = expected_run_count()
    if len(items) != expected:
        print(f"refusing to start: the plan has {len(items)} runs but the scenario files "
              f"give {expected}", file=sys.stderr)
        raise SystemExit(2)
    if not args.dry_run and args.max_tokens is None:
        print("refusing to start: this calls the real model, so --max-tokens is required (D6)",
              file=sys.stderr)
        raise SystemExit(2)
    out = pathlib.Path(args.out) if args.out else default_out_dir(datetime.date.today())
    out.mkdir(parents=True, exist_ok=True)
    (out / "plan.json").write_text(json.dumps(items))
    if args.dry_run:
        return 0
    git_sha, dirty = _git_info()
    print(f"smoke: {out}  workers: {args.workers}  commit: {git_sha}{' (dirty)' if dirty else ''}")
    budget = budget_mod.Budget(max_runs=args.max_runs, max_tokens=args.max_tokens)
    rows = study.run_items(items, out, workers=args.workers, budget=budget,
                           git_sha=git_sha, dirty=dirty)
    rec = json.loads((out / "budget.json").read_text())
    print(f"\ncompleted {rec['completed']} of {rec['planned']}; errors "
          f"{sum(1 for r in rows if r['status'] == 'error')}; tokens {rec['tokens_used']}"
          f"; stopped by {rec['stopped_by'] or 'nothing (all runs done)'}")
    print(f"report: uv run python -m runner.smoke_report {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
