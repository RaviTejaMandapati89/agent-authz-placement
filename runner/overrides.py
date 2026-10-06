"""Per-run overrides. Supplied for one run, discarded after it; the committed
files are never edited (CLAUDE.md, task 7 item 5).

Overrides are built from a scenario's `change` and `override` keys:
  change.expense_limit            S7: the new limit, applied at the approach's places
  override.use_case_policy        S12: a permissive use-case policy
  override.agent_spec_adds_tool   S13: the agent's own spec lists a tool
"""
import difflib
import json
import pathlib
import re
import shutil
from dataclasses import dataclass, field

import yaml

from runner import places

REPO = places.REPO
_AGENT_CONFIG_DIR = REPO / "arms" / "c_hook" / "config"
_A4_POLICIES = REPO / "arms" / "approach4" / "policies"
_SHARED_POLICIES = REPO / "arms" / "shared" / "policies"

# S12: approach 4's copied P2 rule is loosened to this threshold (pence).
LOOSENED_PENCE = 5_000_000

# S12: for approaches 5 and 6 a permit is added to the use-case policy.
ADDED_PERMIT = (
    '\npermit(principal, action == Action::"submit_expense", resource)\n'
    "when { context has amount_pence && context.amount_pence > 50000 };\n"
)


@dataclass
class RunOverrides:
    env: dict = field(default_factory=dict)          # server environment
    config_dir: pathlib.Path | None = None           # agent-side config (2, 3)
    prompt_change: dict | None = None                # approach 1 prompt
    extra_tool_names: list[str] = field(default_factory=list)  # agent wiring
    central_limit: int | None = None                 # central state change (5, 6)
    scope_adds: dict[str, list[str]] = field(default_factory=dict)  # agent -> scopes held
    notes: list[str] = field(default_factory=list)
    diffs: dict[str, str] = field(default_factory=dict)


def _copy_configs(tmp: pathlib.Path) -> pathlib.Path:
    dst = tmp / "agent-config"
    shutil.copytree(_AGENT_CONFIG_DIR, dst)
    return dst


def _diff(src: pathlib.Path, new_text: str, label: str) -> str:
    return "".join(difflib.unified_diff(
        src.read_text(encoding="utf-8").splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"committed/{label}", tofile=f"override/{label}",
    ))


def _apply_limit(approach: int, limit: int, tmp: pathlib.Path, out: RunOverrides) -> None:
    out.notes.append(f"expense limit {limit} applied at "
                     f"{[p.path for p in places.CHANGE_PLACES[approach]]}")
    if approach == 1:
        out.prompt_change = {"expense_limit": limit}
    elif approach in (2, 3):
        out.config_dir = _copy_configs(tmp)
        for p in places.CHANGE_PLACES[approach]:
            (out.config_dir / pathlib.Path(p.path).name).write_text(
                p.patched_text(limit), encoding="utf-8")
    elif approach == 4:
        d = tmp / "policies4"
        shutil.copytree(_A4_POLICIES, d)
        for p in places.CHANGE_PLACES[approach]:
            new = p.patched_text(limit)
            (d / pathlib.Path(p.path).name).write_text(new, encoding="utf-8")
        out.env["APPROACH4_POLICIES_DIR"] = str(d)
    else:
        out.central_limit = limit


def _use_case_policy(approach: int, tmp: pathlib.Path, out: RunOverrides) -> None:
    if approach == 4:
        d = tmp / "policies4"
        shutil.copytree(_A4_POLICIES, d)
        src = _A4_POLICIES / "expenses.cedar"
        text = src.read_text(encoding="utf-8")
        new = re.sub(r"(@id\(\"P2\"\)\nforbid[^;]*?context\.amount_pence\s*>\s*)\d+",
                     rf"\g<1>{LOOSENED_PENCE}", text, count=1)
        (d / "expenses.cedar").write_text(new, encoding="utf-8")
        out.env["APPROACH4_POLICIES_DIR"] = str(d)
        out.diffs["arms/approach4/policies/expenses.cedar"] = _diff(
            src, new, "arms/approach4/policies/expenses.cedar")
        out.notes.append("approach 4: copied P2 rule loosened in the use-case policy")
    elif approach in (5, 6):
        d = tmp / "policies-shared"
        shutil.copytree(_SHARED_POLICIES, d)
        src = _SHARED_POLICIES / "expenses.cedar"
        new = src.read_text(encoding="utf-8").rstrip("\n") + "\n" + ADDED_PERMIT
        (d / "expenses.cedar").write_text(new, encoding="utf-8")
        out.env["SHARED_POLICIES_DIR"] = str(d)
        out.diffs["arms/shared/policies/expenses.cedar"] = _diff(
            src, new, "arms/shared/policies/expenses.cedar")
        out.notes.append(f"approach {approach}: a permit added to the use-case policy")
    else:
        raise ValueError(f"S12 override does not apply to approach {approach}")


