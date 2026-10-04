"""
Tests for task 3a: approach 3 pipeline.

Items under test correspond to the numbered items in CLAUDE.md's "Current task" section.
Every test that references arms/approach3/* is expected to fail against the current code
because that directory does not yet exist.
"""
import importlib.util
import inspect
import json
import pathlib
import re
import textwrap

import pytest

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_APPROACH3 = _REPO_ROOT / "arms" / "approach3"

# ---------------------------------------------------------------------------
# Item 1: Generated checkpoint has the same interface as approach 2's PolicyHook
# ---------------------------------------------------------------------------

def test_contract_tests_file_exists():
    """arms/approach3/_contract_tests.py must exist."""
    assert (_APPROACH3 / "_contract_tests.py").exists()


def test_contract_tests_references_policyhook_class():
    """_contract_tests.py must reference the PolicyHook class."""
    text = (_APPROACH3 / "_contract_tests.py").read_text(encoding="utf-8")
    assert "PolicyHook" in text


def test_contract_tests_references_before_tool_call_event():
    """_contract_tests.py must verify BeforeToolCallEvent registration."""
    text = (_APPROACH3 / "_contract_tests.py").read_text(encoding="utf-8")
    assert "BeforeToolCallEvent" in text


def test_contract_tests_check_constructor_signature():
    """_contract_tests.py must verify the constructor accepts agent_name, user, base_url."""
    text = (_APPROACH3 / "_contract_tests.py").read_text(encoding="utf-8")
    for arg in ("agent_name", "user", "base_url"):
        assert arg in text, f"contract tests do not mention constructor arg {arg!r}"


def test_policyhook_constructor_matches_approach2_signature():
    """The PolicyHook in any gen-N checkpoint must accept the approach 2 constructor args.

    Imports from arms.approach3 — fails until the directory and at least one gen exist.
    """
    from arms.approach3 import agent as _agent_mod  # noqa: F401
    # If agent.py exists we can probe further; the import itself validates existence.


# ---------------------------------------------------------------------------
# Item 2: Fairness — kit must not contain approach 2's implementation
# ---------------------------------------------------------------------------

def test_kit_excludes_approach2_hook_implementation(tmp_path):
    """Kit must not contain hook.py or any file that names approach 2's hook."""
    from arms.approach3.make_kit import build_kit

    kit = tmp_path / "kit"
    build_kit(kit)

    for p in kit.rglob("hook.py"):
        pytest.fail(f"kit contains hook.py: {p.relative_to(kit)}")

    for p in sorted(kit.rglob("*.py")):
        text = p.read_text(encoding="utf-8")
        assert "c_hook" not in text, (
            f"{p.relative_to(kit)} references 'c_hook' (approach 2 directory)"
        )


def test_kit_excludes_gateway_and_grants(tmp_path):
    """Kit must not contain gateway.py, grants.py, or central_*.py files."""
    from arms.approach3.make_kit import build_kit

    kit = tmp_path / "kit"
    build_kit(kit)

    for rel in (
        "domain/gateway.py",
        "domain/grants.py",
        "domain/central_service.py",
        "domain/central_publisher.py",
        "domain/central_policy.cedar",
    ):
        assert not (kit / rel).exists(), f"{rel} must not appear in the kit"


def test_kit_excludes_approach456_and_boundary_code(tmp_path):
    """Kit must not contain approach 4, 5, or 6 code, or boundary-enforcement code."""
    from arms.approach3.make_kit import build_kit

    kit = tmp_path / "kit"
    build_kit(kit)

    for forbidden_dir in (
        "approach4", "approach5", "approach6",
        "d_boundary", "a_guides", "b_spec", "c_hook",
    ):
        for p in kit.rglob("*"):
            if forbidden_dir in p.parts:
                pytest.fail(f"kit contains forbidden path component {forbidden_dir!r}: {p.relative_to(kit)}")


def test_kit_excludes_approved_grants_data(tmp_path):
    """Kit must not contain domain/fingerprints.json (approved grants / reviewed fingerprints)."""
    from arms.approach3.make_kit import build_kit

    kit = tmp_path / "kit"
    build_kit(kit)

    assert not (kit / "domain" / "fingerprints.json").exists(), (
        "kit must not contain domain/fingerprints.json"
    )


# ---------------------------------------------------------------------------
# Item 3: Kit contents — ALLOW list, scopes.py, neutral config path
# ---------------------------------------------------------------------------

def test_kit_contains_scopes_py(tmp_path):
    """Kit must include domain/scopes.py."""
    from arms.approach3.make_kit import build_kit

    kit = tmp_path / "kit"
    build_kit(kit)
    assert (kit / "domain" / "scopes.py").exists(), "domain/scopes.py missing from kit"


def test_kit_agent_configs_under_neutral_path(tmp_path):
    """Agent config files must appear under a neutral path (not an approach-named path)."""
    from arms.approach3.make_kit import build_kit

    kit = tmp_path / "kit"
    build_kit(kit)

    yamls = list(kit.rglob("*.yaml"))
    assert yamls, "kit must contain agent config yaml files"

    for cfg in yamls:
        rel = str(cfg.relative_to(kit))
        for bad in ("c_hook", "approach2", "hook"):
            assert bad not in rel, (
                f"config {rel!r} uses approach-named path component {bad!r}"
            )


def test_kit_contains_all_three_agent_configs(tmp_path):
    """Kit must have a config file for each of the three agents."""
    from arms.approach3.make_kit import build_kit

    kit = tmp_path / "kit"
    build_kit(kit)

    stems = {p.stem for p in kit.rglob("*.yaml")}
    for agent in ("expense-assistant", "travel-assistant", "payments-agent"):
        assert agent in stems, f"agent config for {agent!r} missing from kit"


