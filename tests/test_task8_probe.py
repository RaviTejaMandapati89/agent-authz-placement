"""Task 8, item 4 and D3: the concurrency probe. Fixed real-model subset at
1, 2, 4 and 8 workers; it stops raising the level at the first throttling or
error."""
import json
import multiprocessing

import pytest

from domain.server import _ScriptedModel
from runner import scenarios, study
from tests.study_scripts import SCRIPTS

_TIMEOUT = 120
_SUBSET = [(5, "S1", None), (2, "S2", None)]


def _probe():
    from runner import probe
    return probe


def _in_a_worker() -> bool:
    return multiprocessing.parent_process() is not None


class _ThrottledOnceInWorkers(_ScriptedModel):
    """The model service throttles the first request, but only when the run is
    in a worker process (so only above one worker). Strands retries it."""

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        if _in_a_worker() and self._idx == 0:
            from strands.types.exceptions import ModelThrottledException
            self._idx += 1
            raise ModelThrottledException("simulated throttling")
        async for ev in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield ev

    stream.__wrapped__ = True


class _BrokenInWorkers(_ScriptedModel):
    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        if _in_a_worker():
            raise RuntimeError("simulated model failure")
        async for ev in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield ev

    stream.__wrapped__ = True


def _kwargs(cls):
    def f(item):
        turns, pay = SCRIPTS[(item[2], item[3])]
        return {"model": cls(turns), "payments_turns": pay}
    return f


def _kwargs_plain(item):
    return _kwargs(_ScriptedModel)(item)


def _kwargs_throttled(item):
    return _kwargs(_ThrottledOnceInWorkers)(item)


def _kwargs_broken(item):
    return _kwargs(_BrokenInWorkers)(item)


def test_t8_probe_subset_is_sixteen_real_model_runs_all_from_the_scenario_files():
    probe = _probe()
    items = probe.subset_items()
    assert len(items) == 16 and len(set(items)) == 16
    for a, g, sid, v, n in items:
        raw = scenarios.load(sid)
        assert scenarios.applies(raw, a) and raw["run"] in ("agent", "chain")   # calls the model
        assert v in scenarios.variants(raw)
    assert probe.LEVELS == (1, 2, 4, 8)


def test_t8_probe_subset_is_the_d8_subset_s1_s3_s7_and_s15_refusal_under_approaches_1_2_4_5():
    items = _probe().subset_items()
    assert {(sid, v) for a, g, sid, v, n in items} == \
        {("S1", None), ("S3", None), ("S7", None), ("S15", "refusal")}
    assert {a for a, g, sid, v, n in items} == {1, 2, 4, 5}
    assert all(g is None and n == 1 for a, g, sid, v, n in items)


def test_t8_probe_records_runs_per_minute_throttling_and_errors_at_each_level(tmp_path):
    from runner import budget
    res = _probe().run_probe(tmp_path, subset=_SUBSET, levels=(1, 2), item_kwargs=_kwargs_plain,
                             budget=budget.Budget(max_tokens=10**9), timeout_s=_TIMEOUT)
    assert [x["workers"] for x in res["levels"]] == [1, 2]
    for lv in res["levels"]:
        assert lv["runs"] == 2 and lv["errors"] == 0 and lv["throttled"] == 0
        assert lv["runs_per_min"] > 0 and lv["wall_s"] > 0 and lv["tokens"] > 0
    assert res["stopped_at"] is None and res["safe_level"] == 2
    saved = json.loads((tmp_path / "probe.json").read_text())
    assert saved["levels"] == res["levels"]
    assert (tmp_path / "level-2" / "results.jsonl").exists()


def test_t8_probe_stops_raising_the_level_at_the_first_throttling(tmp_path):
    from runner import budget
    res = _probe().run_probe(tmp_path, subset=_SUBSET, levels=(1, 2, 4, 8),
                             item_kwargs=_kwargs_throttled,
                             budget=budget.Budget(max_tokens=10**9), timeout_s=_TIMEOUT)
    assert [x["workers"] for x in res["levels"]] == [1, 2]       # 4 and 8 never ran
    assert res["levels"][0]["throttled"] == 0 and res["levels"][1]["throttled"] >= 1
    assert res["stopped_at"] == {"workers": 2, "reason": "throttling"}
    assert res["safe_level"] == 1
    assert not (tmp_path / "level-4").exists()


def test_t8_probe_stops_raising_the_level_at_the_first_error(tmp_path):
    from runner import budget
    res = _probe().run_probe(tmp_path, subset=_SUBSET, levels=(1, 2, 4, 8),
                             item_kwargs=_kwargs_broken,
                             budget=budget.Budget(max_tokens=10**9), timeout_s=_TIMEOUT)
    assert [x["workers"] for x in res["levels"]] == [1, 2]
    assert res["levels"][1]["errors"] == 2
    assert res["stopped_at"] == {"workers": 2, "reason": "error"}
    assert res["safe_level"] == 1


def test_t8_probe_with_the_real_model_refuses_without_max_tokens(tmp_path, capsys):
    with pytest.raises(SystemExit) as e:
        _probe().main(["--out", str(tmp_path / "p")])
    assert e.value.code not in (0, None)
    assert "max-tokens" in capsys.readouterr().err
