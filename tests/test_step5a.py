"""
Tests for multi-hop step 5a: A2A dependency gate.

Three acceptance tests, all of which must FAIL against the current code:

  T1  lock_purity  – every package in the frozen tag's uv.lock has the same
                     version in the current uv.lock, AND a2a-sdk is present
                     (fails now because a2a-sdk is absent from the lock).

  T2  kit_frozen   – the kit built by make_kit.build_kit is byte-identical to
                     what the frozen pipeline would produce.  Depends on a
                     committed snapshot of the kit's uv.lock and agent configs
                     at arms/approach3/kit_snapshot/ (fails now because that
                     snapshot has not been committed yet).

  T3  a2a_import   – strands.multiagent.a2a is importable (fails now because
                     the a2a-sdk package is not installed).
"""

import hashlib
import pathlib
import re
import subprocess
import sys
import tempfile

import pytest

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_APPROACH3 = _REPO_ROOT / "arms" / "approach3"
_KIT_SNAPSHOT = _APPROACH3 / "kit_snapshot"

_FROZEN_TAG = "approach3-pipeline-frozen"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_lock_versions(text: str) -> dict[str, str]:
    """Return {package_name: version} for every [[package]] block that has a
    version field.  The project entry (guides-vs-gates / checkpoint-gen)
    is excluded because its version is not meaningful for this check."""
    skip = {"guides-vs-gates", "checkpoint-gen"}
    result: dict[str, str] = {}
    for block in re.split(r"\n(?=\[\[package\]\])", text):
        m_name = re.search(r'^name\s*=\s*"([^"]+)"', block, re.MULTILINE)
        m_ver  = re.search(r'^version\s*=\s*"([^"]+)"', block, re.MULTILINE)
        if m_name and m_ver and m_name.group(1) not in skip:
            result[m_name.group(1)] = m_ver.group(1)
    return result


def _frozen_lock_text() -> str:
    return subprocess.check_output(
        ["git", "show", f"{_FROZEN_TAG}:uv.lock"],
        cwd=_REPO_ROOT,
    ).decode()


def _dir_hash(root: pathlib.Path) -> dict[str, str]:
    """Return a mapping of relative-path → sha256 for every file under root."""
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = str(path.relative_to(root))
            result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


# ---------------------------------------------------------------------------
# T1: lock purity + a2a-sdk presence
# ---------------------------------------------------------------------------

def test_lock_no_existing_package_version_changed():
    """Guard: every package present in the frozen tag's uv.lock must have
    the same version in the current uv.lock.

    This prevents accidentally bumping an existing package when adding the
    a2a extra.
    """
    frozen  = _parse_lock_versions(_frozen_lock_text())
    current = _parse_lock_versions((_REPO_ROOT / "uv.lock").read_text())

    changed = {
        name: (frozen[name], current[name])
        for name in frozen
        if name in current and frozen[name] != current[name]
    }
    assert not changed, (
        "Existing packages changed version (frozen → current):\n"
        + "\n".join(f"  {n}: {a} → {b}" for n, (a, b) in sorted(changed.items()))
    )


def test_lock_contains_a2a_sdk():
    """a2a-sdk must be present in uv.lock (added by the strands-agents[a2a] extra).

    FAILS now because strands-agents[a2a] has not been added to the lock yet.
    """
    current = _parse_lock_versions((_REPO_ROOT / "uv.lock").read_text())
    assert "a2a-sdk" in current, (
        "a2a-sdk is not in uv.lock. "
        "Add strands-agents[a2a]==1.57.1 to pyproject.toml and run uv lock."
    )


# ---------------------------------------------------------------------------
# T2: kit byte-identical to frozen reference
# ---------------------------------------------------------------------------

def test_kit_snapshot_committed():
    """arms/approach3/kit_snapshot/uv.lock must be committed.

    FAILS now because the snapshot has not been committed yet.
    The snapshot is the kit's uv.lock as it would be derived from the
    frozen tag's uv.lock, committed so that later lock changes cannot
    alter what make_kit produces.
    """
    assert _KIT_SNAPSHOT.exists(), (
        f"kit_snapshot/ directory not found at {_KIT_SNAPSHOT}. "
        "Commit the frozen kit inputs (uv.lock + config/agents/) to "
        "arms/approach3/kit_snapshot/."
    )
    kit_lock = _KIT_SNAPSHOT / "uv.lock"
    assert kit_lock.exists(), (
        f"arms/approach3/kit_snapshot/uv.lock not found. "
        "Commit the kit uv.lock snapshot."
    )
    agents_dir = _KIT_SNAPSHOT / "config" / "agents"
    assert agents_dir.exists(), (
        f"arms/approach3/kit_snapshot/config/agents/ not found. "
        "Commit the agent config snapshot."
    )


