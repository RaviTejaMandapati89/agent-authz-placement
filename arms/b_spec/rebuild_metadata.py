#!/usr/bin/env python3
"""
Rebuild metadata.json for a kit from its existing step logs.

Usage:
    python arms/b_spec/rebuild_metadata.py <N>          # ~/gvg-arm-b/gen-N
    python arms/b_spec/rebuild_metadata.py --kit <path> # explicit kit path

Preserves gen, speckit_version, claude_code_version, start_time, end_time,
failed_step, and failed_exit_code from the existing metadata.json (if present).
Re-parses logs for steps, cost, and model.
"""
import json
import pathlib
import sys


def rebuild_metadata(kit_root: pathlib.Path) -> None:
    meta: dict = {}
    existing = kit_root / "metadata.json"
    if existing.exists():
        try:
            meta = json.loads(existing.read_text(encoding="utf-8"))
        except Exception:
            pass

    steps: dict = {}
    model = "unknown"

    for name in ["constitution", "specify", "plan", "tasks", "implement"]:
        p = kit_root / "logs" / f"step-{name}.jsonl"
        if not p.exists():
            continue
        events = []
        try:
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        except Exception:
            continue
        result_event = next((e for e in events if e.get("type") == "result"), None)
        if result_event is None:
            continue
        usage = result_event.get("usage", {})
        steps[name] = {
            "input_tokens":       usage.get("input_tokens", 0),
            "output_tokens":      usage.get("output_tokens", 0),
            "cost_usd":           result_event.get("total_cost_usd"),
            "permission_denials": result_event.get("permission_denials", []),
        }
        if model == "unknown":
            model_usage = result_event.get("modelUsage", {})
            if model_usage:
                model = next(iter(model_usage))

    meta["model"] = model
    meta["steps"] = steps

    (kit_root / "metadata.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    print(f"metadata.json rebuilt at {kit_root / 'metadata.json'}")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(1)

    if args[0] == "--kit":
        if len(args) < 2:
            print("--kit requires a path argument")
            sys.exit(1)
        kit_root = pathlib.Path(args[1]).expanduser().resolve()
    else:
        try:
            n = int(args[0])
        except ValueError:
            print("N must be an integer, or use --kit <path>")
            sys.exit(1)
        kit_root = pathlib.Path.home() / "gvg-arm-b" / f"gen-{n}"

    if not kit_root.exists():
        print(f"Kit not found: {kit_root}")
        sys.exit(1)

    rebuild_metadata(kit_root)


if __name__ == "__main__":
    main()