def test_kit_forbidden_word_check_function_exists():
    """make_kit.py must export a _check_forbidden function and a FORBIDDEN_WORDS list."""
    from arms.approach3.make_kit import _check_forbidden, FORBIDDEN_WORDS

    assert callable(_check_forbidden)
    assert isinstance(FORBIDDEN_WORDS, (list, tuple, frozenset))
    assert len(FORBIDDEN_WORDS) >= 3


def test_kit_forbidden_word_check_detects_violations(tmp_path):
    """_check_forbidden must flag files containing any forbidden word."""
    from arms.approach3.make_kit import _check_forbidden, FORBIDDEN_WORDS

    content = "\n".join(f"x = '{w}'" for w in FORBIDDEN_WORDS)
    (tmp_path / "bad.py").write_text(content, encoding="utf-8")
    hits = _check_forbidden(tmp_path)
    detected = {h.split("contains ")[-1].strip("'") for h in hits}
    assert set(FORBIDDEN_WORDS) <= detected


def test_real_kit_passes_forbidden_word_check(tmp_path):
    """A kit built from the current repo must pass the forbidden-word check."""
    from arms.approach3.make_kit import build_kit, _check_forbidden

    kit = tmp_path / "kit"
    build_kit(kit)
    hits = _check_forbidden(kit)
    assert hits == [], "kit contains forbidden words:\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# Item 4: spec_input.md — plain-terms description of the checkpoint's job
# ---------------------------------------------------------------------------

def test_spec_input_md_exists():
    """arms/approach3/spec_input.md must exist."""
    assert (_APPROACH3 / "spec_input.md").exists()


def test_spec_input_states_runtime_config_read():
    """spec_input.md must state that allowed_tools and fingerprints are read at run time."""
    text = (_APPROACH3 / "spec_input.md").read_text(encoding="utf-8")
    lower = text.lower()
    assert "run time" in lower or "runtime" in lower
    assert "config" in lower


def test_spec_input_names_all_rules():
    """spec_input.md must name each rule id P1 through P7."""
    text = (_APPROACH3 / "spec_input.md").read_text(encoding="utf-8")
    for rule in ("P1", "P2", "P3", "P4", "P5", "P6", "P7"):
        assert rule in text, f"spec_input.md missing rule {rule}"


def test_spec_input_names_directory_routes():
    """spec_input.md must describe the directory HTTP routes."""
    text = (_APPROACH3 / "spec_input.md").read_text(encoding="utf-8")
    assert "/directory/" in text


def test_spec_input_names_required_log_fields():
    """spec_input.md must name the required decision log fields."""
    text = (_APPROACH3 / "spec_input.md").read_text(encoding="utf-8")
    for field in ("call_id", "run_id", "decision", "rule", "reason", "sim_time"):
        assert field in text, f"spec_input.md does not name log field {field!r}"


def test_spec_input_requires_5xx_propagation():
    """spec_input.md must state that directory 5xx errors propagate."""
    text = (_APPROACH3 / "spec_input.md").read_text(encoding="utf-8")
    assert "5xx" in text or "503" in text or "propagate" in text.lower()


def test_spec_input_names_scopes_py():
    """spec_input.md must reference domain/scopes.py or TOOL_SCOPE_MAP."""
    text = (_APPROACH3 / "spec_input.md").read_text(encoding="utf-8")
    assert "scopes" in text.lower() or "TOOL_SCOPE_MAP" in text


def test_spec_input_states_check_order_as_numbered_items():
    """spec_input.md must have a check-order section with exactly 6 numbered items;
    each item is judged by its own opening text so that re-ordering any two steps
    is caught independently.
    Expected sequence:
      1 — blank-user guard
      2 — allowed_tools / P7
      3 — acts_for
      4 — P6
      5 — SCOPE
      6 — P1–P5
    """
    import re
    text = (_APPROACH3 / "spec_input.md").read_text(encoding="utf-8")
    lower = text.lower()

    section_start = -1
    for marker in ("check order", "check-order", "order of checks", "evaluation order"):
        idx = lower.find(marker)
        if idx != -1:
            section_start = idx
            break
    assert section_start != -1, \
        "spec_input.md must contain a dedicated check-order section"

    next_heading = lower.find("\n#", section_start + 1)
    section = (
        lower[section_start:]
        if next_heading == -1
        else lower[section_start:next_heading]
    )

    items = {}
    for m in re.finditer(r"^\s*(\d)\.\s+(.+)", section, re.MULTILINE):
        n = int(m.group(1))
        if 1 <= n <= 6 and n not in items:
            items[n] = m.group(2)

    assert set(items.keys()) == {1, 2, 3, 4, 5, 6}, (
        f"check-order section must have exactly items 1–6; "
        f"found keys {sorted(items.keys())}"
    )

    expected = {
        1: ("blank",),
        2: ("allowed_tools", "allowed tools", "p7"),
        3: ("acts_for", "acts for"),
        4: ("p6",),
        5: ("scope",),
        6: ("p1",),
    }
    for n, keywords in expected.items():
        item_text = items[n]
        assert any(kw in item_text for kw in keywords), (
            f"check-order item {n} must begin with one of {keywords!r}; "
            f"got: {item_text!r}"
        )


def test_spec_input_states_missing_and_undecodable_token_give_scope():
    """spec_input.md must explicitly cover both the missing (empty/blank) bearer-token
    case and the undecodable (invalid JWT) case, and must name SCOPE as the rule for both.
    """
    text = (_APPROACH3 / "spec_input.md").read_text(encoding="utf-8")
    lower = text.lower()

    has_missing = "missing" in lower or "empty" in lower or "blank" in lower
    assert has_missing, \
        "spec_input.md must describe the missing/empty bearer-token case"

    has_undecodable = (
        "undecodable" in lower
        or "cannot be decoded" in lower
        or "invalid jwt" in lower
        or "not a valid jwt" in lower
    )
    assert has_undecodable, \
        "spec_input.md must describe the undecodable/invalid-JWT bearer-token case"

    assert "SCOPE" in text, \
        "spec_input.md must name SCOPE as the rule applied to an invalid bearer token"


