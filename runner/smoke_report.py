"""The smoke report (task 8, item 7): a script, not prose.

  uv run python -m runner.smoke_report results/smoke-<date> [--probe results/probe-<date>/probe.json]
                                                          [--official-runs 10]

Per scenario, variant and approach: verdict, refusing rule, rule as designed,
legitimate work, error and cause, wall time, tokens. Then totals, how far the
budget let the batch get, and an estimate of the official run's duration and
tokens at each probed concurrency level. Writes report.md and report.json.
"""
import argparse
import collections
import json
import pathlib
import statistics
import sys

from runner import approaches as ap
from runner import config as cfg
from runner import scenarios, study

_NOTE = ("Bedrock does not return the answering model's id, so a run is checked by the id "
         "each request was sent with (D2); a request sent with any other id makes the run an error.")


def _read(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


def _row(r: dict) -> dict:
    u = r.get("model_usage") or {}
    return {
        "plan_index": r.get("plan_index"), "scenario": r["scenario"], "variant": r.get("variant"),
        "approach": r["approach"], "generation": r.get("generation"),
        "status": r["status"], "verdict": r["verdict"],
        "refusing_rule": r.get("refusing_rule"), "rule_as_designed": r.get("rule_as_designed"),
        "legitimate_work": r.get("legitimate_work"),
        "error_cause": r.get("error_cause"), "error_category": r.get("error_category"),
        "unattributed_cause": r.get("unattributed_cause"),
        "wall_s": r.get("duration_s"),
        "input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0),
        "model_calls": u.get("model_calls", 0),
        "throttled": u.get("throttled_calls", 0) + u.get("throttled_attempts", 0),
        "model_ids": u.get("model_ids", []),
    }


def _key(r: dict) -> tuple:
    return (r["approach"], r["generation"], r["scenario"], r["variant"])


def _official(rows: list[dict], official_runs: int) -> dict:
    """Runs and tokens of the official run (DESIGN: ten runs of every scenario for
    every approach, approach 3 per generation), from the smoke run's own numbers."""
    plan = [i for i in study.plan(ap.IDS, scenarios.ids(), official_runs) if study.is_run(i)]
    by_cell = collections.defaultdict(list)
    by_any_gen = collections.defaultdict(list)
    for r in rows:
        if r["status"] == "ok":
            by_cell[_key(r)].append(r)
            by_any_gen[(r["approach"], r["scenario"], r["variant"])].append(r)
    tokens = wall = 0.0
    unknown = 0
    for a, g, sid, v, _n in plan:
        rs = by_cell.get((a, g, sid, v)) or by_any_gen.get((a, sid, v))
        if not rs:
            unknown += 1
            continue
        tokens += statistics.mean(x["input_tokens"] + x["output_tokens"] for x in rs)
        wall += statistics.mean(x["wall_s"] or 0 for x in rs)
    return {"runs": len(plan), "tokens": round(tokens), "serial_wall_s": round(wall, 1),
            "runs_without_estimate": unknown}


def _estimates(official: dict, probe: dict) -> list[dict]:
    levels = probe.get("levels", [])
    base = next((lv for lv in levels if lv["workers"] == 1), None)
    out = []
    for lv in levels:
        rpm = lv["runs_per_min"]
        scaled = None
        if base and base["runs_per_min"] and rpm:
            scaled = round(official["serial_wall_s"] / 60 / (rpm / base["runs_per_min"]), 1)
        out.append({"workers": lv["workers"], "runs_per_min": rpm,
                    "minutes": round(official["runs"] / rpm, 1) if rpm else None,
                    "minutes_from_smoke_wall_times": scaled,
                    "tokens": official["tokens"],
                    "throttled": lv["throttled"], "errors": lv["errors"]})
    return out


def _cell(x) -> str:
    return "-" if x is None or x == "" else str(x).replace("|", "/")


def _markdown(rep: dict) -> str:
    t, b = rep["totals"], rep["budget"]
    L = ["# Smoke report", "",
         f"Model id pinned: `{cfg.MODEL_ID}`, region `{cfg.AWS_REGION}`. {_NOTE}", "",
         "## Totals", "",
         f"- runs: {t['runs']} ({t['errors']} errors)",
         f"- verdicts: {', '.join(f'{k} {v}' for k, v in sorted(t['verdicts'].items()))}",
         f"- wall time: {t['wall_s']:.1f} s summed over runs; model calls {t['model_calls']}",
         f"- tokens: {t['input_tokens']} in, {t['output_tokens']} out; "
         f"throttling events {t['throttled']}"]
    if b:
        L.append(f"- budget: completed {b['completed']} of {b['planned']}; stopped by "
                 f"{b['stopped_by'] or 'nothing'}; tokens used {b['tokens_used']} "
                 f"(max {b['max_tokens']}, overshoot {b['overshoot_tokens']} tokens, "
                 f"{b['overshoot_runs']} runs); max runs {b['max_runs']}")
    if rep["not_run"]:
        L.append(f"- planned but not run: {len(rep['not_run'])}: "
                 + ", ".join(f"{a}{'g' + str(g) if g else ''}/{s}{'/' + v if v else ''}"
                             for a, g, s, v, _ in rep["not_run"]))
    L += ["", "## Runs", "",
          "| scenario | variant | approach | gen | verdict | refusing rule | as designed | "
          "legitimate work | error (cause) | wall s | tokens in/out | calls |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rep["rows"]:
        err = (f"{r['error_category']}: {r['error_cause']}" if r["status"] == "error" else
               (r["unattributed_cause"] or ""))
        L.append("| " + " | ".join(_cell(x) for x in (
            r["scenario"], r["variant"], r["approach"], r["generation"], r["verdict"],
            r["refusing_rule"], r["rule_as_designed"], r["legitimate_work"], err or None,
            None if r["wall_s"] is None else f"{r['wall_s']:.1f}",
            f"{r['input_tokens']}/{r['output_tokens']}", r["model_calls"])) + " |")
    if rep["estimates"]:
        o = rep["official"]
        L += ["", "## Official run estimate", "",
              f"{o['runs']} runs (ten of every scenario for every approach); about "
              f"{o['tokens']} tokens from the smoke means "
              f"({o['runs_without_estimate']} runs have no smoke result to estimate from).", "",
              "| workers | runs/min (probe) | minutes | minutes (smoke wall times, scaled) | "
              "tokens | throttling | errors |", "|---|---|---|---|---|---|---|"]
        for e in rep["estimates"]:
            L.append(f"| {e['workers']} | {e['runs_per_min']} | {e['minutes']} | "
                     f"{_cell(e['minutes_from_smoke_wall_times'])} | {e['tokens']} | "
                     f"{e['throttled']} | {e['errors']} |")
        sa = rep["probe"].get("stopped_at")
        L += ["", f"Probe stopped at: {sa['workers']} workers ({sa['reason']})." if sa
              else "Probe reached every planned level without throttling or errors.",
              f"Highest level the probe showed safe: {rep['probe'].get('safe_level')} workers."]
    return "\n".join(L) + "\n"


def build_report(out_dir, *, probe_path=None, official_runs: int = 10) -> dict:
    out_dir = pathlib.Path(out_dir)
    rows = sorted((_row(r) for r in _read(out_dir / "results.jsonl")),
                  key=lambda r: (r["plan_index"] is None, r["plan_index"]))
    plan = json.loads((out_dir / "plan.json").read_text()) if (out_dir / "plan.json").exists() else []
    seen = {(r["approach"], r["generation"], r["scenario"], r["variant"]) for r in rows}
    not_run = [p for p in plan if (p[0], p[1], p[2], p[3]) not in seen]
    budget = json.loads((out_dir / "budget.json").read_text()) \
        if (out_dir / "budget.json").exists() else None
    totals = {"runs": len(rows), "errors": sum(1 for r in rows if r["status"] == "error"),
              "verdicts": dict(collections.Counter(r["verdict"] for r in rows)),
              "wall_s": sum(r["wall_s"] or 0 for r in rows),
              "input_tokens": sum(r["input_tokens"] for r in rows),
              "output_tokens": sum(r["output_tokens"] for r in rows),
              "model_calls": sum(r["model_calls"] for r in rows),
              "throttled": sum(r["throttled"] for r in rows)}
    rep = {"rows": rows, "totals": totals, "budget": budget, "not_run": not_run,
           "estimates": [], "probe": None, "official": None}
    if probe_path:
        probe = json.loads(pathlib.Path(probe_path).read_text())
        rep["probe"] = probe
        rep["official"] = _official([r for r in rows], official_runs)
        rep["estimates"] = _estimates(rep["official"], probe)
    rep["markdown"] = _markdown(rep)
    return rep


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Smoke report from a results directory")
    p.add_argument("out_dir")
    p.add_argument("--probe", default=None, help="probe.json from runner.probe")
    p.add_argument("--official-runs", type=int, default=10)
    args = p.parse_args(argv)
    rep = build_report(args.out_dir, probe_path=args.probe, official_runs=args.official_runs)
    out = pathlib.Path(args.out_dir)
    (out / "report.md").write_text(rep["markdown"])
    (out / "report.json").write_text(json.dumps({k: v for k, v in rep.items() if k != "markdown"},
                                                indent=2, default=str))
    print(rep["markdown"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
