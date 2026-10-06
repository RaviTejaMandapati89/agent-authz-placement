"""Summaries per approach and scenario (and per variant, per generation for
approach 3, then combined). Runs recorded as errors are excluded from every
metric; blocked and not-applicable scenarios are reported as such, never graded.
"""
import collections
import json
import pathlib
import statistics

from runner import grader


def _key(row: dict) -> tuple:
    return (row["approach"], row.get("generation"), row["scenario"], row.get("variant"))


def _label(row: dict) -> str:
    return row["approach_label"]


def _pooled(rows: list[dict], metric: str, field: str) -> list[float]:
    out: list[float] = []
    for r in rows:
        samples = r["metrics"]["latency"].get(field)
        if samples:
            out.extend(samples)
    return out


def _summarise_group(rows: list[dict]) -> dict:
    first = rows[0]
    base = {"approach": first["approach"], "approach_label": _label(first),
            "generation": first.get("generation"), "scenario": first["scenario"],
            "variant": first.get("variant")}
    statuses = {r["status"] for r in rows}
    if statuses == {"blocked"}:
        return {**base, "status": "blocked", "verdict": "blocked"}
    if statuses == {"n/a"}:
        return {**base, "status": "n/a", "verdict": "n/a"}

    errors = [r for r in rows if r["status"] == "error"]
    graded = [r for r in rows if r["status"] == "ok"]
    out = {**base, "status": "ok", "runs": len(rows), "errors": len(errors),
           "error_causes": sorted({r["error_cause"] for r in errors}),
           "graded": len(graded)}
    if not graded:
        return out
    verdicts = collections.Counter(r["verdict"] for r in graded)
    out["verdicts"] = dict(verdicts)
    # a refusal without a logged deny line is never counted as refused (T7-4)
    for key in ("unattributed_cause", "not_completed_cause"):
        causes = collections.Counter(r[key] for r in graded if r.get(key))
        if causes:
            out[key.replace("_cause", "_causes")] = dict(causes)
    work = [r["legitimate_work"] for r in graded if r.get("legitimate_work")]
    out["legitimate_work"] = ({"completed": sum(1 for w in work if w == "completed"),
                               "of": len(work)} if work else None)
    rules: collections.Counter = collections.Counter()
    for r in graded:
        rules.update(r.get("refusal_rules") or {})
    out["refusal_rules"] = dict(rules)
    out["rule_as_designed"] = _designed_counts(graded)
    person = [r["person_rule_held"] for r in graded if r.get("person_rule_held") is not None]
    out["person_rule_held"] = ({"held": sum(1 for h in person if h), "of": len(person)}
                               if person else None)
    out["violation_rate"] = sum(1 for r in graded if r["violated"]) / len(graded)
    legit = [r for r in graded if r["legitimate_completed"] is not None]
    if legit:
        out["legitimate_completed_rate"] = (
            sum(1 for r in legit if r["legitimate_completed"]) / len(legit))
    ms = [r["metrics"] for r in graded]
    refused = sum(m["false_refusals"]["refused"] for m in ms)
    attempted = sum(m["false_refusals"]["attempted"] for m in ms)
    out["false_refusals"] = {"refused": refused, "attempted": attempted}
    if "freshness" in ms[0]:
        windows = [m["freshness"].get("window_s") for m in ms
                   if m["freshness"].get("window_s") is not None]
        out["freshness_window_s"] = (
            {"median": statistics.median(windows), "max": max(windows)} if windows else None)
        # the window bounds are reported beside the verdict (DESIGN section 10)
        last = [m["freshness"]["last_allowed_s"] for m in ms
                if m["freshness"].get("last_allowed_s") is not None]
        first = [m["freshness"]["first_refused_s"] for m in ms
                 if m["freshness"].get("first_refused_s") is not None]
        out["freshness_unplaced_refusals"] = sum(
            m["freshness"].get("unplaced_refusals", 0) for m in ms)
        out["freshness_bounds_s"] = {"last_allowed": max(last, default=None),
                                     "first_refused": max(first, default=None)}
        rebook = collections.Counter(m["freshness"].get("rebooking") for m in ms
                                     if m["freshness"].get("rebooking"))
        if rebook:
            out["rebooking_attempt"] = dict(rebook)
    if "consistency" in ms[0]:
        cons = [m["consistency"] for m in ms if m["consistency"].get("instants")]
        out["consistency"] = {
            "agree_first": sum(1 for c in cons if c["agree_first"]) / len(cons) if cons else None,
            "window_s": ("not closed within horizon"
                         if any(not c["closed"] for c in cons)
                         else (statistics.median([c["window_s"] for c in cons]) if cons else None)),
        }
    if "change_cost" in ms[0]:
        out["change_cost"] = ms[0]["change_cost"]
    if "failure_behaviour" in ms[0]:
        out["failure_behaviour"] = dict(collections.Counter(m["failure_behaviour"] for m in ms))
    if "chains" in ms[0]:
        out["chains"] = {
            "refusing_hop": dict(collections.Counter(str(m["chains"]["refusing_hop"]) for m in ms)),
            "refusing_rule": dict(collections.Counter(str(m["chains"]["refusing_rule"]) for m in ms)),
        }
    done = sum(m["audit"]["complete"] for m in ms)
    total = sum(m["audit"]["total"] for m in ms)
    out["audit_completeness"] = (done / total) if total else None
    check = _pooled(graded, "latency", "check_ms")
    tool = _pooled(graded, "latency", "tool_ms")
    out["latency_ms"] = {
        "check": ({"median": grader.median(check), "p95": grader.percentile(check, 95)}
                  if check else "n/a"),
        "tool_call": ({"median": grader.median(tool), "p95": grader.percentile(tool, 95)}
                      if tool else None),
    }
    cc = [m["central_calls_per_decision"] for m in ms
          if m["central_calls_per_decision"] is not None]
    out["central_calls_per_decision"] = statistics.mean(cc) if cc else None
    toks = [t for t in (grader.tokens_per_completed_task(r) for r in graded) if t is not None]
    out["tokens_per_completed_task"] = statistics.mean(toks) if toks else None
    return out


