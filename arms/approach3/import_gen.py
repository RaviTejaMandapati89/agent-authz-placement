#!/usr/bin/env python3
"""
Import a completed generation into arms/approach3/gen-N/.

Usage: python arms/approach3/import_gen.py <N> [--replace]

Copies from ~/checkpoint-gen/run-N/:
  - checkpoint.py      (the generated hook module)
  - metadata.json      (generation metadata)
  - .specify/          (Spec Kit artefacts)
  - specs/             (spec, plan, tasks)
  - logs/step-*.jsonl  (full step transcripts)
  - spec_input.md      (spec used during generation)

Writes arms/approach3/gen-N/ with a manifest.json of sha256 per file,
plus a seal_check result and changed_from_kit list.
If the target folder already exists, refuses unless --replace is passed.
"""

import hashlib
import json
import pathlib
import re
import shutil
import sys
import tempfile

_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent
_APPROACH3 = pathlib.Path(__file__).parent

_COPY_FILES = [
    "checkpoint.py",
    "metadata.json",
    "spec_input.md",
]

_COPY_DIRS = [
    ".specify",
    "specs",
]

_COPY_GLOBS = [
    ("logs", "step-*.jsonl"),
]

_FILE_TOOLS = frozenset({"Read", "Write", "Edit"})
_ABS_TOKEN_RE = re.compile(r"(?<!\w)(/[^\s,;\"']+)")


def _iter_tool_calls(events: list[dict]):
    denied_ids: set[str] = set()
    for event in events:
        if event.get("type") == "result":
            for denial in event.get("permission_denials", []):
                if isinstance(denial, dict):
                    uid = denial.get("tool_use_id", "")
                    if uid:
                        denied_ids.add(uid)

    for event in events:
        if event.get("type") == "assistant":
            content = event.get("message", {}).get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    uid = block.get("id", "")
                    yield block.get("name", ""), block.get("input", {}), uid, uid in denied_ids


def _path_outside_kit(path_str: str, kit_root: pathlib.Path) -> bool:
    if not path_str:
        return False
    p = pathlib.Path(path_str)
    if p.is_absolute():
        if p.parts[:2] == ("/", "dev"):
            return False
        try:
            p.resolve().relative_to(kit_root.resolve())
            return False
        except ValueError:
            return True
    if ".." in p.parts:
        try:
            (kit_root / p).resolve().relative_to(kit_root.resolve())
            return False
        except ValueError:
            return True
    return False


def seal_check(kit_root: pathlib.Path) -> dict:
    """Scan step logs for tool calls that escape the kit sandbox.

    Returns:
      {"status": "clean"}
      {"status": "held",    "seal_attempts": [...]}
      {"status": "breached","seal_breaches": [...]}
    """
    attempts: list[dict] = []
    breaches: list[dict] = []
    logs_dir = kit_root / "logs"
    if not logs_dir.exists():
        return {"status": "clean"}

    for log_file in sorted(logs_dir.glob("step-*.jsonl")):
        step = log_file.stem
        events = []
        try:
            for line in log_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        except Exception:
            continue

        for tool_name, inp, uid, is_denied in _iter_tool_calls(events):
            record: dict | None = None
            if tool_name == "Bash":
                cmd = inp.get("command", "")
                flagged = "../" in cmd
                if not flagged:
                    for m in _ABS_TOKEN_RE.finditer(cmd):
                        token = m.group(1)
                        if _path_outside_kit(token, kit_root) and pathlib.Path(token).exists():
                            flagged = True
                            break
                if flagged:
                    record = {"step": step, "tool": tool_name, "tool_use_id": uid, "command": cmd}
            elif tool_name in _FILE_TOOLS:
                fp = inp.get("file_path", "")
                if _path_outside_kit(fp, kit_root):
                    record = {"step": step, "tool": tool_name, "tool_use_id": uid, "path": fp}

            if record is not None:
                if is_denied:
                    attempts.append(record)
                else:
                    breaches.append(record)

    if not attempts and not breaches:
        return {"status": "clean"}
    if breaches:
        return {"status": "breached", "seal_breaches": breaches}
    return {"status": "held", "seal_attempts": attempts}


