"""Task 7: scenario files, applicability, labels and the change-cost table.

Fails against the code before task 7: the scenario format, runner.scenarios,
runner.approaches and runner.places do not exist.
"""
import hashlib
import pathlib
import re

import pytest
import yaml

from domain.fixtures import EXPENSE_LIMIT
from runner import approaches, overrides, places, scenarios

_REPO = pathlib.Path(__file__).parent.parent
_DESIGN = (_REPO / "DESIGN.md").read_text(encoding="utf-8")


def _design_section(heading: str) -> str:
    start = _DESIGN.index(heading)
    return _DESIGN[start:_DESIGN.index("\n## ", start + 1)]


def _design_applicability() -> dict[str, list[int]]:
    out = {}
    for m in re.finditer(r"^\| (S\d+) \|.*\| ([^|]+) \|$", _design_section("## 5. Scenarios"), re.M):
        cell = m.group(2).strip()
        out[m.group(1)] = list(approaches.IDS) if cell == "All" else [
            int(x) for x in cell.split(",")]
    return out


# ---- one file per scenario, in one format -------------------------------------------

def test_one_file_for_each_of_s1_to_s16():
    assert scenarios.ids() == [f"S{i}" for i in range(1, 17)]


@pytest.mark.parametrize("sid", [f"S{i}" for i in range(1, 17)])
def test_every_scenario_file_holds_id_approaches_setup_request_and_outcome(sid):
    s = scenarios.load(sid)
    assert s["id"] == sid
    assert isinstance(s["approaches"], list) and s["approaches"]
    assert "setup" in s
    assert "expected" in s and "outcome" in s["expected"]
    if scenarios.status(s) != "blocked":
        # a request: a turn for the agent, or a scripted direct call
        assert s.get("turns") or s.get("direct")


@pytest.mark.parametrize("sid", [f"S{i}" for i in range(1, 17)])
def test_applicability_in_the_scenario_file_matches_design_section_5(sid):
    assert sorted(scenarios.load(sid)["approaches"]) == _design_applicability()[sid]


def test_there_is_one_scenario_format_no_old_arms_key():
    for sid in scenarios.ids():
        assert "arms" not in scenarios.load(sid), f"{sid} still uses the old `arms` key"


def test_expected_outcomes_are_observable_facts_never_agent_text():
    banned = {"reply", "text", "final_reply", "transcript", "message"}
    for sid in scenarios.ids():
        s = scenarios.load(sid)
        conds = []
        for k in ("violation", "legitimate", "legitimate_calls"):
            conds.extend(s["expected"].get(k) or [])
        for v in (s.get("variants") or {}).values():
            for k in ("violation", "legitimate", "legitimate_calls"):
                conds.extend((v.get("expected") or {}).get(k) or [])
        for c in conds:
            assert not (set(c) & banned), (sid, c)


# ---- variants (item 4) --------------------------------------------------------------------

def test_variants_s5_s14_s15():
    s5 = scenarios.load("S5")
    assert {k: v["token_lifetime"] for k, v in s5["variants"].items()} == {
        "lifetime-5min": 300, "lifetime-60min": 3600}
    assert set(scenarios.load("S14")["variants"]) == {"wrong-issuer", "expired"}
    assert set(scenarios.load("S15")["variants"]) == {"refusal", "control", "scope"}
    assert scenarios.variants(scenarios.load("S1")) == [None]


def test_variant_overlay_replaces_keys_and_names_the_variant():
    r = scenarios.resolve(scenarios.load("S15"), "control")
    assert r["user"] == "erin" and r["variant"] == "control"
    assert scenarios.resolve(scenarios.load("S15"), "refusal")["user"] == "alice"


# ---- S11 (item 8, A10) ------------------------------------------------------------------------

def test_s11_is_blocked_and_nothing_is_generated():
    s = scenarios.load("S11")
    assert scenarios.status(s) == "blocked"
    assert s["approaches"] == [4, 5, 6]
    assert not (_REPO / "arms" / "generated").exists()


# ---- approach labels (item 15) -------------------------------------------------------------------

