"""The study engine (task 7): runs one scenario for one approach end to end,
grades it from the logs, and writes one row.

Every run starts its own server through the runner's domain_server, with its
own decision, checkpoint and issuer logs. Setup steps are control actions on
that server (the real issuers, the central source's own state changes, the
simulated clock). The only thing a test may swap is the model.
"""
import concurrent.futures
import contextlib
import multiprocessing
import datetime
import json
import os
import pathlib
import tempfile
import time
import uuid

import httpx

from domain import tokens
from runner import approaches as ap
from runner import config as cfg
from runner import budget as budget_mod
from runner import grader, model_record, overrides, places, scenarios, timing
from runner._server import domain_server
from runner.run import _AGENT_SCOPES, _direct_mcp_call

REPO = pathlib.Path(__file__).parent.parent
_HOP_AUDIENCE = "payments-agent"


class RunError(Exception):
    """A failure outside the approach: the model service or the harness."""


# ---------------------------------------------------------------------------
# small harness helpers
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _environ(**kv):
    old = {k: os.environ.get(k) for k in kv}
    os.environ.update({k: v for k, v in kv.items() if v is not None})
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _post(base: str, path: str, body: dict | None = None) -> dict:
    r = httpx.post(f"{base}{path}", json=body or {}, timeout=60.0)
    r.raise_for_status()
    return r.json()


def _sim_now(base: str) -> float:
    return _post(base, "/control/clock/advance", {"seconds": 0})["sim_time"]


def _advance(base: str, seconds: float) -> float:
    return _post(base, "/control/clock/advance", {"seconds": seconds})["sim_time"]


def _issue_obo(base: str, user: str, agent: str, *, lifetime: int = tokens.DEFAULT_LIFETIME,
               audience: str | None = None, extra_scopes: list[str] | None = None) -> str:
    """A real on-behalf-of token: the real issuers, then the exchange.
    Scopes are the agent's own, from runner._AGENT_SCOPES, plus any a labelled
    positive-control scenario adds."""
    scopes = list(_AGENT_SCOPES[agent]) + list(extra_scopes or [])
    user_tok = _post(base, "/control/identity/user-token", {
        "sub": user, "aud": agent, "scope": scopes, "lifetime": lifetime})["access_token"]
    agent_tok = _post(base, "/control/identity/agent-token", {
        "sub": agent, "scope": scopes, "lifetime": lifetime})["access_token"]
    body = {"subject_token": user_tok, "actor_token": agent_tok}
    if audience:
        body["audience"] = audience
    return _post(base, "/identity/exchange", body)["access_token"]


def _tool_url(base: str, approach: int) -> str:
    return f"{base}/gateway/mcp" if ap.get(approach).gateway else f"{base}/mcp"


def _call(url: str, tool: str, args: dict, bearer: str) -> dict:
    return _direct_mcp_call(url, tool, args, {"Authorization": f"Bearer {bearer}"})


def _is_refused(result: dict) -> bool:
    return bool(result.get("isError"))


def _app_call(base: str, user: str, args: dict) -> dict:
    from runner.run import _app_expense_call
    return _app_expense_call(base, user, args)


def _probe_offsets(spec: dict, until: float) -> list[float]:
    """Offsets after the change: the listed ones, then every `every` seconds up
    to and including `until`."""
    offsets = list(spec.get("offsets", []))
    every = spec.get("every", 60)
    t = every
    while t <= until:
        if t not in offsets:
            offsets.append(t)
        t += every
    return sorted(offsets)


# ---------------------------------------------------------------------------
# agent runs
# ---------------------------------------------------------------------------

