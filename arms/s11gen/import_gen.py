"""Import one pair of S11 generations into arms/s11gen/gen-<pair>/.

Usage: python -m arms.s11gen.import_gen <pair> [--replace]

Reads ~/s11-gen/<use_case>-<pair>/ (the workspace, with the generated
policy.cedar) and ~/s11-gen/logs/<use_case>-<pair>.{jsonl,meta.json}. For each
use case it stores, as it came: policy.cedar, the four inputs, the generation
log and a manifest.json (hashes, model, settings, time, seal check, findings).
The findings come from check.py and are never used to change the policy.
"""
import datetime
import hashlib
import json
import pathlib
import shutil
import sys

from arms.approach3.import_gen import _ABS_TOKEN_RE, _iter_tool_calls, _path_outside_kit
from arms.s11gen import check, make_workspace

HERE = pathlib.Path(__file__).parent
ROOT = pathlib.Path.home() / "s11-gen"
USE_CASES = make_workspace.USE_CASES
INPUT_NAMES = make_workspace.INPUT_NAMES
_FILE_TOOLS = frozenset({"Read", "Write", "Edit"})


def sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _events(log: pathlib.Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]


def seal_check(ws: pathlib.Path, use_case: str, log: pathlib.Path | None) -> dict:
    """The workspace held only the named inputs (and the policy), the inputs are
    the committed ones, and no logged tool call reached outside the workspace."""
    problems: dict = {}
    files = {p.relative_to(ws).as_posix() for p in ws.rglob("*") if p.is_file()}
    extra = sorted(files - set(INPUT_NAMES) - {"policy.cedar"})
    if extra:
        problems["extra_files"] = extra
    changed = [n for n, src in make_workspace.sources(use_case).items()
               if not (ws / n).exists() or sha256(ws / n) != sha256(src)]
    if changed:
        problems["changed_inputs"] = changed
    attempts, breaches = [], []
    if log is not None and log.exists():
        events = _events(log)
        for tool, inp, uid, denied in _iter_tool_calls(events):
            rec = None
            if tool == "Bash":
                cmd = inp.get("command", "")
                if "../" in cmd or any(_path_outside_kit(m.group(1), ws) and pathlib.Path(m.group(1)).exists()
                                       for m in _ABS_TOKEN_RE.finditer(cmd)):
                    rec = {"tool": tool, "tool_use_id": uid, "command": cmd}
            elif tool in _FILE_TOOLS and _path_outside_kit(inp.get("file_path", ""), ws):
                rec = {"tool": tool, "tool_use_id": uid, "path": inp.get("file_path")}
            if rec:
                (attempts if denied else breaches).append(rec)
    if breaches:
        problems["seal_breaches"] = breaches
    if problems:
        return {"status": "breached", **problems}
    if attempts:
        return {"status": "held", "seal_attempts": attempts}
    return {"status": "clean" if log is not None and log.exists() else "clean (no log supplied)"}


def _model_from_log(log: pathlib.Path | None) -> str | None:
    if log is None or not log.exists():
        return None
    for e in _events(log):
        if e.get("type") == "result" and e.get("modelUsage"):
            return next(iter(e["modelUsage"]))
    return None


def import_policy(ws: pathlib.Path, use_case: str, pair: int, dest: pathlib.Path, *,
                  log: pathlib.Path | None = None, meta: dict | None = None,
                  replace: bool = False) -> pathlib.Path:
    """Store one generated policy under dest/<use_case>/ with its manifest.
    Returns that folder."""
    ws, dest = pathlib.Path(ws), pathlib.Path(dest)
    meta = meta or {}
    out = dest / use_case
    if out.exists():
        if not replace:
            raise FileExistsError(f"{out} exists: pass replace to overwrite")
        shutil.rmtree(out)
    (out / "inputs").mkdir(parents=True)
    shutil.copyfile(ws / "policy.cedar", out / "policy.cedar")
    for name in INPUT_NAMES:
        shutil.copyfile(ws / name, out / "inputs" / name)
    manifest = {
        "use_case": use_case, "pair": pair,
        "policy_sha256": sha256(out / "policy.cedar"),
        "inputs_sha256": {n: sha256(out / "inputs" / n) for n in INPUT_NAMES},
        "model": _model_from_log(log) or meta.get("model") or "unknown",
        "pinned_model": meta.get("model"),
        "settings": meta.get("settings"),
        "time": {"start": meta.get("start_time"), "end": meta.get("end_time"),
                 "imported": datetime.datetime.now(datetime.timezone.utc).isoformat()},
        "seal_check": seal_check(ws, use_case, log),
    }
    if log is not None and log.exists():
        shutil.copyfile(log, out / "generation.jsonl")
        manifest["log_sha256"] = sha256(out / "generation.jsonl")
    manifest["findings"] = check.run(out / "policy.cedar", use_case)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return out


def import_pair(pair: int, *, replace: bool = False) -> None:
    if pair == 0:
        sys.exit("pair 0 is the pilot and is never imported")
    dest = HERE / f"gen-{pair}"
    for uc in USE_CASES:
        ws = ROOT / f"{uc}-{pair}"
        if not (ws / "policy.cedar").exists():
            sys.exit(f"no generated policy at {ws / 'policy.cedar'}")
        if (dest / uc).exists() and not replace:
            sys.exit(f"{dest / uc} exists: pass --replace to overwrite")
    for uc in USE_CASES:
        meta_path = ROOT / "logs" / f"{uc}-{pair}.meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        out = import_policy(ROOT / f"{uc}-{pair}", uc, pair, dest,
                            log=ROOT / "logs" / f"{uc}-{pair}.jsonl", meta=meta, replace=replace)
        m = json.loads((out / "manifest.json").read_text())
        f = m["findings"]
        print(f"imported {uc} pair {pair}: seal {m['seal_check'] if isinstance(m['seal_check'], str) else m['seal_check']['status']}; "
              f"parses {f['parses']}, validates {f['validates']}; self-approval {f['self_approval']['decision']}, "
              f"manager {f['manager_approval']['decision']}, isolated {f['isolated_self_approval']['decision']}")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--replace"]
    if len(args) != 1 or not args[0].isdigit():
        sys.exit("Usage: python -m arms.s11gen.import_gen <pair> [--replace]")
    import_pair(int(args[0]), replace="--replace" in sys.argv)
