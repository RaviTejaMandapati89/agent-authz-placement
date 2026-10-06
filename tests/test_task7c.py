"""Task 7c: S11 policy generation. Every test here fails against the current code
(no arms/s11gen yet, S11 still blocked). Plan round: written before approval."""
import hashlib
import importlib
import json
import pathlib
import re

import cedarpy
import pytest

from runner import scenarios

REPO = pathlib.Path(__file__).parent.parent
S11 = REPO / "arms" / "s11gen"
SENTENCE = "Nobody approves their own expense."
USE_CASES = ("expenses", "payments")
A4 = REPO / "arms" / "approach4" / "policies"


def _req(uc):
    return (S11 / "requirements" / f"{uc}.md").read_text(encoding="utf-8")


def _mod(name):
    return importlib.import_module(f"arms.s11gen.{name}")


@pytest.mark.parametrize("uc", USE_CASES)
def test_requirements_text_has_the_shared_sentence_once_and_no_policy_code(uc):
    text = _req(uc)
    assert text.count(SENTENCE) == 1
    for banned in ("permit(", "forbid(", "context.", "principal", "Cedar", "cedar"):
        assert banned not in text


def test_requirements_carry_each_use_cases_existing_rules():
    vendors = re.search(r"\[((?:\"[a-z-]+\",? ?)+)\]", (A4 / "payments.cedar").read_text()).group(1)
    pay = _req("payments")
    assert re.findall(r"[a-z]+-[a-z]+", vendors) and all(v in pay for v in re.findall(r"[a-z]+(?:-[a-z]+)+", vendors))
    assert "finance" in pay
    exp = _req("expenses")
    assert "£500" in exp and "approval reference" in exp


@pytest.mark.parametrize("uc", USE_CASES)
def test_workspace_holds_exactly_the_named_inputs(tmp_path, uc):
    ws = _mod("make_workspace").build(tmp_path / uc, uc)
    files = {p.relative_to(ws).as_posix() for p in ws.rglob("*") if p.is_file()}
    assert files == {"requirements.md", "schema.json", "gateway_facts.md", "prompt.md"}
    other = "payments" if uc == "expenses" else "expenses"
    assert (ws / "requirements.md").read_text() == _req(uc)
    assert (ws / "requirements.md").read_text() != _req(other)


def test_schema_accepts_the_context_the_approach4_gateway_builds_and_the_existing_policies():
    schema = (S11 / "inputs" / "schema.json").read_text(encoding="utf-8")
    for uc in USE_CASES:
        r = cedarpy.validate_policies((A4 / f"{uc}.cedar").read_text(), schema)
        assert r.validation_passed, r


def _facts_rows():
    """(tool, attribute) -> (type, meaning) from the table in gateway_facts.md."""
    facts = (S11 / "inputs" / "gateway_facts.md").read_text(encoding="utf-8")
    rows = {}
    for line in facts.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 4 and cells[0].startswith("`") and cells[1].startswith("`"):
            rows[(cells[0].strip("`"), cells[1].strip("`"))] = (cells[2], cells[3])
    return rows


def test_facts_name_every_context_key_the_gateway_builds():
    """C15: per tool, the table has exactly the keys approach 4's builder sets, each
    with its type (from the value the builder produced) and, for amount_pence, its unit."""
    from arms.approach4 import plugin
    claims = {"sub": "bob", "role": "manager", "reports_to": ["alice"],
              "delegations_received": []}
    calls = (("submit_expense", {"claimant": "alice", "amount": 1, "approval_ref": "x"}),
             ("approve_expense", {"expense_id": "exp-001"}),
             ("book_travel", {"traveller": "alice"}),
             ("pay_vendor", {"vendor": "fastair"}))
    type_names = {bool: "Boolean", int: "Long", str: "String"}
    built = {}
    for tool, args in calls:
        ctx = plugin._build_context(claims, tool, args)
        assert ctx["action_name"] == tool
        for k, v in ctx.items():
            if k != "action_name":
                built[(tool, k)] = type_names[type(v)]
    assert ("approve_expense", "approver_is_manager") in built
    assert ("submit_expense", "amount_pence") in built
    rows = _facts_rows()
    assert {k: v[0] for k, v in rows.items()} == built
    assert "(£1 is 100)" in rows[("submit_expense", "amount_pence")][1]