def test_spec_input_has_no_people_table_or_named_users():
    """spec_input.md must not name any fixture user as a whole word
    (alice, bob, carol, dan, erin) anywhere in the file, and must
    explicitly state that facts must never be hard-coded.
    """
    import re
    text = (_APPROACH3 / "spec_input.md").read_text(encoding="utf-8")
    lower = text.lower()

    m = re.search(r"\b(alice|bob|carol|dan|erin)\b", lower)
    assert m is None, \
        f"spec_input.md must not name fixture user {m.group()!r} (revision 6)"

    assert "hard-cod" in lower or "never be hard" in lower, \
        "spec_input.md must state that facts must never be hard-coded"


def test_spec_input_does_not_name_other_approaches_or_cedar():
    """spec_input.md must not name cedar, gateway, grants, c_hook, or any approach number."""
    text = (_APPROACH3 / "spec_input.md").read_text(encoding="utf-8")
    lower = text.lower()
    for forbidden in ("cedar", "gateway", "grants", "c_hook", "approach2", "approach 2",
                      "approach3", "approach 3", "approach4", "approach 4",
                      "approach5", "approach 5", "approach6", "approach 6"):
        assert forbidden not in lower, \
            f"spec_input.md must not name {forbidden!r}"


def test_spec_input_states_only_checkpoint_py_is_generated_source():
    """spec_input.md must state checkpoint.py is the only SOURCE FILE the generator
    produces and that Spec Kit documents are permitted alongside it.
    """
    text = (_APPROACH3 / "spec_input.md").read_text(encoding="utf-8")
    lower = text.lower()
    assert "checkpoint.py" in text, \
        "spec_input.md must name checkpoint.py as the generated file"
    assert "only" in lower, \
        "spec_input.md must state checkpoint.py is the only source file generated"
    has_kit_docs_ok = (
        "spec kit" in lower or "specification" in lower or "plan" in lower
    ) and ("permitted" in lower or "allowed" in lower or "alongside" in lower)
    assert has_kit_docs_ok, \
        "spec_input.md must state that Spec Kit documents are permitted alongside checkpoint.py"


# ---------------------------------------------------------------------------
# Item 5: Contract tests check FORMAT only — no rule-outcome encoding
# ---------------------------------------------------------------------------

def test_contract_tests_check_required_log_fields():
    """_contract_tests.py must verify all required decision log fields."""
    text = (_APPROACH3 / "_contract_tests.py").read_text(encoding="utf-8")
    for field in ("call_id", "run_id", "decision", "rule", "reason"):
        assert field in text, f"contract tests do not check log field {field!r}"


def test_contract_tests_do_not_assert_specific_deny_outcomes():
    """Contract tests must not hard-code expected allow/deny outcomes for policy rules.

    Pattern 'decision.*==.*"deny"' encodes a rule outcome and is forbidden.
    """
    import re
    text = (_APPROACH3 / "_contract_tests.py").read_text(encoding="utf-8")

    # These patterns encode specific rule outcomes
    bad_patterns = [
        r'"decision"\s*==\s*["\']deny["\']',
        r'"decision"\s*==\s*["\']allow["\']',
        r'assert\s+\w+\s*==\s*["\']deny["\']',
        r'assert\s+\w+\s*==\s*["\']allow["\']',
        r'P[1-7][^\n]*deny',
        r'P[1-7][^\n]*allow',
    ]
    for pat in bad_patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            pytest.fail(
                f"contract tests encode a policy outcome "
                f"(pattern {pat!r} matched {m.group()!r}); "
                "contract tests must check format only"
            )


# ---------------------------------------------------------------------------
# Item 6: Pipeline — make_kit, generate.sh, import_gen, seal check
# ---------------------------------------------------------------------------

def test_make_kit_py_exists():
    """arms/approach3/make_kit.py must exist."""
    assert (_APPROACH3 / "make_kit.py").exists()


def test_make_kit_has_build_kit_function():
    """make_kit.py must export a build_kit() function."""
    from arms.approach3.make_kit import build_kit
    assert callable(build_kit)


def test_generate_sh_exists():
    """arms/approach3/generate.sh must exist."""
    assert (_APPROACH3 / "generate.sh").exists()


def test_generate_sh_pins_sonnet_46():
    """generate.sh must pin the model to claude-sonnet-4-6."""
    text = (_APPROACH3 / "generate.sh").read_text(encoding="utf-8")
    assert "claude-sonnet-4-6" in text or "sonnet-4-6" in text


def test_generate_sh_pins_speckit_to_tagged_version():
    """generate.sh must install Spec Kit from a pinned tag."""
    text = (_APPROACH3 / "generate.sh").read_text(encoding="utf-8")
    assert "spec-kit" in text and "@v" in text


def test_import_gen_py_exists():
    """arms/approach3/import_gen.py must exist."""
    assert (_APPROACH3 / "import_gen.py").exists()


def test_import_gen_has_import_gen_function():
    """import_gen.py must export an import_gen() function."""
    from arms.approach3.import_gen import import_gen
    assert callable(import_gen)


def test_import_gen_has_seal_check():
    """import_gen.py must export a seal_check() function."""
    from arms.approach3.import_gen import seal_check
    assert callable(seal_check)


