"""Build a fresh workspace for one S11 policy generation.

A workspace holds exactly four files: that use case's requirements text, the
Cedar schema, the gateway facts and the prompt. Nothing else: no existing
policy and no other use case's text or output.

Usage: python -m arms.s11gen.make_workspace <dest> <use_case>
"""
import pathlib
import shutil
import sys

HERE = pathlib.Path(__file__).parent
USE_CASES = ("expenses", "payments")
# workspace file name -> source, relative to this folder, per use case
INPUT_NAMES = ("requirements.md", "schema.json", "gateway_facts.md", "prompt.md")


def sources(use_case: str) -> dict[str, pathlib.Path]:
    if use_case not in USE_CASES:
        raise ValueError(f"use case must be one of {USE_CASES}, not {use_case!r}")
    return {
        "requirements.md": HERE / "requirements" / f"{use_case}.md",
        "schema.json": HERE / "inputs" / "schema.json",
        "gateway_facts.md": HERE / "inputs" / "gateway_facts.md",
        "prompt.md": HERE / "inputs" / "prompt.md",
    }


def build(dest: pathlib.Path, use_case: str) -> pathlib.Path:
    dest = pathlib.Path(dest)
    if dest.exists():
        raise FileExistsError(f"{dest} exists: every generation gets a fresh workspace")
    src = sources(use_case)
    dest.mkdir(parents=True)
    for name, path in src.items():
        shutil.copyfile(path, dest / name)
    return dest


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("Usage: python -m arms.s11gen.make_workspace <dest> <use_case>")
    print(build(pathlib.Path(sys.argv[1]).expanduser(), sys.argv[2]))