def _run_agent(approach: int, gen: int | None, *, agent: str, user: str, turns: list[str],
               base: str, run_id: str, bearer: str, model, ov: overrides.RunOverrides,
               hook_log: pathlib.Path, between_turns_fn=None):
    """Drive one agent through the approach's own run(). model is the only
    thing swapped. Returns (final_reply, usage, turns_data)."""
    import importlib
    a = ap.get(approach)
    mod = importlib.import_module(a.agent_module)
    mcp_url = f"{base}/mcp"
    common = dict(agent_name=agent, user=user, turns=turns, mcp_url=mcp_url,
                  run_id=run_id, between_turns_fn=between_turns_fn,
                  bearer_token=bearer, model=model, use_gateway=a.gateway)

    def wrap(hook):
        return timing.timed_hook(hook, hook_log, run_id, approach)

    if approach in (1, 4, 5, 6):
        mod.apply_policy_change(ov.prompt_change or {})
        try:
            return mod.run(**common, extra_tool_names=ov.extra_tool_names or None,
                           rules_in_prompt=approach == 1)
        finally:
            mod.apply_policy_change({})
    if approach == 2:
        return mod.run(**common, config_dir=ov.config_dir, hook_wrap=wrap)
    return mod.run(**common, config_dir=ov.config_dir, gen=gen, hook_wrap=wrap)


# ---------------------------------------------------------------------------
# checkpoint probe (approaches 2 and 3): the checkpoint as the harness calls
# it before a tool call, then the tool
# ---------------------------------------------------------------------------

class CheckpointProbe:
    def __init__(self, approach: int, gen: int | None, *, agent: str, user: str, base: str,
                 run_id: str, bearer: str, hook_log: pathlib.Path,
                 config_dir: pathlib.Path | None):
        from strands.hooks.registry import HookRegistry
        from strands.tools.mcp import MCPClient
        if approach == 2:
            from arms.c_hook.hook import PolicyHook
        else:
            from arms.approach3.agent import DEFAULT_CONFIG_DIR, _load_checkpoint
            PolicyHook = _load_checkpoint(gen)
            config_dir = config_dir or DEFAULT_CONFIG_DIR
        hook = PolicyHook(agent_name=agent, user=user, base_url=base, run_id=run_id,
                          limit_override=None, config_dir=config_dir, bearer_token=bearer)
        timing.timed_hook(hook, hook_log, run_id, approach, source="probe")
        self._registry = HookRegistry()
        hook.register_hooks(self._registry)
        self._bearer = bearer
        self._base = base
        self._hook_log = hook_log
        self._run_id = run_id
        self._url = f"{base}/mcp"
        client = MCPClient(url=self._url, headers={"Authorization": f"Bearer {bearer}"})
        with client:
            self._tools = {t.tool_name: t for t in client.list_tools_sync()}

    def call(self, tool: str, arguments: dict) -> dict:
        from strands.hooks import BeforeToolCallEvent
        probe_id = f"probe-{uuid.uuid4().hex[:8]}"
        # The harness's own clock at the probe. The grader joins this line to the
        # checkpoint's decision line by probe_id (a checkpoint that refuses before
        # reading the facts store logs no sim_time of its own).
        with pathlib.Path(self._hook_log).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "probe", "run_id": self._run_id,
                                 "probe_id": probe_id, "tool": tool,
                                 "sim_time": _sim_now(self._base)}) + "\n")
        event = BeforeToolCallEvent(
            agent=None, selected_tool=self._tools.get(tool),
            tool_use={"toolUseId": probe_id, "name": tool, "input": dict(arguments)},
            invocation_state={})
        try:
            self._registry.invoke_callbacks(event)
        except Exception as exc:  # the checkpoint's own failure; logged by it
            return {"refused": True, "crashed": True, "reason": str(exc)}
        if event.cancel_tool:
            return {"refused": True, "reason": str(event.cancel_tool)}
        result = _call(self._url, tool, arguments, self._bearer)
        return {"refused": _is_refused(result), "reason": None}


# ---------------------------------------------------------------------------
# flows
# ---------------------------------------------------------------------------

def _setup(base: str, approach: int, scenario: dict, ov: overrides.RunOverrides,
           user: str, agent: str | None) -> None:
    for step in scenarios.steps_for(scenario.get("setup"), approach):
        action, args = step["action"], step.get("args", {})
        if action == "apply-change":
            if ov.central_limit is not None:
                _post(base, "/control/set-limit", {"limit": ov.central_limit})
        else:
            _post(base, f"/control/{action}", args)