def test_seal_check_returns_clean_for_kit_local_calls(tmp_path):
    """seal_check returns {'status': 'clean'} when all tool calls stay inside the kit."""
    from arms.approach3.import_gen import seal_check

    kit = tmp_path / "kit"
    logs = kit / "logs"
    logs.mkdir(parents=True)
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Write",
             "input": {"file_path": "checkpoint.py"}},
        ]}},
        {"type": "result", "subtype": "success"},
    ]
    (logs / "step-implement.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    assert seal_check(kit) == {"status": "clean"}


def test_seal_check_returns_held_when_escape_refused(tmp_path):
    """seal_check returns {'status': 'held', ...} when an escape attempt was refused."""
    from arms.approach3.import_gen import seal_check

    kit = tmp_path / "kit"
    outside = tmp_path / "secret.py"
    outside.write_text("# secret\n", encoding="utf-8")

    uid = "tu_held_01"
    logs = kit / "logs"
    logs.mkdir(parents=True)
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": uid, "name": "Read",
             "input": {"file_path": str(outside)}},
        ]}},
        {"type": "result", "subtype": "success",
         "permission_denials": [{"tool_use_id": uid}]},
    ]
    (logs / "step-implement.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    result = seal_check(kit)
    assert result["status"] == "held"
    assert "seal_attempts" in result


def test_seal_check_returns_breached_when_escape_executed(tmp_path):
    """seal_check returns {'status': 'breached', ...} when an escape actually executed."""
    from arms.approach3.import_gen import seal_check

    kit = tmp_path / "kit"
    outside = tmp_path / "secret.py"
    outside.write_text("# secret\n", encoding="utf-8")

    logs = kit / "logs"
    logs.mkdir(parents=True)
    events = [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Read",
             "input": {"file_path": str(outside)}},
        ]}},
        {"type": "result", "subtype": "success"},
    ]
    (logs / "step-implement.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8"
    )
    result = seal_check(kit)
    assert result["status"] == "breached"
    assert "seal_breaches" in result


def test_import_gen_writes_manifest_with_hashes(tmp_path, monkeypatch):
    """import_gen() copies the generated checkpoint and writes manifest.json with sha256 values."""
    import arms.approach3.import_gen as _ig

    kit_root = tmp_path / "checkpoint-gen" / "run-1"
    kit_root.mkdir(parents=True)
    (kit_root / "checkpoint.py").write_text("class PolicyHook: pass\n", encoding="utf-8")
    (kit_root / "metadata.json").write_text('{"gen": 1}\n', encoding="utf-8")

    monkeypatch.setattr(_ig, "_APPROACH3", tmp_path / "arms" / "approach3")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    _ig.import_gen(1)

    gen_dst = tmp_path / "arms" / "approach3" / "gen-1"
    manifest_path = gen_dst / "manifest.json"
    assert manifest_path.exists(), "manifest.json not written by import_gen"

    import hashlib
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for rel, digest in manifest.items():
        if isinstance(digest, str) and len(digest) == 64:
            actual = hashlib.sha256((gen_dst / rel).read_bytes()).hexdigest()
            assert actual == digest, f"{rel}: manifest hash mismatch"


def test_import_gen_records_seal_result(tmp_path, monkeypatch):
    """import_gen() records the seal check result in manifest.json."""
    import arms.approach3.import_gen as _ig

    kit_root = tmp_path / "checkpoint-gen" / "run-2"
    kit_root.mkdir(parents=True)
    (kit_root / "checkpoint.py").write_text("class PolicyHook: pass\n", encoding="utf-8")
    (kit_root / "metadata.json").write_text('{"gen": 2}\n', encoding="utf-8")
    # Clean logs
    logs = kit_root / "logs"
    logs.mkdir()
    events = [{"type": "result", "subtype": "success"}]
    (logs / "step-implement.jsonl").write_text(
        json.dumps(events[0]) + "\n", encoding="utf-8"
    )

    monkeypatch.setattr(_ig, "_APPROACH3", tmp_path / "arms" / "approach3")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    _ig.import_gen(2)

    gen_dst = tmp_path / "arms" / "approach3" / "gen-2"
    manifest = json.loads((gen_dst / "manifest.json").read_text(encoding="utf-8"))
    assert "seal_check" in manifest


# ---------------------------------------------------------------------------
# Item 7: Runner — agent.py accepts bearer_token and config_dir; records gen
# ---------------------------------------------------------------------------

def test_agent_py_exists():
    """arms/approach3/agent.py must exist."""
    assert (_APPROACH3 / "agent.py").exists()


def test_agent_run_accepts_bearer_token():
    """arms/approach3/agent.py run() must accept a bearer_token parameter."""
    from arms.approach3 import agent as _agent_mod
    sig = inspect.signature(_agent_mod.run)
    assert "bearer_token" in sig.parameters, "run() must accept bearer_token"


def test_agent_run_accepts_config_dir():
    """arms/approach3/agent.py run() must accept a config_dir parameter."""
    from arms.approach3 import agent as _agent_mod
    sig = inspect.signature(_agent_mod.run)
    assert "config_dir" in sig.parameters, "run() must accept config_dir"


def test_agent_run_accepts_gen_parameter():
    """arms/approach3/agent.py run() must accept a gen parameter to select the checkpoint."""
    from arms.approach3 import agent as _agent_mod
    sig = inspect.signature(_agent_mod.run)
    assert "gen" in sig.parameters, "run() must accept gen (generation number)"


def test_runner_load_arm_supports_approach3():
    """runner.run._load_arm must support the '3' arm identifier for approach 3."""
    from runner.run import _load_arm
    try:
        mod = _load_arm("3")
        assert mod is not None
    except ValueError:
        pytest.fail("runner._load_arm does not support arm '3' (approach 3)")