def sha256_file(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _kit_file_hashes() -> dict[str, str]:
    try:
        from arms.approach3.make_kit import build_kit
    except ImportError:
        sys.path.insert(0, str(_REPO_ROOT))
        from arms.approach3.make_kit import build_kit  # type: ignore[no-redef]

    with tempfile.TemporaryDirectory() as tmp:
        kit = pathlib.Path(tmp) / "kit"
        build_kit(kit)
        result: dict[str, str] = {}
        for f in sorted(kit.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
                result[str(f.relative_to(kit))] = sha256_file(f)
        return result


def _copy_path(src: pathlib.Path, dst: pathlib.Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def _copy_dir(src: pathlib.Path, dst: pathlib.Path) -> None:
    if src.exists():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def import_gen(n: int, *, replace: bool = False) -> None:
    if n == 0:
        print("N = 0 is the pilot generation and is never imported.")
        sys.exit(1)

    kit_root = pathlib.Path.home() / "checkpoint-gen" / f"run-{n}"
    gen_dst  = _APPROACH3 / f"gen-{n}"

    if not kit_root.exists():
        print(f"Kit not found: {kit_root}")
        sys.exit(1)

    if gen_dst.exists():
        if not replace:
            print(f"Destination already exists: {gen_dst}")
            print("Pass --replace to overwrite.")
            sys.exit(1)
        shutil.rmtree(gen_dst)

    gen_dst.mkdir(parents=True)

    manifest: dict = {}

    for rel in _COPY_FILES:
        src = kit_root / rel
        if not src.exists():
            print(f"WARNING: {rel} not found in kit — skipping")
            continue
        dst = gen_dst / rel
        _copy_path(src, dst)
        manifest[rel] = sha256_file(dst)

    for rel_dir in _COPY_DIRS:
        src = kit_root / rel_dir
        if not src.exists():
            continue
        dst = gen_dst / rel_dir
        _copy_dir(src, dst)
        for f in sorted(dst.rglob("*")):
            if f.is_file() and "__pycache__" not in f.parts and f.suffix != ".pyc":
                rel_file = str(f.relative_to(gen_dst))
                manifest[rel_file] = sha256_file(f)

    for rel_dir, pattern in _COPY_GLOBS:
        src_dir = kit_root / rel_dir
        if not src_dir.exists():
            continue
        for src in sorted(src_dir.glob(pattern)):
            rel_file = str(pathlib.Path(rel_dir) / src.name)
            dst = gen_dst / rel_file
            _copy_path(src, dst)
            manifest[rel_file] = sha256_file(dst)

    kit_hashes = _kit_file_hashes()
    changed_from_kit: list[str] = [
        rel for rel, digest in sorted(manifest.items())
        if isinstance(digest, str) and len(digest) == 64
        and kit_hashes.get(rel) != digest
    ]
    manifest["changed_from_kit"] = changed_from_kit

    seal_result = seal_check(kit_root)
    status = seal_result["status"]
    if status == "clean":
        manifest["seal_check"] = "clean"
    elif status == "held":
        manifest["seal_check"] = seal_result
        attempts = seal_result["seal_attempts"]
        print(f"NOTICE: seal check: {len(attempts)} attempt(s) refused by permissions:")
        for v in attempts:
            if "command" in v:
                print(f"  [{v['step']}] Bash: {v['command'][:100]!r}")
            else:
                print(f"  [{v['step']}] {v['tool']}: {v['path'][:100]!r}")
    else:
        manifest["seal_check"] = seal_result
        breaches = seal_result["seal_breaches"]
        print(f"WARNING: SEAL BREACHED — {len(breaches)} tool call(s) escaped the kit sandbox:")
        for v in breaches:
            if "command" in v:
                print(f"  [{v['step']}] Bash: {v['command'][:100]!r}")
            else:
                print(f"  [{v['step']}] {v['tool']}: {v['path'][:100]!r}")

    manifest_path = gen_dst / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")

    print(f"Imported gen-{n} to {gen_dst}")
    file_count = sum(1 for d in manifest.values() if isinstance(d, str) and len(d) == 64)
    print(f"{file_count} files in manifest")
    cfl = manifest.get("changed_from_kit", [])
    print(f"changed_from_kit ({len(cfl)} files): {cfl}")


def main() -> None:
    args_list = sys.argv[1:]
    replace = "--replace" in args_list
    args_list = [a for a in args_list if a != "--replace"]
    if len(args_list) != 1:
        print("Usage: python arms/approach3/import_gen.py <N> [--replace]")
        sys.exit(1)
    try:
        n = int(args_list[0])
    except ValueError:
        print("N must be an integer")
        sys.exit(1)
    import_gen(n, replace=replace)


if __name__ == "__main__":
    main()
