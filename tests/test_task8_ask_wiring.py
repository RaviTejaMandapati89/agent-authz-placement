"""Task 8, D13 (Fix A): ask_payments_agent is wired into an agent's tool list only
where domain/grants.py grants it. The server is real, started through the
runner's domain_server, with a timeout. The model is the only thing swapped: a
scripted model that records the tool names it is offered. Nothing calls Bedrock.
"""
import asyncio
import importlib

import pytest

from domain import grants, server
from domain.server import _ScriptedModel
from runner import approaches as ap
from runner import study
from runner._server import domain_server

TOOL = "ask_payments_agent"
_PAYMENTS_USER = "erin"
_FIRST_AGENT_USER = {"expense-assistant": "alice", "travel-assistant": "dan"}


class _Offered(_ScriptedModel):
    """A scripted model that records the tool names it is offered."""

    def __init__(self, turns, seen):
        super().__init__(turns)
        self._seen = seen

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self._seen.append(sorted(s["name"] for s in tool_specs or []))
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield event


@pytest.fixture(autouse=True)
def _no_bedrock(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("a test must not call Bedrock")
    monkeypatch.setattr("strands.models.bedrock.BedrockModel.__init__", boom)


def _env(approach, tmp_path):
    a = ap.get(approach)
    tmp_path.mkdir(parents=True, exist_ok=True)
    env = {"ISSUER_LOG": str(tmp_path / "issuer.jsonl"), "HOOK_LOG": str(tmp_path / "hook.jsonl"),
           "PAYMENTS_AGENT_APPROACH": str(approach), "SERVER_TEST_MODE": "1"}
    if approach == 3:
        env["PAYMENTS_AGENT_GEN"] = "gen-1"
    if a.gateway:
        env.update({"GATEWAY": "true", "GATEWAY_PLUGIN": a.gateway_plugin})
    return env


def _offered_agent_side(approach, agent, tmp_path):
    """Tool names the agent's own run() offers its model, through the approach's module."""
    a = ap.get(approach)
    user = _PAYMENTS_USER if agent == "payments-agent" else _FIRST_AGENT_USER[agent]
    seen: list[list[str]] = []
    with domain_server(str(tmp_path / "decisions.jsonl"), env_extra=_env(approach, tmp_path)) as (_p, base):
        study._post(base, "/control/reset")
        bearer = study._issue_obo(base, user, agent)
        kwargs = dict(agent_name=agent, user=user, turns=["Hello."], mcp_url=f"{base}/mcp",
                      bearer_token=bearer, model=_Offered([], seen), use_gateway=a.gateway)
        if approach == 3:
            kwargs["gen"] = 1
        importlib.import_module(a.agent_module).run(**kwargs)
    assert seen and seen[0], f"{agent} under approach {approach} was offered no tools"
    return seen[0]


def _offered_server_side(approach, monkeypatch, tmp_path):
    """Tool names the server's own payments-agent (S15, S16) offers its model."""
    a = ap.get(approach)
    seen: list[list[str]] = []
    with domain_server(str(tmp_path / "decisions.jsonl"), env_extra=_env(approach, tmp_path)) as (port, base):
        study._post(base, "/control/reset")
        bearer = study._issue_obo(base, _PAYMENTS_USER, "payments-agent")
        monkeypatch.setenv("SERVER_PORT", str(port))
        monkeypatch.setattr(server, "_payments_agent_model_turns", [])
        monkeypatch.setattr(server, "_ScriptedModel", lambda turns: _Offered(turns, seen))
        gen = "gen-1" if approach == 3 else ""
        server._run_payments_agent("Say hello.", bearer, _PAYMENTS_USER, str(approach), gen, a.gateway)
    assert seen and seen[0], f"server-side payments-agent under approach {approach} was offered no tools"
    return seen[0]


@pytest.mark.parametrize("where", ["agent side", "server side"])
@pytest.mark.parametrize("approach", ap.IDS)
def test_payments_agent_is_not_wired_ask_payments_agent(approach, where, monkeypatch, tmp_path):
    assert TOOL not in grants.AGENT_GRANTS["payments-agent"]
    offered = (_offered_agent_side(approach, "payments-agent", tmp_path) if where == "agent side"
               else _offered_server_side(approach, monkeypatch, tmp_path))
    assert TOOL not in offered, (
        f"payments-agent ({where}, approach {approach}) was offered {offered}; "
        f"domain/grants.py grants it {grants.AGENT_GRANTS['payments-agent']}")


@pytest.mark.parametrize("approach", ap.IDS)
@pytest.mark.parametrize("agent", ["expense-assistant", "travel-assistant"])
def test_expense_assistant_keeps_ask_payments_agent(approach, agent, tmp_path):
    """Guard: agents granted the tool keep it. Passes before and after the fix."""
    assert TOOL in grants.AGENT_GRANTS[agent]
    offered = _offered_agent_side(approach, agent, tmp_path)
    assert TOOL in offered, f"{agent} under approach {approach} lost {TOOL}: {offered}"