# ---------------------------------------------------------------------------
# Acceptance: kit exclusions, inclusions, forbidden words, runtime config read
# ---------------------------------------------------------------------------

def test_acceptance_kit_excludes_hook_gateway_grants_central_456(tmp_path):
    """Acceptance: kit excludes hook.py, gateway, grants, central, and approach 4-6 files."""
    from arms.approach3.make_kit import build_kit

    kit = tmp_path / "kit"
    build_kit(kit)

    must_not_exist = [
        "domain/gateway.py",
        "domain/grants.py",
        "domain/central_service.py",
        "domain/central_publisher.py",
        "domain/central_policy.cedar",
        "domain/fingerprints.json",
    ]
    for rel in must_not_exist:
        assert not (kit / rel).exists(), f"kit must not contain {rel}"

    for p in kit.rglob("hook.py"):
        pytest.fail(f"kit contains hook.py at {p.relative_to(kit)}")

    forbidden_dirs = {"approach4", "approach5", "approach6", "d_boundary", "c_hook"}
    for p in kit.rglob("*"):
        for d in forbidden_dirs:
            if d in p.parts:
                pytest.fail(f"kit contains forbidden directory {d!r}: {p.relative_to(kit)}")


def test_acceptance_kit_includes_scopes_and_neutral_configs(tmp_path):
    """Acceptance: kit includes domain/scopes.py and agent configs under a neutral path."""
    from arms.approach3.make_kit import build_kit

    kit = tmp_path / "kit"
    build_kit(kit)

    assert (kit / "domain" / "scopes.py").exists(), "domain/scopes.py missing from kit"

    yamls = list(kit.rglob("*.yaml"))
    assert yamls, "no agent config yaml files in kit"
    for cfg in yamls:
        rel = str(cfg.relative_to(kit))
        assert "c_hook" not in rel, f"config path {rel!r} contains 'c_hook'"
        assert "hook" not in rel, f"config path {rel!r} contains 'hook'"


def test_acceptance_no_forbidden_words_in_kit(tmp_path):
    """Acceptance: no file in the kit contains a forbidden word."""
    from arms.approach3.make_kit import build_kit, _check_forbidden

    kit = tmp_path / "kit"
    build_kit(kit)
    hits = _check_forbidden(kit)
    assert not hits, "kit contains forbidden words:\n" + "\n".join(hits)


# ---------------------------------------------------------------------------
# K1: full forbidden-word list, scopes.py clean, approach+digit pattern
# ---------------------------------------------------------------------------

def test_k1_forbidden_words_covers_full_list():
    """FORBIDDEN_WORDS must include every word in the K1 list:
    cedar, gateway, grants, central_service, central_publisher, c_hook.
    """
    from arms.approach3.make_kit import FORBIDDEN_WORDS

    required = {"cedar", "gateway", "grants", "central_service", "central_publisher", "c_hook"}
    missing = required - set(FORBIDDEN_WORDS)
    assert not missing, (
        f"FORBIDDEN_WORDS is missing K1-required entries: {sorted(missing)}"
    )


def test_k1_forbidden_check_rejects_planted_gateway(tmp_path):
    """_check_forbidden must flag a file that contains the word 'gateway'."""
    from arms.approach3.make_kit import _check_forbidden

    (tmp_path / "planted.py").write_text(
        "# this file mentions gateway\n", encoding="utf-8"
    )
    hits = _check_forbidden(tmp_path)
    assert hits, "_check_forbidden did not detect 'gateway' in a planted file"
    assert any("gateway" in h for h in hits), (
        f"hits did not mention 'gateway': {hits}"
    )


def test_k1_forbidden_check_rejects_planted_approach_digit(tmp_path):
    """_check_forbidden must flag 'approach 2' and 'approach_3' (approach+digit patterns)."""
    from arms.approach3.make_kit import _check_forbidden

    (tmp_path / "planted.py").write_text(
        "# see approach 2 for comparison\n# also approach_3 variant\n",
        encoding="utf-8",
    )
    hits = _check_forbidden(tmp_path)
    assert hits, (
        "_check_forbidden did not detect 'approach 2' or 'approach_3' patterns"
    )


def test_k1_scopes_py_has_no_forbidden_word():
    """domain/scopes.py in the repo must not contain any K1 forbidden word or approach+digit."""
    import re
    scopes = (
        pathlib.Path(__file__).parent.parent / "domain" / "scopes.py"
    ).read_text(encoding="utf-8")

    simple_words = ["cedar", "gateway", "grants", "central_service",
                    "central_publisher", "c_hook"]
    for word in simple_words:
        assert word not in scopes, (
            f"domain/scopes.py contains forbidden word {word!r}"
        )

    assert not re.search(r"approach[\s_]?\d", scopes, re.IGNORECASE), (
        "domain/scopes.py contains an 'approach <digit>' reference"
    )


# ---------------------------------------------------------------------------
# K2: kit must include policy.md
# ---------------------------------------------------------------------------

def test_k2_kit_contains_policy_md(tmp_path):
    """K2: the kit assembled by build_kit must include policy.md."""
    from arms.approach3.make_kit import build_kit

    kit = tmp_path / "kit"
    build_kit(kit)
    assert (kit / "policy.md").exists(), "kit is missing policy.md (K2)"


# ---------------------------------------------------------------------------
# K3: kit must contain all eight domain files
# ---------------------------------------------------------------------------

