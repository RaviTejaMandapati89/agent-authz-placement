"""Recompute every S11 generation manifest's hashes and report any mismatch.

Usage: python -m arms.s11gen.seal      (exit 1 on a mismatch or a missing file)
Checks, for each arms/s11gen/gen-*/<use_case>/: the policy, each stored input
and the generation log against manifest.json, that nothing else is in the
folder, and that every pair has both use cases.
"""
import hashlib
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).parent
USE_CASES = ("expenses", "payments")
INPUT_NAMES = ("requirements.md", "schema.json", "gateway_facts.md", "prompt.md")


def _sha(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def problems(root: pathlib.Path = HERE) -> tuple[int, list[str]]:
    """(manifests checked, problems found)."""
    found: list[str] = []
    checked = 0
    for gen in sorted(root.glob("gen-*")):
        for uc in USE_CASES:
            d = gen / uc
            if not (d / "manifest.json").exists():
                found.append(f"{gen.name}/{uc}: no manifest.json")
                continue
            checked += 1
            m = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
            label = f"{gen.name}/{uc}"
            if not (d / "policy.cedar").exists() or _sha(d / "policy.cedar") != m["policy_sha256"]:
                found.append(f"{label}: policy.cedar differs from its manifest")
            for name, digest in m["inputs_sha256"].items():
                p = d / "inputs" / name
                if not p.exists() or _sha(p) != digest:
                    found.append(f"{label}: inputs/{name} differs from its manifest")
            if "log_sha256" in m and (not (d / "generation.jsonl").exists()
                                      or _sha(d / "generation.jsonl") != m["log_sha256"]):
                found.append(f"{label}: generation.jsonl differs from its manifest")
            expected = {"manifest.json", "policy.cedar", *(f"inputs/{n}" for n in m["inputs_sha256"])}
            if "log_sha256" in m:
                expected.add("generation.jsonl")
            present = {p.relative_to(d).as_posix() for p in d.rglob("*") if p.is_file()}
            if present != expected:
                found.append(f"{label}: files differ from the manifest: {sorted(present ^ expected)}")
    return checked, found


if __name__ == "__main__":
    n, bad = problems()
    for line in bad:
        print(line)
    print(f"s11gen seal: {n} manifests checked, {len(bad)} problems")
    sys.exit(1 if bad else 0)