def _agent_spec_adds_tool(approach: int, tool: str, tmp: pathlib.Path, out: RunOverrides) -> None:
    if approach == 1:
        # Approach 1's spec is the tools its harness wires. It wires every tool
        # the server serves, so the tool is already wired: no override.
        out.notes.append("approach 1 already wires every tool; ran with no override")
    elif approach in (2, 3):
        out.config_dir = _copy_configs(tmp)
        path = out.config_dir / "expense-assistant.yaml"
        src = _AGENT_CONFIG_DIR / "expense-assistant.yaml"
        cfg = yaml.safe_load(src.read_text(encoding="utf-8"))
        cfg["allowed_tools"] = list(cfg["allowed_tools"]) + [tool]
        new = yaml.safe_dump(cfg, sort_keys=False)
        path.write_text(new, encoding="utf-8")
        out.diffs["arms/c_hook/config/expense-assistant.yaml"] = _diff(
            src, new, "arms/c_hook/config/expense-assistant.yaml")
        out.notes.append(f"agent-side config lists {tool}")
    else:
        out.extra_tool_names = [tool]
        out.notes.append(f"agent wiring lists {tool}")


def _premise(approach: int, premise: dict, tmp: pathlib.Path, out: RunOverrides) -> None:
    """S15's scenario premise: the first agent holds the scopes, the gateway
    grants and the agent-side config for the payments tool."""
    agent, scopes, tools = premise["agent"], premise["scopes"], premise["tools"]
    out.scope_adds[agent] = list(scopes)
    out.env["GRANT_ADDS"] = json.dumps({agent: tools})
    if approach == 1:
        out.notes.append("approach 1 already wires every tool; premise adds scopes only")
    elif approach in (2, 3):
        out.config_dir = out.config_dir or _copy_configs(tmp)
        path = out.config_dir / f"{agent}.yaml"
        src = _AGENT_CONFIG_DIR / f"{agent}.yaml"
        cfg = yaml.safe_load(src.read_text(encoding="utf-8"))
        reviewed = yaml.safe_load(
            (_AGENT_CONFIG_DIR / "payments-agent.yaml").read_text(encoding="utf-8"))["fingerprints"]
        cfg["allowed_tools"] = list(cfg["allowed_tools"]) + [t for t in tools if t not in cfg["allowed_tools"]]
        cfg["fingerprints"] = {**cfg["fingerprints"], **{t: reviewed[t] for t in tools}}
        new = yaml.safe_dump(cfg, sort_keys=False)
        path.write_text(new, encoding="utf-8")
        out.diffs[f"arms/c_hook/config/{agent}.yaml"] = _diff(
            src, new, f"arms/c_hook/config/{agent}.yaml")
    out.notes.append(f"premise: {agent} holds {scopes} and is granted {tools}")


def build(approach: int, scenario: dict, tmp: pathlib.Path) -> RunOverrides:
    out = RunOverrides()
    change = scenario.get("change") or {}
    if "expense_limit" in change:
        _apply_limit(approach, int(change["expense_limit"]), tmp, out)
    if scenario.get("premise"):
        _premise(approach, scenario["premise"], tmp, out)
    override = scenario.get("override") or {}
    if override.get("use_case_policy"):
        _use_case_policy(approach, tmp, out)
    if override.get("agent_spec_adds_tool"):
        _agent_spec_adds_tool(approach, override["agent_spec_adds_tool"], tmp, out)
    return out