def test_k3_kit_contains_eight_domain_files(tmp_path):
    """K3: kit domain/ must contain the agreed eight files:
    __init__, fixtures, identity, server, simclock, state, tokens, scopes.
    """
    from arms.approach3.make_kit import build_kit

    kit = tmp_path / "kit"
    build_kit(kit)

    required = {
        "__init__.py",
        "fixtures.py",
        "identity.py",
        "server.py",
        "simclock.py",
        "state.py",
        "tokens.py",
        "scopes.py",
    }
    present = {p.name for p in (kit / "domain").iterdir() if p.suffix == ".py"}
    missing = required - present
    assert not missing, (
        f"kit domain/ is missing K3-required files: {sorted(missing)}"
    )


def test_acceptance_stub_checkpoint_hardcoding_fails_runtime_check(tmp_path):
    """Acceptance: a checkpoint that ignores config_dir fails the runtime config-read check.

    The runtime-read requirement: instantiating PolicyHook with two different config_dirs
    (each declaring different allowed_tools) must produce instances whose effective tool
    lists differ. A stub that hard-codes its tool list will produce identical instances
    regardless of config_dir — this assertion catches that.

    This test imports from arms.approach3 to obtain the check helper function; it fails
    against the current code because that module does not yet exist.
    """
    import yaml

    # Two config dirs with mutually exclusive tool sets
    cfg_a = tmp_path / "cfg_a"
    cfg_a.mkdir()
    (cfg_a / "expense-assistant.yaml").write_text(
        "agent: expense-assistant\nacts_for: any\n"
        "allowed_tools:\n- read_receipt\n"
        "fingerprints:\n  read_receipt: aabb\n",
        encoding="utf-8",
    )
    cfg_b = tmp_path / "cfg_b"
    cfg_b.mkdir()
    (cfg_b / "expense-assistant.yaml").write_text(
        "agent: expense-assistant\nacts_for: any\n"
        "allowed_tools:\n- submit_expense\n"
        "fingerprints:\n  submit_expense: ccdd\n",
        encoding="utf-8",
    )

    # Stub that hard-codes tools and ignores config_dir
    stub_src = textwrap.dedent("""\
        class PolicyHook:
            def __init__(self, agent_name, user, base_url, run_id=None,
                         limit_override=None, config_dir=None, bearer_token=""):
                # Hard-codes the tool list instead of reading from config_dir
                self._allowed_tools = ["read_receipt", "submit_expense"]
    """)
    stub_file = tmp_path / "stub_checkpoint.py"
    stub_file.write_text(stub_src, encoding="utf-8")
    spec_ = importlib.util.spec_from_file_location("stub_checkpoint", stub_file)
    stub_mod = importlib.util.module_from_spec(spec_)
    spec_.loader.exec_module(stub_mod)

    # The helper that verifies runtime reading lives in arms.approach3.make_kit
    from arms.approach3.make_kit import verify_reads_config_at_runtime  # fails until built

    result = verify_reads_config_at_runtime(
        stub_mod.PolicyHook, "expense-assistant", cfg_a, cfg_b
    )
    assert result is False, (
        "verify_reads_config_at_runtime must return False for a stub that hard-codes tools"
    )


# ---------------------------------------------------------------------------
# G1 – G4: approved round-2 changes (neutral naming, specific allowedTools)
# ---------------------------------------------------------------------------

_GENERATE_SH  = _APPROACH3 / "generate.sh"
_MAKE_KIT_SRC = _APPROACH3 / "make_kit.py"
_IMPORT_GEN_SRC = _APPROACH3 / "import_gen.py"


# G1: Write and Edit must be allowed for ./specs/** and ./.specify/memory/**

def test_g1_generate_sh_allows_writes_to_specs_dir():
    """G1: generate.sh allowed-tools must permit Write inside ./specs/."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert re.search(r"Write\(\.\/specs\/", text), (
        "generate.sh must include Write(./specs/**) in its allowedTools (G1)"
    )


def test_g1_generate_sh_allows_edits_to_specs_dir():
    """G1: generate.sh allowed-tools must permit Edit inside ./specs/."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert re.search(r"Edit\(\.\/specs\/", text), (
        "generate.sh must include Edit(./specs/**) in its allowedTools (G1)"
    )


def test_g1_generate_sh_allows_writes_to_specify_memory_dir():
    """G1: generate.sh allowed-tools must permit Write inside ./.specify/memory/."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert re.search(r"Write\(\.\/\.specify\/memory\/", text), (
        "generate.sh must include Write(./.specify/memory/**) in its allowedTools (G1)"
    )


def test_g1_generate_sh_allows_edits_to_specify_memory_dir():
    """G1: generate.sh allowed-tools must permit Edit inside ./.specify/memory/."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert re.search(r"Edit\(\.\/\.specify\/memory\/", text), (
        "generate.sh must include Edit(./.specify/memory/**) in its allowedTools (G1)"
    )


# G2: Replace the broad Bash(uv run *) with the specific Bash(uv run pytest*)

def test_g2_generate_sh_has_no_broad_uv_run_wildcard():
    """G2: generate.sh must not contain the broad Bash(uv run *)."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert "Bash(uv run *)" not in text, (
        "generate.sh must replace Bash(uv run *) with Bash(uv run pytest*) (G2)"
    )


def test_g2_generate_sh_has_specific_uv_run_pytest():
    """G2: generate.sh must contain Bash(uv run pytest*) (or similar) in its allowed-tools."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert "Bash(uv run pytest" in text, (
        "generate.sh must include Bash(uv run pytest*) in its allowedTools (G2)"
    )


# G3: Remove the broad Bash(git *) wildcard

def test_g3_generate_sh_has_no_broad_git_wildcard():
    """G3: generate.sh must not contain the broad Bash(git *) wildcard."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert "Bash(git *)" not in text, (
        "generate.sh must not use Bash(git *); allow only specific git commands (G3)"
    )


# G4: Neutral kit folder path; neutral pyproject name; forbidden-word check covers gvg/arm3

def test_g4_generate_sh_kit_folder_has_no_gvg():
    """G4: generate.sh KIT= path must not contain 'gvg'."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("KIT="):
            assert "gvg" not in line.lower(), (
                f"generate.sh KIT= path must not contain 'gvg' (G4): {line.strip()!r}"
            )
            return
    pytest.fail("generate.sh has no KIT= line")


