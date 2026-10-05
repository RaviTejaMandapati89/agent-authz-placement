"""Generate results/approach3_conformance.json.

Probes each gen-N checkpoint by instantiating its PolicyHook and
triggering one evaluation, then reports which fields required by the
decision-log table in kit_snapshot/spec_input.md are present or missing.

Run from the repo root:
    uv run python scripts/gen_conformance.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import re
import sys
import tempfile

import yaml

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_APPROACH3 = _REPO_ROOT / "arms" / "approach3"
_SPEC_INPUT = _APPROACH3 / "kit_snapshot" / "spec_input.md"


def _parse_required_fields() -> list[str]:
    """Read required decision-log fields from the table in kit_snapshot/spec_input.md."""
    text = _SPEC_INPUT.read_text(encoding="utf-8")
    # Find the decision log section (## 8.) and its table
    section_match = re.search(r"## 8\. Decision log.*?(?=\n## |\Z)", text, re.DOTALL)
    if not section_match:
        raise ValueError("Could not find '## 8. Decision log' section in spec_input.md")
    section = section_match.group(0)
    # Extract first column of each data row (skip header and separator rows)
    fields = []
    for line in section.splitlines():
        cells = [c.strip() for c in line.split("|") if c.strip()]
        if len(cells) >= 2 and not re.match(r"^[-:]+$", cells[0]):
            name = cells[0].strip("`")
            if name and name != "Field":
                fields.append(name)
    return fields


REQUIRED_FIELDS = _parse_required_fields()


def _load_checkpoint(gen_dir: pathlib.Path):
    checkpoint_path = gen_dir / "checkpoint.py"
    spec = importlib.util.spec_from_file_location(
        f"checkpoint_{gen_dir.name}", checkpoint_path,
    )
    mod = importlib.util.module_from_spec(spec)
    repo_root_str = str(_REPO_ROOT)
    if repo_root_str not in sys.path:
        sys.path.insert(0, repo_root_str)
    spec.loader.exec_module(mod)
    return mod


def _probe_gen(gen_dir: pathlib.Path) -> dict:
    """Instantiate the gen's PolicyHook, run one event, return the log entry."""
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as tmp:
        log_path = tmp.name

    os.environ["HOOK_LOG"] = log_path
    try:
        mod = _load_checkpoint(gen_dir)

        with tempfile.TemporaryDirectory() as cfg_tmp:
            cfg_dir = pathlib.Path(cfg_tmp)
            (cfg_dir / "expense-assistant.yaml").write_text(
                yaml.dump({
                    "agent": "expense-assistant",
                    "acts_for": "alice",
                    "allowed_tools": ["read_receipt"],
                    "fingerprints": {},
                    "expense_limit": 500,
                }),
                encoding="utf-8",
            )

            hook = mod.PolicyHook(
                agent_name="expense-assistant",
                user="",
                base_url="http://localhost:9999",
                run_id="conformance-check",
                config_dir=cfg_dir,
            )

            class _FakeEvent:
                tool_use = {"name": "read_receipt", "input": {"receipt_id": "r1"}}
                selected_tool = None
                cancel_tool = None

            hook._before_tool_call(_FakeEvent())

        raw = pathlib.Path(log_path).read_text(encoding="utf-8").splitlines()
        entries = [json.loads(l) for l in raw if l.strip()]
        return entries[-1] if entries else {}
    finally:
        os.unlink(log_path)
        del os.environ["HOOK_LOG"]


def main() -> None:
    record: dict = {
        "schema": "results/approach3_conformance.json v1",
        "source": "scripts/gen_conformance.py — reads actual checkpoint output, not hand-written",
        "required_fields": REQUIRED_FIELDS,
        "note": (
            "'type' is not a required field per the decision-log table in "
            "arms/approach3/kit_snapshot/spec_input.md (section 8), which is the "
            "authoritative field list. Its absence from generated checkpoints is correct. "
            "Harness readers must not filter on type=='decision'; "
            "use presence of 'decision' and 'tool' fields instead."
        ),
        "generations": {},
    }

    gen_dirs = sorted(_APPROACH3.glob("gen-*"))
    for gen_dir in gen_dirs:
        name = gen_dir.name
        try:
            entry = _probe_gen(gen_dir)
            missing = [f for f in REQUIRED_FIELDS if f not in entry]
            extra = [f for f in entry if f not in REQUIRED_FIELDS]
            record["generations"][name] = {
                "missing_required_fields": missing,
                "extra_fields_beyond_schema": extra,
                "type_field_present": "type" in entry,
                "all_fields_emitted": list(entry.keys()),
                "status": "ok" if not missing else "non-conformant",
            }
        except Exception as exc:
            record["generations"][name] = {
                "error": str(exc),
                "status": "probe-failed",
            }

    out_path = _REPO_ROOT / "results" / "approach3_conformance.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    print(f"Written: {out_path}")


if __name__ == "__main__":
    main()
