"""Scenario files: the only place applicability, variants and status live.

Runner, grader and summaries import from here; nothing else lists which
approaches a scenario applies to.
"""
import copy
import pathlib
import re

import yaml

SCENARIOS_DIR = pathlib.Path(__file__).parent.parent / "scenarios"


def _key(sid: str) -> int:
    m = re.fullmatch(r"S(\d+)", sid)
    return int(m.group(1)) if m else 10**6


def ids() -> list[str]:
    return sorted((p.stem for p in SCENARIOS_DIR.glob("S*.yaml")), key=_key)


def load(scenario_id: str) -> dict:
    path = SCENARIOS_DIR / f"{scenario_id}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def status(scenario: dict) -> str:
    return scenario.get("status", "active")


def applies(scenario: dict, approach: int) -> bool:
    return approach in (scenario.get("approaches") or [])


def variants(scenario: dict) -> list[str | None]:
    """Variant names, or [None] for a scenario with none. Each is reported
    separately."""
    v = scenario.get("variants")
    return list(v) if v else [None]


def resolve(scenario: dict, variant: str | None) -> dict:
    """The scenario with a variant's keys laid over it."""
    out = copy.deepcopy(scenario)
    out.pop("variants", None)
    if variant is not None:
        overlay = (scenario.get("variants") or {}).get(variant)
        if overlay is None:
            raise ValueError(f"{scenario['id']} has no variant {variant!r}")
        for k, v in overlay.items():
            out[k] = copy.deepcopy(v)
        out["variant"] = variant
    return out


def steps_for(steps: list[dict] | None, approach: int) -> list[dict]:
    """Setup steps that apply to this approach (a step may name `approaches`)."""
    return [s for s in (steps or [])
            if "approaches" not in s or approach in s["approaches"]]