def _flow_agent(ctx: dict) -> dict:
    s = ctx["scenario"]
    reply, usage, _ = _run_agent(
        ctx["approach"], ctx["gen"], agent=s["agent"], user=s["user"],
        turns=s["turns"], base=ctx["base"], run_id=ctx["run_id"],
        bearer=ctx["bearer"], model=ctx["model"], ov=ctx["ov"], hook_log=ctx["hook_log"])
    return {"reply": reply, "usage": usage}


def _flow_direct(ctx: dict) -> dict:
    s, approach, base = ctx["scenario"], ctx["approach"], ctx["base"]
    d = s["direct"]
    tok = s.get("token") or {"kind": "obo"}
    kind = tok["kind"]
    user, agent = s["user"], s["agent"]
    if kind == "obo":
        bearer = ctx["bearer"]
    elif kind == "expired":
        bearer = _issue_obo(base, user, agent, lifetime=tok.get("lifetime", 60))
        _advance(base, tok.get("advance", 120))
    elif kind == "wrong-issuer":
        # A third issuer from the production class: its own key, an issuer id
        # no verifier trusts. It uses the simulated time the server reports.
        rogue = tokens.Issuer("untrusted-issuer", "untrusted-key")
        bearer = rogue.issue(
            user, tokens.SERVER_AUDIENCE, _AGENT_SCOPES[agent], tokens.DEFAULT_LIFETIME,
            extra={"act": {"sub": agent}, "role": "employee", "reports_to": [],
                   "delegations_received": []},
            now=_sim_now(base), kind="wrong-issuer")
    else:
        raise ValueError(f"unknown token kind {kind!r}")
    url = f"{base}/mcp" if d.get("path") == "mcp" else _tool_url(base, approach)
    result = _call(url, d["tool"], d.get("args", {}), bearer)
    return {"reply": "(direct call)", "usage": {}, "result_refused": _is_refused(result)}


def _flow_window(ctx: dict) -> dict:
    """S5: the first booking through the agent, the revocation as a state
    change, then probes with the real token the agent held (no model)."""
    s, approach, gen, base = ctx["scenario"], ctx["approach"], ctx["gen"], ctx["base"]
    agent, user, bearer = s["agent"], s["user"], ctx["bearer"]
    lifetime = s["token_lifetime"]
    turns = s["turns"]
    revoke = {"delegation_id": s["revoke"]["delegation_id"]}

    def do_revoke():
        _post(base, "/control/revoke-delegation", revoke)

    if approach == 1:
        # no check outside the model: the result is the agent's single rebooking attempt
        reply, usage, _ = _run_agent(
            approach, gen, agent=agent, user=user, turns=turns, base=base,
            run_id=ctx["run_id"], bearer=bearer, model=ctx["model"], ov=ctx["ov"],
            hook_log=ctx["hook_log"], between_turns_fn=do_revoke)
        return {"reply": reply, "usage": usage, "probes": 0}

    reply, usage, _ = _run_agent(
        approach, gen, agent=agent, user=user, turns=turns[:1], base=base,
        run_id=ctx["run_id"], bearer=bearer, model=ctx["model"], ov=ctx["ov"],
        hook_log=ctx["hook_log"])
    do_revoke()
    t_rev = _sim_now(base)

    probe_args = dict(s["probe_call"]["arguments"])
    tool = s["probe_call"]["tool"]
    if approach in (2, 3):
        probe = CheckpointProbe(approach, gen, agent=agent, user=user, base=base,
                                run_id=ctx["run_id"], bearer=bearer,
                                hook_log=ctx["hook_log"], config_dir=ctx["ov"].config_dir)
        do_probe = lambda: probe.call(tool, probe_args)
    else:
        url = _tool_url(base, approach)
        do_probe = lambda: {"refused": _is_refused(_call(url, tool, probe_args, bearer))}

    offsets = _probe_offsets(s["probes"], lifetime + s["probes"]["grace"])
    results = []
    for off in offsets:
        _advance(base, t_rev + off - _sim_now(base))
        results.append({"offset": off, **do_probe()})
    return {"reply": reply, "usage": usage, "probes": len(offsets), "probe_results": results}


