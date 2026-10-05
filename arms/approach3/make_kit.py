#!/usr/bin/env python3
"""
Build a generation kit for the checkpoint pipeline.

Usage: python arms/approach3/make_kit.py <N>

Creates ~/checkpoint-gen/run-<N>/ containing:
  - domain/       (allow-listed files only, including scopes.py)
  - config/agents/ (the three agent config YAML files)
  - spec_input.md
  - policy.md
  - _contract_tests.py  (copied as tests/test_contract.py)
  - pyproject.toml
  - CLAUDE.md

Fails if the assembled kit contains any word from FORBIDDEN_WORDS or any
pattern matching an approach number reference.
"""

import hashlib
import pathlib
import re
import shutil
import sys

_REPO_ROOT  = pathlib.Path(__file__).parent.parent.parent
_APPROACH3  = pathlib.Path(__file__).parent

# Words that must not appear anywhere in the kit (case-sensitive).
FORBIDDEN_WORDS = [
    "cedar",
    "gateway",
    "grants",
    "central_service",
    "central_publisher",
    "c_hook",
    "d_boundary",
    "PREREG",
    "gvg",
    "arm3",
    "guides-vs-gates",
    "agent-authz-placement",
]

# Regex that matches "approach" followed by an optional space or underscore
# and then a digit — e.g. "approach 2", "approach_3", "approach4".
_APPROACH_DIGIT_RE = re.compile(r"approach[\s_]?\d", re.IGNORECASE)

# Domain files the generator is allowed to see (K3: seven agreed files + scopes.py).
_DOMAIN_ALLOW = frozenset({
    "__init__.py",
    "fixtures.py",
    "identity.py",
    "scopes.py",
    "server.py",
    "simclock.py",
    "state.py",
    "tokens.py",
})

_PYPROJECT = """\
[project]
name = "checkpoint-gen"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "pyyaml>=6.0.3",
    "httpx>=0.27",
    "PyJWT>=2.8",
    "cryptography>=42",
    "strands-agents>=0.1",
    "python-dotenv>=1.0",
]

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.24",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
"""

# Project entry written into the kit's uv.lock in place of the repo project.
# Matches _PYPROJECT exactly: direct deps only, no forbidden words.
_CHECKPOINT_GEN_LOCK_ENTRY = """\
[[package]]
name = "checkpoint-gen"
version = "0.1.0"
source = { virtual = "." }
dependencies = [
    { name = "cryptography" },
    { name = "httpx" },
    { name = "pyjwt" },
    { name = "python-dotenv" },
    { name = "pyyaml" },
    { name = "strands-agents" },
]

[package.dev-dependencies]
dev = [
    { name = "pytest" },
    { name = "pytest-asyncio" },
]

[package.metadata]
requires-dist = [
    { name = "cryptography", specifier = ">=42" },
    { name = "httpx", specifier = ">=0.27" },
    { name = "pyjwt", specifier = ">=2.8" },
    { name = "python-dotenv", specifier = ">=1.0" },
    { name = "pyyaml", specifier = ">=6.0.3" },
    { name = "strands-agents", specifier = ">=0.1" },
]

[package.metadata.requires-dev]
dev = [
    { name = "pytest", specifier = ">=8" },
    { name = "pytest-asyncio", specifier = ">=0.24" },
]

"""


def _build_kit_uv_lock(repo_root: pathlib.Path, kit_dir: pathlib.Path) -> None:
    """Derive a kit-specific uv.lock from the repo's lock.

    Keeps every package entry unchanged (preserving exact versions and hashes),
    removes the cedarpy entry (contains forbidden word), and replaces the repo
    project entry with a checkpoint-gen entry that matches the kit's pyproject.toml.
    The result is a valid frozen lock file for `uv sync --frozen`.
    """
    lock_text = (repo_root / "uv.lock").read_text(encoding="utf-8")

    # Split at every [[package]] header, keeping the header as first segment.
    positions = [m.start() for m in re.finditer(r"^\[\[package\]\]", lock_text, re.MULTILINE)]
    header = lock_text[: positions[0]] if positions else lock_text

    blocks: list[str] = []
    for i, pos in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(lock_text)
        block = lock_text[pos:end]
        m = re.match(r"\[\[package\]\]\s*\nname\s*=\s*\"([^\"]+)\"", block)
        name = m.group(1) if m else None

        if name == "cedarpy":
            continue  # cedarpy's name contains the forbidden word "cedar"
        if name in ("guides-vs-gates",):
            blocks.append(_CHECKPOINT_GEN_LOCK_ENTRY)
            continue
        blocks.append(block)

    (kit_dir / "uv.lock").write_text(header + "".join(blocks), encoding="utf-8")