def test_g4_generate_sh_kit_folder_has_no_arm3():
    """G4: generate.sh KIT= path must not contain 'arm3'."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.strip().startswith("KIT="):
            assert "arm3" not in line.lower(), (
                f"generate.sh KIT= path must not contain 'arm3' (G4): {line.strip()!r}"
            )
            return
    pytest.fail("generate.sh has no KIT= line")


def test_g4_make_kit_build_path_has_no_gvg_arm3():
    """G4: make_kit.py must not contain 'gvg-arm3' as a kit folder component."""
    text = _MAKE_KIT_SRC.read_text(encoding="utf-8")
    assert "gvg-arm3" not in text, (
        "make_kit.py must not use 'gvg-arm3' as the kit folder name (G4)"
    )


def test_g4_import_gen_path_has_no_gvg_arm3():
    """G4: import_gen.py must not contain 'gvg-arm3' as a kit folder component."""
    text = _IMPORT_GEN_SRC.read_text(encoding="utf-8")
    assert "gvg-arm3" not in text, (
        "import_gen.py must not use 'gvg-arm3' as the kit folder name (G4)"
    )


def test_g4_kit_pyproject_name_is_neutral():
    """G4: kit pyproject.toml template must not contain 'gvg' or 'arm3'."""
    from arms.approach3.make_kit import _PYPROJECT

    assert "gvg" not in _PYPROJECT.lower(), (
        "kit pyproject.toml template must not contain 'gvg' (G4)"
    )
    assert "arm3" not in _PYPROJECT.lower(), (
        "kit pyproject.toml template must not contain 'arm3' (G4)"
    )


def test_g4_forbidden_words_includes_gvg():
    """G4: FORBIDDEN_WORDS must include 'gvg'."""
    from arms.approach3.make_kit import FORBIDDEN_WORDS

    assert "gvg" in FORBIDDEN_WORDS, "FORBIDDEN_WORDS must include 'gvg' (G4)"


def test_g4_forbidden_words_includes_arm3():
    """G4: FORBIDDEN_WORDS must include 'arm3'."""
    from arms.approach3.make_kit import FORBIDDEN_WORDS

    assert "arm3" in FORBIDDEN_WORDS, "FORBIDDEN_WORDS must include 'arm3' (G4)"


def test_g4_forbidden_words_includes_study_project_name():
    """G4: FORBIDDEN_WORDS must include the study project name 'guides-vs-gates'."""
    from arms.approach3.make_kit import FORBIDDEN_WORDS

    assert "guides-vs-gates" in FORBIDDEN_WORDS, (
        "FORBIDDEN_WORDS must include 'guides-vs-gates' (the study project name) (G4)"
    )


def test_g4_forbidden_check_rejects_planted_gvg(tmp_path):
    """G4: _check_forbidden must flag a file containing 'gvg'."""
    from arms.approach3.make_kit import _check_forbidden

    (tmp_path / "planted.py").write_text("# this file mentions gvg\n", encoding="utf-8")
    hits = _check_forbidden(tmp_path)
    assert hits, "_check_forbidden did not detect 'gvg' in a planted file (G4)"
    assert any("gvg" in h for h in hits), f"hits did not mention 'gvg': {hits}"


def test_g4_forbidden_check_rejects_planted_arm3(tmp_path):
    """G4: _check_forbidden must flag a file containing 'arm3'."""
    from arms.approach3.make_kit import _check_forbidden

    (tmp_path / "planted.py").write_text("# this file mentions arm3\n", encoding="utf-8")
    hits = _check_forbidden(tmp_path)
    assert hits, "_check_forbidden did not detect 'arm3' in a planted file (G4)"
    assert any("arm3" in h for h in hits), f"hits did not mention 'arm3': {hits}"


def test_g4_forbidden_words_includes_repo_name():
    """G4: FORBIDDEN_WORDS must include this repository's own name 'agent-authz-placement'."""
    from arms.approach3.make_kit import FORBIDDEN_WORDS

    assert "agent-authz-placement" in FORBIDDEN_WORDS, (
        "FORBIDDEN_WORDS must include 'agent-authz-placement' (G4)"
    )


def test_g4_forbidden_check_rejects_planted_repo_name(tmp_path):
    """G4: _check_forbidden must flag a file containing 'agent-authz-placement'."""
    from arms.approach3.make_kit import _check_forbidden

    (tmp_path / "planted.py").write_text(
        "# this file mentions agent-authz-placement\n", encoding="utf-8"
    )
    hits = _check_forbidden(tmp_path)
    assert hits, "_check_forbidden did not detect 'agent-authz-placement' in a planted file (G4)"
    assert any("agent-authz-placement" in h for h in hits), (
        f"hits did not mention 'agent-authz-placement': {hits}"
    )


# ---------------------------------------------------------------------------
# G5: Write and Edit for ./.specify/feature.json must be in allowedTools
# ---------------------------------------------------------------------------

def test_g5_generate_sh_allows_write_to_specify_feature_json():
    """G5: generate.sh allowed-tools must permit Write(./.specify/feature.json)."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert "Write(./.specify/feature.json)" in text, (
        "generate.sh must include Write(./.specify/feature.json) in its allowedTools (G5)"
    )


def test_g5_generate_sh_allows_edit_to_specify_feature_json():
    """G5: generate.sh allowed-tools must permit Edit(./.specify/feature.json)."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert "Edit(./.specify/feature.json)" in text, (
        "generate.sh must include Edit(./.specify/feature.json) in its allowedTools (G5)"
    )