def _flow_paired(ctx: dict) -> dict:
    """S10: the same request through the agent and through the non-agent
    channel, at the moment of the central limit change and at later instants."""
    s, approach, base = ctx["scenario"], ctx["approach"], ctx["base"]
    agent, user = s["agent"], s["user"]
    request = dict(s["request"])
    url = _tool_url(base, approach)
    t_change = _sim_now(base)
    rounds = []

    reply, usage, _ = _run_agent(
        approach, ctx["gen"], agent=agent, user=user, turns=s["turns"], base=base,
        run_id=ctx["run_id"], bearer=ctx["bearer"], model=ctx["model"], ov=ctx["ov"],
        hook_log=ctx["hook_log"])
    rounds.append({"offset": 0, "app": _app_call(base, user, request)["status"]})

    spec = s["probes"]
    for off in _probe_offsets(spec, spec["horizon"]):
        if off == 0:
            continue
        _advance(base, t_change + off - _sim_now(base))
        tok = _issue_obo(base, user, agent)
        agent_res = _call(url, "submit_expense", {"claimant": user, **request}, tok)
        app_res = _app_call(base, user, request)
        rounds.append({"offset": off, "agent_refused": _is_refused(agent_res),
                       "app": app_res["status"]})
    return {"reply": reply, "usage": usage, "rounds": len(rounds)}


def _flow_chain(ctx: dict) -> dict:
    """S15 and S16: the first agent asks payments-agent. The harness exchanges
    the token at the hop; the model never does."""
    s, approach, base = ctx["scenario"], ctx["approach"], ctx["base"]
    agent, user = s["agent"], s["user"]
    deleg = _issue_obo(base, user, agent, audience=_HOP_AUDIENCE,
                       extra_scopes=ctx["ov"].scope_adds.get(agent))
    reply, usage, _ = _run_agent(
        approach, ctx["gen"], agent=agent, user=user, turns=s["turns"], base=base,
        run_id=ctx["run_id"], bearer=deleg, model=ctx["model"], ov=ctx["ov"],
        hook_log=ctx["hook_log"])
    return {"reply": reply, "usage": usage}


def _flow_s11(ctx: dict) -> dict:
    """S11: for each use case, a scripted (model-free) attempt by the claimant to
    approve their own expense, through the approach's gateway with the real
    on-behalf-of token. A case may first make the claimant's expense through
    the production app channel (the use-case policies are not involved)."""
    s, approach, base = ctx["scenario"], ctx["approach"], ctx["base"]
    url = _tool_url(base, approach)
    results = {}
    for name, case in s["cases"].items():
        args = dict(case.get("args", {}))
        if "own_expense" in case:
            made = _app_call(base, case["user"], dict(case["own_expense"]))
            if made["status"] != 200:
                raise RuntimeError(f"S11 {name}: setup expense refused: {made}")
            args["expense_id"] = made["body"]["expense"]["id"]
        bearer = _issue_obo(base, case["user"], case["agent"])
        results[name] = {"refused": _is_refused(_call(url, case["tool"], args, bearer)),
                         "expense_id": args["expense_id"]}
    return {"reply": "(direct calls)", "usage": {}, "cases": results}


_FLOWS = {"agent": _flow_agent, "direct": _flow_direct, "window": _flow_window,
          "paired": _flow_paired, "chain": _flow_chain, "s11": _flow_s11}


# ---------------------------------------------------------------------------
# error classification
# ---------------------------------------------------------------------------

# What the model service raises (Bedrock through botocore, or Strands' own
# throttling exception). EventLoopException is only a wrapper, so it is not listed:
# its cause decides.
_MODEL_ERRORS = ("ModelThrottledException", "ClientError", "ReadTimeoutError",
                 "EndpointConnectionError", "ModelException")


