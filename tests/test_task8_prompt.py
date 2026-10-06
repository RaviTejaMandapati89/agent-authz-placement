"""Task 8, D10/D12: approaches 2 to 6 share one identity-only system prompt;
only approach 1 carries the policy.md rules. The model is the only thing
swapped; agents, servers and the run engine are production code."""
import json
import pathlib
import re

from domain.server import _ScriptedModel
from runner import study
from tests.study_scripts import SCRIPTS

_TIMEOUT = 120
_RULE_ID = re.compile(r"\bP[1-7]\b")


def _run(approach, sid, tmp_path, variant=None):
    scripted, pay = SCRIPTS[(sid, variant)]
    return study.run_scenario(approach, sid, variant=variant, out_dir=tmp_path,
                              model=_ScriptedModel(scripted), payments_turns=pay,
                              timeout_s=_TIMEOUT)


def _first_prompt(row, source):
    path = pathlib.Path(row["run_dir"]) / "transcript.jsonl"
    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    prompts = [x["system_prompt"] for x in lines if x["source"] == source and x.get("system_prompt")]
    assert prompts, f"no {source} call with a system prompt in {path}"
    return prompts[0]


def test_prompt_is_identity_only_and_identical_for_approaches_2_to_6(tmp_path):
    prompts = {a: _first_prompt(_run(a, "S7", tmp_path / str(a)), "harness")
               for a in (2, 4, 5, 6)}
    assert len(set(prompts.values())) == 1, prompts
    prompt = prompts[2]
    assert "£" not in prompt, prompt
    assert not _RULE_ID.search(prompt), prompt


def test_payments_agent_prompt_carries_rules_only_under_approach_1(tmp_path):
    with_rules = _first_prompt(_run(1, "S15", tmp_path / "a1", "refusal"), "server")
    assert _RULE_ID.search(with_rules), "approach 1 payments agent lost its rules"
    for a in (4, 5, 6):
        prompt = _first_prompt(_run(a, "S15", tmp_path / f"a{a}", "refusal"), "server")
        assert "£" not in prompt, (a, prompt)
        assert not _RULE_ID.search(prompt), (a, prompt)
