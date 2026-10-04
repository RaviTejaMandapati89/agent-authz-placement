#!/usr/bin/env python3
"""
Build an arm B generation kit.

Usage: python arms/b_spec/make_kit.py <N>

Creates ~/gvg-arm-b/gen-<N>/ containing:
  - domain/  (copy of repo domain/ with arm D code stripped)
  - tests/conftest.py and tests/test_contract.py
  - pyproject.toml  (minimal, mcp pinned to current version)
  - spec_input.md

Fails if the kit contains the strings:
  cedar, d_boundary, c_hook, hook, arms, PREREG
"""

import hashlib
import pathlib
import re
import shutil
import sys

_REPO_ROOT = pathlib.Path(__file__).parent.parent.parent
_B_SPEC    = pathlib.Path(__file__).parent

# Words that must not appear anywhere in the kit (case-sensitive).
_FORBIDDEN = ["cedar", "d_boundary", "c_hook", "hook", "arms", "PREREG", "central_publisher"]

_ARM_D_MARKER_RE = re.compile(
    r"[ \t]*# --- ARM D BEGIN ---.*?# --- ARM D END ---\n?",
    re.DOTALL,
)

_GATEWAY_MARKER_RE = re.compile(
    r"[ \t]*# --- GATEWAY BEGIN ---.*?# --- GATEWAY END ---\n?",
    re.DOTALL,
)

_DOMAIN_ALLOW = frozenset({
    "__init__.py",
    "fixtures.py",
    "identity.py",
    "server.py",
    "simclock.py",
    "state.py",
    "tokens.py",
})

_PYPROJECT = """\
[project]
name = "gvg-arm-b-gen"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "mcp==1.30.0",
    "pyyaml>=6.0.3",
    "httpx>=0.27",
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


def _strip_arm_d(text: str) -> str:
    return _ARM_D_MARKER_RE.sub("", text)


def _strip_gateway(text: str) -> str:
    return _GATEWAY_MARKER_RE.sub("", text)


def _check_forbidden(kit_root: pathlib.Path) -> list[str]:
    hits: list[str] = []
    for path in sorted(kit_root.rglob("*")):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        rel = path.relative_to(kit_root)
        for word in _FORBIDDEN:
            if word in text:
                hits.append(f"{rel}: contains {word!r}")
    return hits


def _copy_domain(src: pathlib.Path, dst: pathlib.Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    for py in sorted(src.glob("*.py")):
        if py.name not in _DOMAIN_ALLOW:
            continue
        text = py.read_text(encoding="utf-8")
        stripped = _strip_gateway(_strip_arm_d(text))
        (dst / py.name).write_text(stripped, encoding="utf-8")


def build_kit(target_dir: pathlib.Path) -> None:
    """Build a complete arm B kit into target_dir (created if absent).

    Raises RuntimeError if forbidden words are found in the assembled kit.
    """
    target_dir.mkdir(parents=True, exist_ok=True)

    _copy_domain(_REPO_ROOT / "domain", target_dir / "domain")

    tests_dst = target_dir / "tests"
    tests_dst.mkdir(exist_ok=True)
    (tests_dst / "__init__.py").write_text("", encoding="utf-8")
    shutil.copy2(_REPO_ROOT / "tests" / "conftest.py", tests_dst / "conftest.py")
    shutil.copy2(_B_SPEC / "_contract_tests.py", tests_dst / "test_contract.py")

    (target_dir / "pyproject.toml").write_text(_PYPROJECT, encoding="utf-8")

    (target_dir / "CLAUDE.md").write_text("Run tests with: uv run pytest\n", encoding="utf-8")

    shutil.copy2(_B_SPEC / "spec_input.md", target_dir / "spec_input.md")

    hits = _check_forbidden(target_dir)
    if hits:
        raise RuntimeError("kit contains forbidden content:\n" + "\n".join(hits))


def build(gen_n: int) -> pathlib.Path:
    kit_root = pathlib.Path.home() / "gvg-arm-b" / f"gen-{gen_n}"

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

    print(f"Kit built: {kit_root}")
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
        print("Usage: python arms/b_spec/make_kit.py <N>")
        sys.exit(1)
    try:
        n = int(sys.argv[1])
    except ValueError:
        print("N must be an integer")
        sys.exit(1)
    build(n)


if __name__ == "__main__":
    main()