def _error_category(exc: BaseException) -> str:
    seen = 0
    while exc is not None and seen < 8:
        if type(exc).__name__ in _MODEL_ERRORS or type(exc).__module__.startswith("botocore"):
            return "model service"
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return "harness"


def _facts_store_error(exc: BaseException) -> bool:
    """Was this exception a failed read of the facts store (the directory)?"""
    seen = 0
    while exc is not None and seen < 10:
        if isinstance(exc, httpx.HTTPStatusError) and \
                exc.request.url.path.startswith("/directory/"):
            return True
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return False


def _checkpoint_failed(exc: BaseException, hook_log: pathlib.Path,
                       server_log: pathlib.Path, run_id: str) -> bool:
    """The agent stopped because of the approach's own checkpoint: it logged an
    error as its last decision, or its read of the facts store failed while the
    store was down (a checkpoint that raises without logging). The run is
    graded as the approach's behaviour, not recorded as an error."""
    mine = [e for e in grader.read_jsonl(hook_log)
            if e.get("run_id") == run_id and "decision" in e and "tool" in e]
    if mine and mine[-1]["decision"] == "error":
        return True
    unserved = any(e.get("type") == "directory_read" and e.get("served") is False
                   for e in grader.read_jsonl(server_log))
    return unserved and _facts_store_error(exc)


# ---------------------------------------------------------------------------
# one run
# ---------------------------------------------------------------------------

def _row_base(approach, gen, scenario_id, variant, run_num, git_sha, dirty) -> dict:
    return {
        "run_id": str(uuid.uuid4()),
        "approach": approach, "approach_label": ap.label(approach),
        "generation": gen, "scenario": scenario_id, "variant": variant,
        "run_num": run_num, "model_id": cfg.MODEL_ID,
        "git_sha": git_sha, "dirty": dirty,
    }