# ---------------------------------------------------------------------------
# G6: uv sync must run in the kit before the first generation step
# ---------------------------------------------------------------------------

def test_g6_generate_sh_runs_uv_sync_before_generation():
    """G6: generate.sh must run 'uv sync' in the kit before the first generation step."""
    text = _GENERATE_SH.read_text(encoding="utf-8")

    assert "uv sync" in text, (
        "generate.sh must run 'uv sync' to install dependencies before generation steps (G6)"
    )

    sync_pos = text.find("uv sync")
    first_run_step_pos = text.find("run_step ")
    assert first_run_step_pos != -1, "generate.sh has no run_step call"
    assert sync_pos < first_run_step_pos, (
        f"'uv sync' (pos {sync_pos}) must appear before the first 'run_step' "
        f"call (pos {first_run_step_pos}) in generate.sh (G6)"
    )


# ---------------------------------------------------------------------------
# G7: allowed tools include Glob and Grep scoped to the kit;
#     do NOT include Bash(find, Bash(ls, Bash(python -c, Bash(uv run python
# ---------------------------------------------------------------------------

def test_g7_generate_sh_allows_glob_scoped_to_kit():
    """G7: generate.sh allowed-tools must include a kit-scoped Glob(./**) (read-only)."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert re.search(r"Glob\(\.\/", text), (
        "generate.sh must include Glob(./**) or similar kit-scoped Glob in allowedTools (G7)"
    )


def test_g7_generate_sh_allows_grep_scoped_to_kit():
    """G7: generate.sh allowed-tools must include a kit-scoped Grep(./**) (read-only)."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert re.search(r"Grep\(\.\/", text), (
        "generate.sh must include Grep(./**) or similar kit-scoped Grep in allowedTools (G7)"
    )


def test_g7_generate_sh_does_not_allow_bash_find():
    """G7: generate.sh must not allow Bash(find ...) in its allowed-tools."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert not re.search(r"Bash\(find\b", text), (
        "generate.sh must not permit Bash(find ...) in allowedTools (G7)"
    )


def test_g7_generate_sh_does_not_allow_bash_ls():
    """G7: generate.sh must not allow Bash(ls ...) in its allowed-tools."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert not re.search(r"Bash\(ls\b", text), (
        "generate.sh must not permit Bash(ls ...) in allowedTools (G7)"
    )


def test_g7_generate_sh_does_not_allow_python_c():
    """G7: generate.sh must not allow Bash(python -c ...) in its allowed-tools."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert not re.search(r"Bash\(python\s+-c", text), (
        "generate.sh must not permit Bash(python -c ...) in allowedTools (G7)"
    )


def test_g7_generate_sh_does_not_allow_uv_run_python():
    """G7: generate.sh must not allow Bash(uv run python ...) in its allowed-tools."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert not re.search(r"Bash\(uv run python\b", text), (
        "generate.sh must not permit Bash(uv run python ...) in allowedTools (G7)"
    )


# ---------------------------------------------------------------------------
# G8: Write/Edit for ./tests/generated/** only; spec_input.md names that path;
#     seal check detects modification of tests/test_contract.py vs. built kit
# ---------------------------------------------------------------------------

def test_g8_generate_sh_allows_write_to_tests_generated():
    """G8: generate.sh allowed-tools must permit Write inside ./tests/generated/."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert re.search(r"Write\(\.\/tests\/generated\/", text), (
        "generate.sh must include Write(./tests/generated/**) in its allowedTools (G8)"
    )


def test_g8_generate_sh_allows_edit_to_tests_generated():
    """G8: generate.sh allowed-tools must permit Edit inside ./tests/generated/."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert re.search(r"Edit\(\.\/tests\/generated\/", text), (
        "generate.sh must include Edit(./tests/generated/**) in its allowedTools (G8)"
    )


def test_g8_spec_input_says_generator_tests_in_tests_generated():
    """G8: spec_input.md must state that the generator's own tests go in tests/generated/."""
    text = (_APPROACH3 / "spec_input.md").read_text(encoding="utf-8")
    assert "tests/generated" in text, (
        "spec_input.md must mention tests/generated/ as the location for "
        "the generator's own tests (G8)"
    )


def test_g8_seal_check_detects_modified_kit_file(tmp_path):
    """G8: seal_check must detect if tests/test_contract.py or any other kit file
    was modified relative to the kit as built by build_kit.
    """
    import shutil
    from arms.approach3.import_gen import seal_check
    from arms.approach3.make_kit import build_kit

    ref = tmp_path / "ref"
    build_kit(ref)

    run_kit = tmp_path / "run"
    shutil.copytree(ref, run_kit)
    (run_kit / "tests" / "test_contract.py").write_text(
        "# tampered by generator\n", encoding="utf-8"
    )

    result = seal_check(run_kit)
    assert result["status"] != "clean", (
        "seal_check must detect modification of tests/test_contract.py "
        f"vs. the built kit (G8); got status {result['status']!r}"
    )


# ---------------------------------------------------------------------------
# G9: Bash(./.specify/scripts/bash/*) must be in allowedTools
# ---------------------------------------------------------------------------

def test_g9_generate_sh_allows_specify_scripts_bash():
    """G9: generate.sh must include Bash(./.specify/scripts/bash/*) in allowedTools."""
    text = _GENERATE_SH.read_text(encoding="utf-8")
    assert re.search(r"Bash\(\.\/\.specify\/scripts\/bash\/", text), (
        "generate.sh must include Bash(./.specify/scripts/bash/*) in its allowedTools (G9)"
    )
