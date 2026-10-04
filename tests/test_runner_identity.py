"""Item 12: the runner uses /control/identity/* and /identity/exchange, requests
each agent's own scopes, records claims (never full tokens), and moves time
only via /control/clock/advance."""
import json
import pathlib
from unittest.mock import MagicMock

import pytest
import yaml


EXPECTED_SCOPES = {
    "expense-assistant": sorted(["expenses:read", "expenses:submit", "expenses:approve"]),
    "travel-assistant": sorted(["expenses:submit", "travel:book"]),
    "payments-agent": sorted(["expenses:approve", "payments:pay"]),
}


class _FakeResponse:
    """Minimal httpx.Response stand-in for runner HTTP calls."""

    def __init__(self, body=None, status_code=200):
        self._body = body if body is not None else {"ok": True}
        self.status_code = status_code
        self.headers = {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def _capture_http(monkeypatch):
    """Patch httpx.post and httpx.get at the module level used by runner.run.
    Returns the list that collects (method, url, json_body) tuples."""
    import httpx as _httpx

    calls: list[tuple] = []

    _original_post = _httpx.post
    _original_get = _httpx.get

    def fake_post(url, **kwargs):
        body = kwargs.get("json")
        calls.append(("POST", url, body))
        if "/control/identity/" in url:
            return _FakeResponse({"access_token": "eyJfake"})
        if "/identity/exchange" in url:
            return _FakeResponse({"access_token": "eyJexchanged"})
        return _FakeResponse()

    def fake_get(url, **kwargs):
        calls.append(("GET", url, None))
        return _FakeResponse([])

    monkeypatch.setattr("httpx.post", fake_post)
    monkeypatch.setattr("httpx.get", fake_get)
    return calls


def _write_scenario(tmp_path, scenario_id, agent, user):
    sdir = tmp_path / "scenarios"
    sdir.mkdir(exist_ok=True)
    (sdir / f"{scenario_id}.yaml").write_text(yaml.dump({
        "agent": agent,
        "user": user,
        "turns": ["Do a thing"],
    }))
    return sdir


def _run_one_captured(monkeypatch, tmp_path, agent, user, scenario_id="S1"):
    """Set up mocks, run one scenario, return (calls, record)."""
    calls = _capture_http(monkeypatch)

    sdir = _write_scenario(tmp_path, scenario_id, agent, user)
    monkeypatch.setattr("runner.run._SCENARIOS_DIR", sdir)

    mock_arm = MagicMock()
    mock_arm.run.return_value = ("reply", {"input_tokens": 0, "output_tokens": 0}, [])
    monkeypatch.setattr("runner.run._load_arm", lambda arm: mock_arm)

    runs_out = tmp_path / "runs.jsonl"
    decision_log = tmp_path / "decisions.jsonl"
    decision_log.touch()

    from runner.run import run_one
    run_one("A", scenario_id, 1, "http://fake", decision_log, runs_out, "abc", False)

    record = json.loads(runs_out.read_text().splitlines()[-1])
    return calls, record


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------

def test_runner_calls_identity_endpoints(monkeypatch, tmp_path):
    """The runner calls /control/identity/* and /identity/exchange."""
    calls, _ = _run_one_captured(monkeypatch, tmp_path, "expense-assistant", "alice")
    post_urls = [url for method, url, _ in calls if method == "POST"]
    assert any("/control/identity/user-token" in u for u in post_urls), (
        "runner must call /control/identity/user-token"
    )
    assert any("/control/identity/agent-token" in u for u in post_urls), (
        "runner must call /control/identity/agent-token"
    )
    assert any("/identity/exchange" in u for u in post_urls), (
        "runner must call /identity/exchange"
    )


@pytest.mark.parametrize("agent,expected_scopes", list(EXPECTED_SCOPES.items()))
def test_runner_requests_correct_scopes_per_agent(
    agent, expected_scopes, monkeypatch, tmp_path,
):
    """The runner requests each agent's own scopes from the agent-token endpoint."""
    calls, _ = _run_one_captured(monkeypatch, tmp_path, agent, "alice")
    agent_token_calls = [
        body for method, url, body in calls
        if method == "POST" and body and "/control/identity/agent-token" in url
    ]
    assert agent_token_calls, "no agent-token call found"
    requested_scope = sorted(agent_token_calls[0].get("scope", []))
    assert requested_scope == expected_scopes


def test_runner_records_claims_not_full_tokens(monkeypatch, tmp_path):
    """The run record must not contain raw JWT strings (eyJ… prefixes)."""
    _, record = _run_one_captured(monkeypatch, tmp_path, "expense-assistant", "alice")
    serialised = json.dumps(record)
    assert "eyJ" not in serialised, (
        "run record contains a raw JWT token — record claims only"
    )


def test_runner_moves_time_only_via_clock_advance(monkeypatch, tmp_path):
    """The runner never calls /control/clock/set; only /control/clock/advance."""
    calls, _ = _run_one_captured(monkeypatch, tmp_path, "expense-assistant", "alice")
    post_urls = [url for method, url, _ in calls if method == "POST"]
    clock_set_calls = [u for u in post_urls if "/control/clock/set" in u]
    assert clock_set_calls == [], (
        "runner must not use /control/clock/set — use /control/clock/advance only"
    )