def _check_forbidden(kit_root: pathlib.Path) -> list[str]:
    """Return a list of violation strings for any forbidden word or approach+digit
    pattern found in the kit."""
    hits: list[str] = []
    for path in sorted(kit_root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(kit_root)
        for word in FORBIDDEN_WORDS:
            if word in text:
                hits.append(f"{rel}: contains {word!r}")
        if _APPROACH_DIGIT_RE.search(text):
            hits.append(f"{rel}: matches forbidden pattern 'approach[_\\s]?\\d'")
    return hits


def _strip_blocks(text: str) -> str:
    """Remove lines between block markers (inclusive) and neutralise any
    remaining section comment that names the removed feature."""
    BLOCK_STARTS = ("# --- GATEWAY BEGIN ---", "# --- ARM D BEGIN ---")
    BLOCK_ENDS   = ("# --- GATEWAY END ---",   "# --- ARM D END ---")
    out = []
    skip = False
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if any(stripped == s for s in BLOCK_STARTS):
            skip = True
            continue
        if any(stripped == s for s in BLOCK_ENDS):
            skip = False
            continue
        if skip:
            continue
        # Reword the section comment that named the stripped feature.
        out.append(
            line.replace(
                "# Revocation listeners and reset callbacks (approach 6)",
                "# Revocation listeners and reset callbacks",
            )
        )
    return "".join(out)


def _copy_domain(src: pathlib.Path, dst: pathlib.Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for py in sorted(src.glob("*.py")):
        if py.name not in _DOMAIN_ALLOW:
            continue
        if py.name == "server.py":
            stripped = _strip_blocks(py.read_text(encoding="utf-8"))
            (dst / py.name).write_text(stripped, encoding="utf-8")
        else:
            shutil.copy2(py, dst / py.name)


def _copy_agent_configs(src_dir: pathlib.Path, dst_dir: pathlib.Path) -> None:
    """Copy agent config YAML files into dst_dir/config/agents/."""
    out = dst_dir / "config" / "agents"
    out.mkdir(parents=True, exist_ok=True)
    for yaml_file in sorted(src_dir.glob("*.yaml")):
        shutil.copy2(yaml_file, out / yaml_file.name)


def _copy_policy_md(src: pathlib.Path, dst: pathlib.Path) -> None:
    """Copy policy.md, omitting the background-reading footnote that names PREREG."""
    text = src.read_text(encoding="utf-8")
    lines = [ln for ln in text.splitlines(keepends=True) if "PREREG" not in ln]
    dst.write_text("".join(lines), encoding="utf-8")


def build_kit(target_dir: pathlib.Path) -> None:
    """Build a complete approach-3 kit into target_dir (created if absent).

    The kit's uv.lock and agent configs are read from the committed snapshot
    at arms/approach3/kit_snapshot/ so that later changes to the repo's
    uv.lock or live agent configs cannot alter what this function produces.

    Raises RuntimeError if forbidden words are found in the assembled kit.
    """
    _KIT_SNAPSHOT = _APPROACH3 / "kit_snapshot"

    target_dir.mkdir(parents=True, exist_ok=True)

    _copy_domain(_REPO_ROOT / "domain", target_dir / "domain")

    _copy_agent_configs(
        _KIT_SNAPSHOT / "config" / "agents",
        target_dir,
    )

    tests_dst = target_dir / "tests"
    tests_dst.mkdir(exist_ok=True)
    (tests_dst / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(_APPROACH3 / "_contract_tests.py", tests_dst / "test_contract.py")

    (target_dir / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")
    (target_dir / "CLAUDE.md").write_text("Run tests with: uv run pytest\n", encoding="utf-8")
    shutil.copy2(_APPROACH3 / "spec_input.md", target_dir / "spec_input.md")
    _copy_policy_md(_REPO_ROOT / "policy.md", target_dir / "policy.md")
    shutil.copy2(_KIT_SNAPSHOT / "uv.lock", target_dir / "uv.lock")

    hits = _check_forbidden(target_dir)
    if hits:
        raise RuntimeError("kit contains forbidden content:\n" + "\n".join(hits))


def verify_reads_config_at_runtime(
    hook_class,
    agent_name: str,
    config_dir_a: pathlib.Path,
    config_dir_b: pathlib.Path,
) -> bool:
    """Return True iff two instances built with different config_dirs have different
    effective allowed_tool sets, proving that tools are read at run time.

    Returns False if both instances have the same tools (hard-coded), or if
    instantiation raises an exception.
    """
    def _get_tools(hook):
        t = getattr(hook, "_allowed_tools", None)
        if t is not None:
            return sorted(t)
        cfg = getattr(hook, "_config", None)
        if isinstance(cfg, dict):
            t = cfg.get("allowed_tools")
            if t is not None:
                return sorted(t)
        return None

    try:
        hook_a = hook_class(
            agent_name=agent_name,
            user="testuser",
            base_url="http://localhost:9999",
            config_dir=config_dir_a,
        )
        hook_b = hook_class(
            agent_name=agent_name,
            user="testuser",
            base_url="http://localhost:9999",
            config_dir=config_dir_b,
        )
    except Exception:
        return False

    tools_a = _get_tools(hook_a)
    tools_b = _get_tools(hook_b)
    if tools_a is None or tools_b is None:
        return False
    return tools_a != tools_b


def build(gen_n: int) -> pathlib.Path:
    kit_root = pathlib.Path.home() / "checkpoint-gen" / f"run-{gen_n}"

    if kit_root.exists():
        print(f"Kit already exists at {kit_root}")
        print("Delete it first, or choose a different N.")
        sys.exit(1)

    try:
        build_kit(kit_root)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        shutil.rmtree(kit_root, ignore_errors=True)
        sys.exit(1)

    print(f"Kit built at: {kit_root}")
    files = sorted(kit_root.rglob("*"))
    total = sum(1 for f in files if f.is_file())
    print(f"{total} files written")

    for f in sorted(files):
        if f.is_file():
            digest = hashlib.sha256(f.read_bytes()).hexdigest()[:12]
            print(f"  {f.relative_to(kit_root)}  {digest}")

    return kit_root


def main() -> None:
    if len(sys.argv) != 2:
        print("Usage: python arms/approach3/make_kit.py <N>")
        sys.exit(1)
    try:
        n = int(sys.argv[1])
    except ValueError:
        print("N must be an integer")
        sys.exit(1)
    build(n)


if __name__ == "__main__":
    main()