def test_kit_byte_identical_to_frozen():
    """The kit built by make_kit.build_kit must be byte-identical to the
    reference kit built from the approach3-pipeline-frozen tag's inputs.

    Concretely: build two kits in temp directories and compare every file:
      reference – built by running the frozen tag's make_kit.py against the
                  frozen tag's files (retrieved via git show), with _REPO_ROOT
                  and _APPROACH3 patched AFTER exec_module so that module-
                  level assignments are overridden.
      current   – built by the current make_kit.build_kit (reads from the
                  committed kit_snapshot/).

    FAILS until kit_snapshot/ is committed AND make_kit reads from it.
    """
    if not (_KIT_SNAPSHOT / "uv.lock").exists():
        pytest.fail(
            "arms/approach3/kit_snapshot/uv.lock is not committed. "
            "Cannot verify kit byte-identity without the frozen snapshot."
        )

    import importlib.util  # noqa: PLC0415
    import shutil  # noqa: PLC0415

    sys.path.insert(0, str(_REPO_ROOT))
    from arms.approach3.make_kit import build_kit  # noqa: PLC0415

    def _git_show(tag_path: str) -> bytes:
        return subprocess.check_output(
            ["git", "show", f"{_FROZEN_TAG}:{tag_path}"],
            cwd=_REPO_ROOT,
        )

    with tempfile.TemporaryDirectory() as tmp_ref, tempfile.TemporaryDirectory() as tmp_cur:
        ref_dir = pathlib.Path(tmp_ref) / "kit"
        cur_dir = pathlib.Path(tmp_cur) / "kit"

        # ── current kit (reads from committed kit_snapshot/) ──────────────
        build_kit(cur_dir)

        # ── reference kit (frozen tag's make_kit.py + frozen tag's files) ──
        scratch = pathlib.Path(tmp_ref) / "scratch"
        scratch.mkdir()

        (scratch / "uv.lock").write_bytes(_git_show("uv.lock"))
        (scratch / "policy.md").write_bytes(_git_show("policy.md"))

        domain_dst = scratch / "domain"
        domain_dst.mkdir()
        for name in (
            "__init__.py", "fixtures.py", "identity.py", "scopes.py",
            "server.py", "simclock.py", "state.py", "tokens.py",
        ):
            try:
                (domain_dst / name).write_bytes(_git_show(f"domain/{name}"))
            except subprocess.CalledProcessError:
                pass

        c_hook_cfg = scratch / "arms" / "c_hook" / "config"
        c_hook_cfg.mkdir(parents=True)
        for yaml_name in ("expense-assistant.yaml", "payments-agent.yaml", "travel-assistant.yaml"):
            try:
                (c_hook_cfg / yaml_name).write_bytes(
                    _git_show(f"arms/c_hook/config/{yaml_name}")
                )
            except subprocess.CalledProcessError:
                pass

        approach3_src = scratch / "arms" / "approach3"
        approach3_src.mkdir(parents=True)
        for name in ("_contract_tests.py", "spec_input.md"):
            try:
                (approach3_src / name).write_bytes(
                    _git_show(f"arms/approach3/{name}")
                )
            except subprocess.CalledProcessError:
                pass

        # Load the frozen tag's make_kit.py source and execute it, then patch
        # module-level globals AFTER exec_module (so our patches win over the
        # module-level assignments computed from __file__).
        frozen_make_kit_src = _git_show("arms/approach3/make_kit.py").decode()
        frozen_make_kit_path = pathlib.Path(tmp_ref) / "make_kit_frozen.py"
        frozen_make_kit_path.write_text(frozen_make_kit_src, encoding="utf-8")

        spec = importlib.util.spec_from_file_location(
            "make_kit_frozen", frozen_make_kit_path
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # Patch AFTER exec so module-level _REPO_ROOT / _APPROACH3 assignments
        # (which use pathlib.Path(__file__).parent…) are overridden.
        mod._REPO_ROOT = scratch   # type: ignore[attr-defined]
        mod._APPROACH3 = approach3_src  # type: ignore[attr-defined]
        mod.build_kit(ref_dir)

        cur_hashes = _dir_hash(cur_dir)
        ref_hashes = _dir_hash(ref_dir)

        diff: list[str] = []
        for rel in sorted(set(cur_hashes) | set(ref_hashes)):
            if cur_hashes.get(rel) != ref_hashes.get(rel):
                diff.append(f"  {rel}")

        assert not diff, (
            "Kit is not byte-identical to the frozen reference.\n"
            "Files that differ or are missing in one kit:\n" + "\n".join(diff) + "\n"
            "Ensure make_kit reads from arms/approach3/kit_snapshot/ "
            "rather than the live uv.lock and agent configs."
        )


# ---------------------------------------------------------------------------
# T3: strands.multiagent.a2a importable
# ---------------------------------------------------------------------------

def test_strands_multiagent_a2a_imports():
    """strands.multiagent.a2a must be importable.

    FAILS now because strands-agents[a2a] (and its a2a-sdk dependency)
    are not installed in the virtual environment.
    """
    import importlib  # noqa: PLC0415
    try:
        importlib.import_module("strands.multiagent.a2a")
    except ImportError as exc:
        pytest.fail(
            f"strands.multiagent.a2a is not importable: {exc}\n"
            "Install strands-agents[a2a]==1.57.1 (uv sync after updating the lock)."
        )