def _write_row(out_dir: pathlib.Path, row: dict) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    with (out_dir / "results.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, default=str) + "\n")
    return row


def run_tag(approach, gen, scenario_id, variant, run_num) -> str:
    """The run's directory name: one per run, so no two runs share a path."""
    return f"{approach}{'-g' + str(gen) if gen else ''}-{scenario_id}" \
           f"{'-' + variant if variant else ''}-{run_num}"


def _metrics(approach: int, scenario: dict, logs: grader.RunLogs, flow_out: dict) -> dict:
    m: dict = {
        "false_refusals": grader.false_refusals(scenario, logs),
        "audit": grader.audit_completeness(approach, logs),
        "latency": grader.latency_samples(approach, logs),
        "central_calls_per_decision": grader.central_calls_per_decision(approach, logs),
    }
    sid = scenario["id"]
    if sid == "S5":
        if approach == 1:
            after = grader._ledger_slice(logs.ledger, {"action_type": "delegation_revoked"}, "after")
            m["freshness"] = {"window_s": None, "note": "n/a: the agent's single rebooking attempt",
                              "rebooking": "allowed" if any(
                                  e["action_type"] == "travel_booked" for e in after) else "refused"}
        else:
            m["freshness"] = grader.freshness_window(logs)
    if sid in ("S7", "S10"):
        m["change_cost"] = places.change_cost(approach)
    if sid == "S8":
        m["failure_behaviour"] = grader.failure_behaviour(logs, approach)
    if sid == "S10":
        m["consistency"] = grader.consistency(logs)
    if sid == "S11":
        m["s11"] = grader.s11_cases(scenario, logs, approach)
    if sid in ("S15", "S16"):
        m["chains"] = grader.agent_chains(logs)
    return m


def run_scenario(
    approach: int, scenario_id: str, *, variant: str | None = None, run_num: int = 1,
    gen: int | None = None, out_dir: pathlib.Path, model=None,
    payments_turns: list[dict] | None = None, timeout_s: float = 600.0,
    git_sha: str = "unknown", dirty: bool = False, write_row: bool = True,
) -> dict:
    """Run one scenario for one approach; grade it from the logs; append one
    row to out_dir/results.jsonl (unless write_row is False: a batch's parent
    writes the rows) and return it. `model` None means the real model."""
    out_dir = pathlib.Path(out_dir)
    emit = _write_row if write_row else (lambda _out, r: r)
    ap.get(approach)
    if approach == 3 and gen is None:
        raise ValueError("approach 3 runs against a generation: pass gen=1..5")
    raw = scenarios.load(scenario_id)
    row = _row_base(approach, gen, scenario_id, variant, run_num, git_sha, dirty)

    if scenarios.status(raw) == "blocked":
        return emit(out_dir, {**row, "status": "blocked", "verdict": "blocked",
                                    "reason": raw.get("blocked_reason")})
    if not scenarios.applies(raw, approach):
        return emit(out_dir, {**row, "status": "n/a", "verdict": "n/a"})

    scenario = scenarios.resolve(raw, variant)
    tag = run_tag(approach, gen, scenario_id, variant, run_num)
    run_dir = out_dir / "runs" / tag
    run_dir.mkdir(parents=True, exist_ok=True)
    server_log, hook_log, issuer_log = (run_dir / "decisions.jsonl",
                                        run_dir / "hook_decisions.jsonl",
                                        run_dir / "issuer.jsonl")
    a = ap.get(approach)
    run_id = row["run_id"]

    model_log = run_dir / "model_calls.jsonl"
    env = {"ISSUER_LOG": str(issuer_log), "HOOK_LOG": str(hook_log),
           "PAYMENTS_AGENT_APPROACH": str(approach), model_record.ENV_VAR: str(model_log)}
    # The model is the only thing a run swaps; whichever it is, it is recorded.
    model = model_record.RecordingModel(
        model, factory=model_record.real_model if model is None else None,
        log_path=model_log, source="harness")
    if approach == 3:
        env["PAYMENTS_AGENT_GEN"] = f"gen-{gen}"
    if a.gateway:
        env.update({"GATEWAY": "true", "GATEWAY_PLUGIN": a.gateway_plugin})
    if payments_turns is not None:
        env["SERVER_TEST_MODE"] = "1"

    start = time.monotonic()
    started_at = time.time()
    server_port = None
    status, error_cause, error_category = "ok", None, None
    flow_out: dict = {}
    ledger: list[dict] = []
    with tempfile.TemporaryDirectory() as tmp, \
            _environ(HOOK_LOG=str(hook_log), ISSUER_LOG=str(issuer_log)):
        ov = overrides.build(approach, scenario, pathlib.Path(tmp))
        env.update(ov.env)
        try:
            with domain_server(str(server_log), env_extra=env) as (_port, base):
                server_port = _port
                _post(base, "/control/reset")
                _post(base, "/control/set-run", {"run_id": run_id, "scenario": scenario_id,
                                                 "arm": str(approach)})
                if payments_turns is not None:
                    _post(base, "/control/set_model_turns",
                          {"agent": "payments-agent", "turns": payments_turns})
                def issue_bearer() -> str:
                    if scenario.get("agent") and scenario.get("run") != "chain":
                        return _issue_obo(base, scenario["user"], scenario["agent"],
                                          lifetime=scenario.get("token_lifetime",
                                                                tokens.DEFAULT_LIFETIME))
                    return ""

                # Tokens are issued after setup (a setup step may move the clock),
                # unless the scenario needs them issued first (S8's outage).
                bearer = issue_bearer() if scenario.get("tokens_before_setup") else ""
                ctx = {"approach": approach, "gen": gen, "scenario": scenario, "base": base,
                       "run_id": run_id, "bearer": bearer, "model": model, "ov": ov,
                       "hook_log": hook_log}
                try:
                    _setup(base, approach, scenario, ov, scenario.get("user"), scenario.get("agent"))
                    if not scenario.get("tokens_before_setup"):
                        ctx["bearer"] = issue_bearer()
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                        fut = pool.submit(_FLOWS[scenario.get("run", "agent")], ctx)
                        flow_out = fut.result(timeout=timeout_s)
                except concurrent.futures.TimeoutError:
                    status, error_cause = "error", f"timeout after {timeout_s}s"
                    error_category = "harness"
                except Exception as exc:
                    if approach in (2, 3) and _checkpoint_failed(exc, hook_log, server_log, run_id):
                        flow_out = {"aborted_by_checkpoint": True}
                    else:
                        status = "error"
                        error_cause = f"{type(exc).__name__}: {exc}"
                        error_category = _error_category(exc)
                ledger = httpx.get(f"{base}/control/ledger", timeout=60.0).json()
        except Exception as exc:  # server could not start or died: the harness
            status, error_cause = "error", f"{type(exc).__name__}: {exc}"
            error_category = "harness"
        record_overrides = {"notes": ov.notes, "diffs": ov.diffs}

    model_usage = model_record.usage(model_log)
    if model_usage["id_mismatches"]:
        # Any request sent with another model id makes the run an error, whatever
        # else happened, and the error names both ids (D2).
        m = model_usage["id_mismatches"][0]
        status, error_category = "error", "model id"
        error_cause = f"model id mismatch: request sent with {m['sent']!r}, pinned {m['pinned']!r}"
    row.update({
        "status": status, "duration_s": round(time.monotonic() - start, 3),
        "started_at": started_at, "finished_at": time.time(),
        "worker_pid": os.getpid(), "server_port": server_port,
        "model_usage": model_usage,
        "overrides": record_overrides, "run_dir": str(run_dir),
    })
    if status == "error":
        row.update({"verdict": "error", "error_cause": error_cause,
                    "error_category": error_category})
        return emit(out_dir, row)

    (run_dir / "ledger.json").write_text(json.dumps(ledger))
    logs = grader.load_logs(ledger, server_log, hook_log, issuer_log)
    graded = grader.grade_run(scenario, logs, approach)
    row.update({
        **graded,
        "completed": graded["legitimate_completed"] is True,
        # Every model call of the run, the server's agents included (one source).
        "input_tokens": model_usage["input_tokens"],
        "output_tokens": model_usage["output_tokens"],
        "aborted_by_checkpoint": bool(flow_out.get("aborted_by_checkpoint")),
        "metrics": _metrics(approach, scenario, logs, flow_out),
    })
    return emit(out_dir, row)


# ---------------------------------------------------------------------------
# batches
# ---------------------------------------------------------------------------

def plan(approach_ids, scenario_ids, runs: int, gens=None):
    """Every (approach, generation, scenario, variant, run) in order. Approach 3
    runs `runs` times against each generation (DESIGN.md section 4)."""
    for a in approach_ids:
        gen_list = (list(gens) if gens else list(ap.GENERATIONS)) if a == 3 else [None]
        for sid in scenario_ids:
            raw = scenarios.load(sid)
            if scenarios.status(raw) == "blocked" or not scenarios.applies(raw, a):
                yield (a, gen_list[0], sid, None, 1)
                continue
            for v in scenarios.variants(raw):
                for g in gen_list:
                    for n in range(1, runs + 1):
                        yield (a, g, sid, v, n)


def is_run(item) -> bool:
    """Does this plan item start a run? A blocked scenario, or one that does not
    apply to the approach, is only recorded (plan() gives it one placeholder)."""
    a, _g, sid, _v, _n = item
    raw = scenarios.load(sid)
    return scenarios.status(raw) != "blocked" and scenarios.applies(raw, a)


def _run_item(index, item, out_dir, item_kwargs, git_sha, dirty, timeout_s, model=None):
    """One plan item in whichever process calls it. The batch's parent writes the
    row; this only runs the scenario and returns it."""
    a, g, sid, v, n = item
    kw = item_kwargs(item) if item_kwargs else ({"model": model} if model is not None else {})
    row = run_scenario(a, sid, variant=v, run_num=n, gen=g, out_dir=out_dir, git_sha=git_sha,
                       dirty=dirty, timeout_s=timeout_s, write_row=False, **kw)
    row["plan_index"] = index
    return row


def _describe(item, row) -> str:
    a, g, sid, v, n = item
    return (f"  approach {a}{f' gen-{g}' if g else ''} {sid}"
            f"{f' [{v}]' if v else ''} run {n}: {row['verdict']}"
            f"{' (' + row['error_cause'] + ')' if row.get('error_cause') else ''}")


def run_items(items, out_dir, *, workers: int = 1, budget=None, model=None, item_kwargs=None,
              git_sha="unknown", dirty=False, timeout_s: float = 600.0, progress=print) -> list[dict]:
    """Run plan items (approach, generation, scenario, variant, run number) and
    write one row each to out_dir/results.jsonl, from this process only.

    With workers > 1 each run is in a worker process, so each run has its own
    environment, server port, simulated clock and log paths; the parent is the
    only writer of the results file. A real-model batch (no model, no
    item_kwargs) refuses to start without a token budget (D6). `item_kwargs`
    (a picklable function of the item) supplies a test's model and scripted
    payments turns; a model object cannot be shared between processes."""
    out_dir = pathlib.Path(out_dir)
    items = list(items)
    real = model is None and item_kwargs is None and any(is_run(i) for i in items)
    if real and (budget is None or budget.max_tokens is None):
        raise budget_mod.BudgetRequired(
            "this batch calls the real model: pass --max-tokens (it will not start without one)")
    if workers > 1 and model is not None:
        raise ValueError("workers > 1 needs item_kwargs: a model object cannot be shared "
                         "between worker processes")
    out_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    not_run: list = []

    def record(row, item):
        _write_row(out_dir, row)
        rows.append(row)
        if budget is not None and row.get("status") not in ("n/a", "blocked"):
            budget.finish(row)
        progress(_describe(item, row))

    def placeholder(index, item):
        record(_run_item(index, item, out_dir, item_kwargs, git_sha, dirty, timeout_s, model), item)

    pending = [(i, it) for i, it in enumerate(items)]
    if workers <= 1:
        for index, item in pending:
            if not is_run(item):
                placeholder(index, item)
            elif budget is not None and not budget.allow_start():
                not_run.append(item)
            else:
                if budget is not None:
                    budget.start()
                record(_run_item(index, item, out_dir, item_kwargs, git_sha, dirty,
                                 timeout_s, model), item)
    else:
        ctx = multiprocessing.get_context("spawn")
        with concurrent.futures.ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
            running: dict = {}
            queue = list(pending)
            while queue or running:
                while queue and len(running) < workers:
                    index, item = queue[0]
                    if not is_run(item):
                        queue.pop(0)
                        placeholder(index, item)
                    elif budget is not None and not budget.allow_start():
                        not_run.extend(it for _, it in queue if is_run(it))
                        queue.clear()
                    else:
                        queue.pop(0)
                        if budget is not None:
                            budget.start()
                        fut = pool.submit(_run_item, index, item, out_dir, item_kwargs,
                                          git_sha, dirty, timeout_s)
                        running[fut] = item
                if running:
                    done, _ = concurrent.futures.wait(
                        running, return_when=concurrent.futures.FIRST_COMPLETED)
                    for fut in done:
                        record(fut.result(), running.pop(fut))
    if budget is not None:
        budget.write(out_dir, planned=sum(1 for it in items if is_run(it)), not_run=not_run)
    return rows


def run_batch(approach_ids, scenario_ids, runs: int, out_dir: pathlib.Path, *, gens=None,
              git_sha="unknown", dirty=False, model=None, workers: int = 1,
              budget=None, item_kwargs=None) -> list[dict]:
    return run_items(plan(approach_ids, scenario_ids, runs, gens), out_dir, workers=workers,
                     budget=budget, model=model, item_kwargs=item_kwargs,
                     git_sha=git_sha, dirty=dirty)