def test_approach_labels_match_design_section_3_exactly():
    rows = dict(re.findall(r"^\| (\d) \| ([^|]+?) \|", _design_section("## 3. The approaches"), re.M))
    assert {int(k): v for k, v in rows.items()} == {a: approaches.label(a) for a in approaches.IDS}


def test_pilot_arms_are_not_runnable_approaches():
    for bad in (0, 7, "B", "D"):
        with pytest.raises(ValueError):
            approaches.get(bad)


# ---- S7 change cost (A6) ------------------------------------------------------------------------

def test_change_cost_table_counts_and_kinds():
    counts = {a: len(p) for a, p in places.CHANGE_PLACES.items()}
    assert counts == {1: 1, 2: 2, 3: 2, 4: 2, 5: 1, 6: 1}
    kinds = {a: {p.kind for p in ps} for a, ps in places.CHANGE_PLACES.items()}
    assert kinds == {1: {"prompt"}, 2: {"config"}, 3: {"config"}, 4: {"policy file"},
                     5: {"central state"}, 6: {"central state"}}


@pytest.mark.parametrize("approach", approaches.IDS)
def test_every_listed_place_holds_the_current_limit(approach):
    for place in places.CHANGE_PLACES[approach]:
        assert place.holds(EXPENSE_LIMIT), f"{place.path} does not hold {EXPENSE_LIMIT}"


def test_a_place_that_holds_another_limit_is_detected(tmp_path):
    root = tmp_path
    (root / "policy.md").write_text("**P2.** An expense above £900 can be submitted")
    assert places.Place("policy.md", places.PROMPT).holds(900, root)
    assert not places.Place("policy.md", places.PROMPT).holds(EXPENSE_LIMIT, root)


def _hash_tree():
    files = [_REPO / "policy.md", *(_REPO / "arms/c_hook/config").glob("*.yaml"),
             *(_REPO / "arms/approach4/policies").glob("*.cedar"),
             *(_REPO / "arms/shared/policies").glob("*.cedar")]
    return {str(f): hashlib.sha256(f.read_bytes()).hexdigest() for f in files}


@pytest.mark.parametrize("approach", [2, 3, 4])
def test_limit_overrides_are_per_run_copies_and_leave_committed_files_untouched(approach, tmp_path):
    before = _hash_tree()
    ov = overrides.build(approach, {"change": {"expense_limit": 300}}, tmp_path)
    assert _hash_tree() == before
    target = ov.config_dir if approach in (2, 3) else pathlib.Path(ov.env["APPROACH4_POLICIES_DIR"])
    assert str(target).startswith(str(tmp_path))
    for place in places.CHANGE_PLACES[approach]:
        patched = places.Place(f"{target.name}/{pathlib.Path(place.path).name}", place.kind)
        assert patched.holds(300, target.parent), place.path
    # exactly the listed places changed: the unlisted file is a byte copy
    listed = {pathlib.Path(p.path).name for p in places.CHANGE_PLACES[approach]}
    committed = {2: _REPO / "arms/c_hook/config", 3: _REPO / "arms/c_hook/config",
                 4: _REPO / "arms/approach4/policies"}[approach]
    for f in committed.iterdir():
        same = (target / f.name).read_bytes() == f.read_bytes()
        assert same == (f.name not in listed), f.name


def test_limit_override_for_central_approaches_is_a_central_state_change(tmp_path):
    for approach in (5, 6):
        assert overrides.build(approach, {"change": {"expense_limit": 300}}, tmp_path).central_limit == 300


def test_limit_override_for_approach_1_changes_only_the_prompt(tmp_path):
    ov = overrides.build(1, {"change": {"expense_limit": 300}}, tmp_path)
    assert ov.prompt_change == {"expense_limit": 300} and ov.config_dir is None and not ov.env


# ---- S12 overrides (A11) --------------------------------------------------------------------------

def _changed_lines(diff: str):
    minus = [l[1:] for l in diff.splitlines() if l.startswith("-") and not l.startswith("---")]
    plus = [l[1:] for l in diff.splitlines() if l.startswith("+") and not l.startswith("+++")]
    return minus, plus