def summarise(rows: list[dict]) -> list[dict]:
    groups: dict[tuple, list[dict]] = collections.defaultdict(list)
    for r in rows:
        groups[_key(r)].append(r)
    out = [_summarise_group(g) for _, g in sorted(
        groups.items(), key=lambda kv: (kv[0][0], str(kv[0][1]), _num(kv[0][2]), str(kv[0][3])))]
    # approach 3: the five generations combined
    combined: dict[tuple, list[dict]] = collections.defaultdict(list)
    for r in rows:
        if r["approach"] == 3:
            combined[(3, "all", r["scenario"], r.get("variant"))].append({**r, "generation": "all"})
    out.extend(_summarise_group(g) for g in combined.values())
    return out


def _num(sid: str) -> int:
    return int(sid[1:]) if sid[1:].isdigit() else 0


def _designed_counts(graded: list[dict]) -> dict | None:
    """Refusals judged against the accepted rules; "n/a" refusals (legitimate
    work, where every refusal is false) are counted apart, never as designed."""
    vals = [r["rule_as_designed"] for r in graded if r.get("rule_as_designed") is not None]
    judged = [v for v in vals if v != "n/a"]
    na = len(vals) - len(judged)
    if not vals:
        return None
    return {"as_designed": sum(1 for v in judged if v), "of": len(judged), "not_applicable": na}


def _num_text(v) -> str:
    return "none" if v is None else f"{v:g}"


def _window_text(s: dict) -> str:
    """The S5 bounds beside the verdict, in simulated seconds after the
    revocation; a missing bound is spelled out as "none", never left blank."""
    if s.get("rebooking_attempt"):
        return "single attempt: " + ", ".join(sorted(s["rebooking_attempt"]))
    b = s.get("freshness_bounds_s")
    if not b:
        return ""
    return f"last allowed {_num_text(b['last_allowed'])}, first refused {_num_text(b['first_refused'])}"


def _ratio(d: dict | None, key: str) -> str:
    if not d:
        return ""
    if key == "as_designed" and d.get("not_applicable"):
        return (f"{d[key]}/{d['of']}, " if d["of"] else "") + f"n/a x{d['not_applicable']}"
    return f"{d[key]}/{d['of']}"


def _rules_text(s: dict) -> str:
    return ", ".join(f"{k} x{v}" for k, v in sorted((s.get("refusal_rules") or {}).items()))


def _causes_text(s: dict) -> str:
    parts = [f"{k.replace('_causes', '')}: {c}" for k in ("unattributed_causes", "not_completed_causes")
             for c in [", ".join(f"{n} x{v}" for n, v in sorted((s.get(k) or {}).items()))] if c]
    return "; ".join(parts)


def write_summary(rows: list[dict], out_dir: pathlib.Path) -> list[dict]:
    summary = summarise(rows)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    lines = ["| approach | gen | scenario | variant | status | runs | errors | verdicts | causes "
             "| legit work | refusing rule | as designed | person rule held | S5 window |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in summary:
        lines.append(
            f"| {s['approach_label']} | {s['generation'] or ''} | {s['scenario']} | "
            f"{s['variant'] or ''} | {s['status']} | {s.get('runs', '')} | "
            f"{s.get('errors', '')} | {s.get('verdicts', s.get('verdict', ''))} | "
            f"{_causes_text(s)} | {_ratio(s.get('legitimate_work'), 'completed')} | "
            f"{_rules_text(s)} | {_ratio(s.get('rule_as_designed'), 'as_designed')} | "
            f"{_ratio(s.get('person_rule_held'), 'held')} | {_window_text(s)} |")
    (out_dir / "summary.md").write_text("\n".join(lines) + "\n")
    return summary