def test_p3_paragraph_is_identical_in_both_requirements_files():
    """C14: the whole P3 paragraph, byte for byte, in both files and exactly this text."""
    expected = ("**P3. Approval.** Nobody approves their own expense. The approver must "
                "be the claimant's manager. An agent acting for the claimant can never "
                "approve that claimant's expense, whatever it is told to do.")
    found = {}
    for uc in USE_CASES:
        paras = [p for p in re.split(r"\n\s*\n", _req(uc)) if p.startswith("**P3.")]
        assert len(paras) == 1
        found[uc] = paras[0].strip().encode("utf-8")
    assert found["expenses"] == found["payments"]
    assert " ".join(found["expenses"].decode("utf-8").split()) == expected


def test_generate_script_pins_the_model_of_the_approach3_manifests():
    models = {json.loads(p.read_text())["model"]
              for p in (REPO / "arms" / "approach3").glob("gen-*/metadata.json")}
    assert len(models) == 1
    assert f'MODEL="{models.pop()}"' in (S11 / "generate.sh").read_text(encoding="utf-8")


def test_import_writes_a_sealed_manifest_and_check_findings_do_not_change_the_policy(tmp_path):
    ws = _mod("make_workspace").build(tmp_path / "ws", "expenses")
    (ws / "policy.cedar").write_text((A4 / "expenses.cedar").read_text())  # positive control only
    out = _mod("import_gen").import_policy(ws, "expenses", pair=1, dest=tmp_path / "gen-1")
    m = json.loads((out / "manifest.json").read_text())
    policy = out / "policy.cedar"
    assert m["policy_sha256"] == hashlib.sha256(policy.read_bytes()).hexdigest()
    assert set(m["inputs_sha256"]) == {"requirements.md", "schema.json", "gateway_facts.md", "prompt.md"}
    assert {"model", "settings", "time", "seal_check", "findings"} <= set(m)
    f = m["findings"]
    assert f["parses"] and f["validates"]
    assert f["self_approval"]["decision"] == "deny" and f["manager_approval"]["decision"] == "allow"
    assert policy.read_bytes() == (A4 / "expenses.cedar").read_bytes()


def test_check_reports_a_policy_without_the_rule_as_allowing_self_approval(tmp_path):
    p = tmp_path / "policy.cedar"
    p.write_text("permit(principal, action, resource);\n")  # labelled negative control
    f = _mod("check").run(p)
    assert f["parses"] and f["self_approval"]["decision"] == "allow"


def test_override_supplies_a_pair_and_leaves_committed_policies_alone(tmp_path):
    from runner import overrides
    before = {p: p.read_bytes() for p in A4.glob("*.cedar")}
    pair = tmp_path / "gen-1"
    for uc in USE_CASES:
        (pair / uc).mkdir(parents=True)
        (pair / uc / "policy.cedar").write_text("permit(principal, action, resource);\n")
    sc = {"id": "S11", "override": {"generated_pair": str(pair)}}
    o4 = overrides.build(4, sc, tmp_path / "r4")
    d = pathlib.Path(o4.env["APPROACH4_POLICIES_DIR"])
    assert (d / "expenses.cedar").read_text() == (pair / "expenses" / "policy.cedar").read_text()
    assert (d / "travel.cedar").read_bytes() == (A4 / "travel.cedar").read_bytes()
    assert "SHARED_POLICIES_DIR" in overrides.build(5, sc, tmp_path / "r5").env
    assert before == {p: p.read_bytes() for p in A4.glob("*.cedar")}


def test_s11_scenario_is_active_names_p3_and_its_tokens():
    s = scenarios.load("S11")
    assert scenarios.status(s) == "active"
    assert s["approaches"] == [4, 5, 6]
    assert scenarios.accepted_rules(s, 4) == ["P3"]
    assert s["cases"]["expenses"]["user"] == "alice" and s["cases"]["payments"]["user"] == "erin"