def test_s12_override_for_approach_4_loosens_exactly_the_copied_p2_rule(tmp_path):
    ov = overrides.build(4, {"override": {"use_case_policy": "permissive"}}, tmp_path)
    (diff,) = ov.diffs.values()
    minus, plus = _changed_lines(diff)
    assert len(minus) == 1 and len(plus) == 1
    assert "context.amount_pence > 50000" in minus[0] and "5000000" in plus[0]
    assert "P2" in (_REPO / "arms/approach4/policies/expenses.cedar").read_text()


@pytest.mark.parametrize("approach", [5, 6])
def test_s12_override_for_approaches_5_and_6_adds_exactly_one_permit(approach, tmp_path):
    ov = overrides.build(approach, {"override": {"use_case_policy": "permissive"}}, tmp_path)
    (diff,) = ov.diffs.values()
    minus, plus = _changed_lines(diff)
    assert minus == []
    assert sum(1 for l in plus if l.startswith("permit(")) == 1


def test_s12_override_leaves_committed_files_untouched(tmp_path):
    before = _hash_tree()
    for approach in (4, 5, 6):
        overrides.build(approach, {"override": {"use_case_policy": "permissive"}}, tmp_path / str(approach))
    assert _hash_tree() == before


def test_s12_applies_to_4_5_6_only():
    assert scenarios.load("S12")["approaches"] == [4, 5, 6]
    with pytest.raises(ValueError):
        overrides.build(2, {"override": {"use_case_policy": "permissive"}}, pathlib.Path("."))


# ---- S13 overrides (A12) ------------------------------------------------------------------------------

def test_s13_override_for_approaches_2_and_3_adds_pay_vendor_to_a_per_run_config(tmp_path):
    before = _hash_tree()
    for approach in (2, 3):
        ov = overrides.build(approach, {"override": {"agent_spec_adds_tool": "pay_vendor"}},
                             tmp_path / str(approach))
        cfg = yaml.safe_load((ov.config_dir / "expense-assistant.yaml").read_text())
        assert "pay_vendor" in cfg["allowed_tools"]
    committed = yaml.safe_load((_REPO / "arms/c_hook/config/expense-assistant.yaml").read_text())
    assert "pay_vendor" not in committed["allowed_tools"]
    assert _hash_tree() == before


def test_s13_override_for_gateway_approaches_wires_the_tool_into_the_agent(tmp_path):
    ov = overrides.build(4, {"override": {"agent_spec_adds_tool": "pay_vendor"}}, tmp_path)
    assert ov.extra_tool_names == ["pay_vendor"] and ov.config_dir is None


def test_s13_approach_1_already_wires_every_tool_so_runs_with_no_override(tmp_path):
    ov = overrides.build(1, {"override": {"agent_spec_adds_tool": "pay_vendor"}}, tmp_path)
    assert ov.config_dir is None and not ov.extra_tool_names and not ov.env
    assert any("already wires every tool" in n for n in ov.notes)


def test_approach_1_harness_wires_every_tool_the_server_serves():
    """The claim above, checked against a real server: the tools approach 1's
    harness is given include pay_vendor."""
    import tempfile
    from strands.tools.mcp import MCPClient
    from runner._server import domain_server
    with tempfile.TemporaryDirectory() as d, \
            domain_server(str(pathlib.Path(d) / "decisions.jsonl")) as (_p, base):
        import httpx
        tok = httpx.post(f"{base}/control/identity/user-token", json={
            "sub": "alice", "aud": "expense-assistant", "scope": ["expenses:read"]}).json()["access_token"]
        atok = httpx.post(f"{base}/control/identity/agent-token", json={
            "sub": "expense-assistant", "scope": ["expenses:read"]}).json()["access_token"]
        bearer = httpx.post(f"{base}/identity/exchange", json={
            "subject_token": tok, "actor_token": atok}).json()["access_token"]
        client = MCPClient(url=f"{base}/mcp", headers={"Authorization": f"Bearer {bearer}"})
        with client:
            names = {t.tool_name for t in client.list_tools_sync()}
    assert "pay_vendor" in names
