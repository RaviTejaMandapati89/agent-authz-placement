"""
CLI runner.

Usage:
  uv run python -m runner.run --approach 5 --scenario S1 --runs 1
  uv run python -m runner.run --approach 4 --all
  uv run python -m runner.run --approach 3 --gen all --all
  uv run python -m runner.run --approach all --all

The functions below the CLI (run_one and its helpers) are the pilot-era runner,
kept for the tests that exercise them. The study runs through runner.study.
"""
import argparse
import contextlib
import datetime
import json
import os
import pathlib
import subprocess
import sys
import time
import uuid

import httpx
import yaml
from dotenv import load_dotenv

from runner import config as cfg
from runner._server import domain_server, free_port, wait_for_port
from runner.grader import _parse_ts, grade, merge_decision_log

load_dotenv()

_REPO_ROOT = pathlib.Path(__file__).parent.parent
_SCENARIOS_DIR = _REPO_ROOT / "scenarios"
_B_SPEC_DIR = _REPO_ROOT / "arms" / "b_spec"

_AGENT_SCOPES: dict[str, list[str]] = {
    "expense-assistant": ["expenses:read", "expenses:submit", "expenses:approve", "agents:payments"],
    "travel-assistant": ["expenses:submit", "travel:book", "agents:payments"],
    "payments-agent": ["expenses:approve", "payments:pay"],
}

_ARM_MODULES: dict[str, object] = {}


def _load_arm(arm: str) -> object:
    if arm not in _ARM_MODULES:
        if arm == "A":
            from arms.a_guides import agent as m
        elif arm == "B":
            from arms.b_spec import agent as m
        elif arm == "C":
            from arms.c_hook import agent as m
        elif arm == "D":
            from arms.d_boundary import agent as m
        elif arm == "3":
            from arms.approach3 import agent as m
        else:
            raise ValueError(f"arm {arm!r} not built")
        _ARM_MODULES[arm] = m
    return _ARM_MODULES[arm]


# ---------------------------------------------------------------------------
# arm B: gen-N server context and contract tests
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _gen_server(gen_dir: pathlib.Path, decision_log: str):
    """Start the gen-N server as a subprocess; yield (port, base_url)."""
    port = free_port()
    env = {
        **os.environ,
        "PYTHONPATH": str(gen_dir),
        "SERVER_PORT": str(port),
        "DECISION_LOG": decision_log,
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "domain.server"],
        env=env,
        cwd=str(gen_dir),
    )
    try:
        wait_for_port(port)
        base_url = f"http://127.0.0.1:{port}"
        yield port, base_url
    finally:
        proc.terminate()
        proc.wait()


def _run_contract_tests(gen_dir: pathlib.Path) -> bool:
    """Run contract tests for the gen-N server. Returns True on pass."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_contract.py", "-x", "-q",
         "--tb=short", "--no-header"],
        cwd=str(gen_dir),
        env={**os.environ, "PYTHONPATH": str(gen_dir)},
    )
    return result.returncode == 0


def _list_gen_dirs() -> list[int]:
    """Return sorted list of available gen numbers in arms/b_spec/."""
    nums = []
    for p in sorted(_B_SPEC_DIR.iterdir()):
        if p.is_dir() and p.name.startswith("gen-"):
            try:
                nums.append(int(p.name[4:]))
            except ValueError:
                pass
    return nums


_LEGACY_ARM_APPROACH = {"A": 1, "C": 2, "3": 3}


def _legacy_applies(scenario: dict, arm: str) -> bool:
    """Applicability is read from the scenario file's `approaches` list; a
    scenario that lists none applies to every arm (as before)."""
    listed = scenario.get("approaches")
    if listed is None:
        return True
    return _LEGACY_ARM_APPROACH.get(arm) in listed


def _load_scenario(scenario_id: str) -> dict:
    path = _SCENARIOS_DIR / f"{scenario_id}.yaml"
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _list_scenarios() -> list[str]:
    return sorted(p.stem for p in _SCENARIOS_DIR.glob("*.yaml"))


def _apply_control(base_url: str, step: dict) -> None:
    action = step["action"]
    args = step.get("args", {})
    endpoint = f"{base_url}/control/{action}"
    resp = httpx.post(endpoint, json=args)
    resp.raise_for_status()


def _app_expense_call(base_url: str, user: str, args: dict) -> dict:
    """Send one expense through the non-agent app channel (POST /app/expenses)
    with a plain user token for the app audience. Returns status and body."""
    r = httpx.post(f"{base_url}/control/identity/user-token", json={
        "sub": user, "aud": "expenses-app", "scope": ["expenses:submit"],
    })
    r.raise_for_status()
    resp = httpx.post(
        f"{base_url}/app/expenses", json=args,
        headers={"Authorization": f"Bearer {r.json()['access_token']}"},
    )
    return {"status": resp.status_code, "body": resp.json()}


def _apply_step(base_url: str, user: str, step: dict) -> None:
    """A between-turns step is either a control action or an app-channel
    request (`channel: app`, with `args`)."""
    if step.get("channel") == "app":
        _app_expense_call(base_url, step.get("user", user), step.get("args", {}))
    else:
        _apply_control(base_url, step)


def _apply_policy_change_server(base_url: str, change: dict) -> None:
    if "expense_limit" in change:
        resp = httpx.post(
            f"{base_url}/control/set-limit",
            json={"limit": change["expense_limit"]},
        )
        resp.raise_for_status()


def _build_transcript(turns_data: list[dict], merged_calls: list[dict]) -> list[dict]:
    """Assign merged tool calls to turns by timestamp; return per-turn transcript entries."""
    transcript = []
    for i, t in enumerate(turns_data):
        try:
            ts_start = datetime.datetime.fromisoformat(t["ts_start"])
            ts_end = datetime.datetime.fromisoformat(t["ts_end"])
        except (KeyError, ValueError):
            ts_start = ts_end = None

        turn_calls = []
        if ts_start is not None:
            for call in merged_calls:
                raw_ts = call.get("timestamp")
                if raw_ts:
                    normalized = raw_ts if not raw_ts.endswith("Z") else raw_ts[:-1] + "+00:00"
                    try:
                        call_ts = datetime.datetime.fromisoformat(normalized)
                        if ts_start <= call_ts <= ts_end:
                            turn_calls.append({
                                "tool": call.get("tool"),
                                "arguments": call.get("arguments"),
                                "executed": call.get("executed"),
                            })
                    except ValueError:
                        pass

        transcript.append({
            "turn": i + 1,
            "user_message": t["user_message"],
            "reply": t["reply"],
            "tool_calls": turn_calls,
        })
    return transcript


def _last_hook_line_is_error(path: pathlib.Path, run_id: str) -> dict | None:
    """Return the last hook log entry for run_id if its decision is 'error', else None."""
    if not path.exists():
        return None
    last_for_run = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            entry = json.loads(raw)
            if entry.get("run_id") == run_id:
                last_for_run = entry
        except json.JSONDecodeError:
            pass
    if last_for_run is not None and last_for_run.get("decision") == "error":
        return last_for_run
    return None


def _detect_fail_mode(
    error_ts: str,
    merged_calls: list[dict],
    ledger: list[dict],
) -> str:
    """Return 'open' if a tool executed (or a ledger entry exists) after the hook
    error timestamp; 'closed' otherwise."""
    try:
        cutoff = _parse_ts(error_ts)
    except ValueError:
        return "closed"

    for call in merged_calls:
        if call.get("executed") is True:
            try:
                if _parse_ts(call.get("timestamp")) > cutoff:
                    return "open"
            except ValueError:
                pass

    for entry in ledger:
        try:
            if _parse_ts(entry.get("timestamp")) > cutoff:
                return "open"
        except ValueError:
            pass

    return "closed"


def _count_hook_decisions(path: pathlib.Path, run_id: str) -> int:
    """Count hook_decisions.jsonl lines that belong to this run_id."""
    if not path.exists():
        return 0
    count = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw:
            try:
                if json.loads(raw).get("run_id") == run_id:
                    count += 1
            except json.JSONDecodeError:
                pass
    return count


def _read_decision_log(path: pathlib.Path) -> list[dict]:
    if not path.exists():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if raw:
            lines.append(json.loads(raw))
    return lines


def _direct_mcp_call(mcp_url: str, tool: str, args: dict, headers: dict) -> dict:
    """Make one MCP tool call without an agent, using raw JSON-RPC over HTTP."""
    from urllib.parse import urlparse
    parsed = urlparse(mcp_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    mcp_path = parsed.path if parsed.path and parsed.path != "/" else "/mcp"

    common = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2025-11-25",
        **headers,
    }
    with httpx.Client(base_url=base_url, timeout=30.0) as client:
        # Initialise session
        r = client.post(
            mcp_path,
            json={
                "jsonrpc": "2.0", "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "runner-direct", "version": "0"},
                },
            },
            headers=common,
        )
        r.raise_for_status()
        session_id = r.headers.get("mcp-session-id")
        call_headers = {**common}
        if session_id:
            call_headers["mcp-session-id"] = session_id

        # Call tool
        r = client.post(
            mcp_path,
            json={
                "jsonrpc": "2.0", "id": 2,
                "method": "tools/call",
                "params": {"name": tool, "arguments": args},
            },
            headers=call_headers,
        )
        r.raise_for_status()
        body = r.json()
        return body.get("result", body)


def _git_info() -> tuple[str, bool]:
    """Return (commit_sha, is_dirty). Falls back to 'unknown'/False on failure."""
    try:
        sha = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=_REPO_ROOT,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        dirty_output = subprocess.check_output(
            ["git", "status", "--porcelain"],
            cwd=_REPO_ROOT,
            stderr=subprocess.DEVNULL,
        ).decode().strip()
        return sha, bool(dirty_output)
    except Exception:
        return "unknown", False


def run_one(
    arm: str,
    scenario_id: str,
    run_num: int,
    base_url: str,
    decision_log_path: pathlib.Path,
    runs_out: pathlib.Path,
    git_sha: str,
    dirty: bool,
    gen: int | None = None,
    gateway: bool = False,
) -> str:
    """Run one scenario and append a row to runs_out. Returns the run status."""
    scenario = _load_scenario(scenario_id)
    run_id = str(uuid.uuid4())
    mcp_url = f"{base_url}/gateway/mcp" if gateway else f"{base_url}/mcp"

    # Load the arm module first so its policy state can always be reset, even
    # for n/a scenarios. Without this, a policy change from a previous scenario
    # would persist across the n/a early-return into the next scenario's run.
    try:
        arm_mod = _load_arm(arm)
        arm_mod.apply_policy_change({})
    except Exception as exc:
        record = {
            "run_id": run_id,
            "arm": arm,
            "gen": gen,
            "scenario": scenario_id,
            "run_num": run_num,
            "status": "error",
            "violated": None,
            "legitimate_completed": None,
            "input_tokens": None,
            "output_tokens": None,
            "duration_s": None,
            "model_id": cfg.MODEL_ID,
            "strands_version": cfg.STRANDS_VERSION,
            "mcp_version": cfg.MCP_VERSION,
            "git_sha": git_sha,
            "dirty": dirty,
            "final_reply": "",
            "error": str(exc),
            "agent_aborted": False,
            "abort_reason": None,
            "fail_mode": None,
            "ledger": [],
            "transcript": None,
            "hook_decisions_count": None,
        }
        with runs_out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        print(f"  {scenario_id} run {run_num}: ERROR  {exc}")
        return "error"

    # Check if this arm applies to this scenario
    if not _legacy_applies(scenario, arm):
        record = {
            "run_id": run_id,
            "arm": arm,
            "gen": gen,
            "scenario": scenario_id,
            "run_num": run_num,
            "status": "n/a",
            "violated": None,
            "legitimate_completed": None,
            "input_tokens": None,
            "output_tokens": None,
            "duration_s": None,
            "model_id": cfg.MODEL_ID,
            "strands_version": cfg.STRANDS_VERSION,
            "mcp_version": cfg.MCP_VERSION,
            "git_sha": git_sha,
            "dirty": dirty,
            "final_reply": None,
            "error": None,
            "ledger": [],
            "transcript": None,
            "hook_decisions_count": None,
        }
        with runs_out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        print(f"  {scenario_id} run {run_num}: n/a (arm {arm} not applicable)")
        return "n/a"

    # 1. Reset
    httpx.post(f"{base_url}/control/reset").raise_for_status()

    # 2. Set run context
    httpx.post(
        f"{base_url}/control/set-run",
        json={"run_id": run_id, "scenario": scenario_id, "arm": arm},
    ).raise_for_status()

    # 3. Apply setup
    for step in scenario.get("setup") or []:
        _apply_control(base_url, step)

    # 4. Apply policy change (arm already reset to {} above)
    policy_change = scenario.get("policy_change") or {}
    if policy_change:
        _apply_policy_change_server(base_url, policy_change)
        if arm == "D" and "expense_limit" in policy_change:
            httpx.post(
                f"{base_url}/control/pdp-policy-change",
                json=policy_change,
            ).raise_for_status()
        arm_mod.apply_policy_change(policy_change)

    # Obtain identity tokens for the agent
    agent_name = scenario["agent"]
    user = scenario["user"]
    agent_scopes = _AGENT_SCOPES.get(agent_name, [])
    bearer_token = ""
    bearer_claims: dict | None = None
    try:
        r = httpx.post(f"{base_url}/control/identity/user-token", json={
            "sub": user, "aud": agent_name, "scope": agent_scopes,
        })
        r.raise_for_status()
        user_token = r.json()["access_token"]

        r = httpx.post(f"{base_url}/control/identity/agent-token", json={
            "sub": agent_name, "scope": agent_scopes,
        })
        r.raise_for_status()
        agent_token = r.json()["access_token"]

        r = httpx.post(f"{base_url}/identity/exchange", json={
            "subject_token": user_token, "actor_token": agent_token,
        })
        r.raise_for_status()
        bearer_token = r.json()["access_token"]

        try:
            import jwt as _jwt
            bearer_claims = _jwt.decode(bearer_token, options={"verify_signature": False})
        except Exception:
            bearer_claims = None
    except Exception:
        pass

    start_ts = time.monotonic()
    final_reply = ""
    input_tokens = None
    output_tokens = None
    between_turns_ts: str | None = None
    error_msg: str | None = None
    agent_aborted = False
    abort_reason: str | None = None
    fail_mode: str | None = None
    turns_data: list[dict] = []

    try:
        if scenario.get("direct"):
            # S9: scripted direct call, no agent
            direct = scenario["direct"]
            _direct_mcp_call(
                base_url,
                tool=direct["tool"],
                args=direct.get("args", {}),
                headers=direct.get("headers", {}),
            )
            final_reply = "(direct call)"
        else:
            # Normal agent run
            turns = scenario.get("turns") or []
            between_turn_steps = scenario.get("between_turns") or []

            def do_between_turns() -> None:
                nonlocal between_turns_ts
                between_turns_ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
                for step in between_turn_steps:
                    _apply_step(base_url, user, step)

            # S8 fault: bring the appropriate decision point down before the agent starts.
            fault = scenario.get("fault") or {}
            if fault.get("type") == "unavailable":
                if arm == "C":
                    httpx.post(f"{base_url}/control/directory-down", json={"down": True}).raise_for_status()
                elif arm == "D":
                    httpx.post(f"{base_url}/control/pdp-down", json={"down": True}).raise_for_status()

            reply, usage, turns_data = arm_mod.run(
                agent_name=agent_name,
                user=user,
                turns=turns,
                mcp_url=mcp_url,
                run_id=run_id,
                between_turns_fn=do_between_turns if between_turn_steps else None,
                bearer_token=bearer_token,
            )
            final_reply = reply
            input_tokens = usage.get("input_tokens")
            output_tokens = usage.get("output_tokens")

    except Exception as exc:
        error_msg = str(exc)

    duration_s = round(time.monotonic() - start_ts, 3)

    # 5. Read ledger
    ledger = httpx.get(f"{base_url}/control/ledger").json()

    # Distinguish hook-originated aborts from infrastructure failures.
    # Only treat as hook-originated when the LAST hook log line for this run_id
    # has decision="error"; a later non-error line means the abort was not the
    # hook's final act and should stay as status="error".
    hook_error_entry: dict | None = None
    if error_msg is not None:
        _hook_log = pathlib.Path(os.environ.get("HOOK_LOG", ""))
        if _hook_log.name:
            hook_error_entry = _last_hook_line_is_error(_hook_log, run_id)
            if hook_error_entry is not None:
                agent_aborted = True
                abort_reason = error_msg
                error_msg = None  # allow grading to run

    # 6+7. Merge decision log and grade — only when no error occurred
    merged_calls: list[dict] = []
    violated = None
    legitimate_completed = None
    if error_msg is None:
        try:
            log_lines = _read_decision_log(decision_log_path)
            merged_calls = merge_decision_log(log_lines, run_id)
            combined_events = ledger + merged_calls
            result = grade(scenario, combined_events, between_turns_ts)
            violated = result["violated"]
            legitimate_completed = result["legitimate_completed"]
        except Exception as exc:
            error_msg = str(exc)

    # fail_mode is computed here where merged_calls is available.
    if agent_aborted and hook_error_entry is not None:
        fail_mode = _detect_fail_mode(
            hook_error_entry.get("timestamp", ""),
            merged_calls,
            ledger,
        )

    transcript = _build_transcript(turns_data, merged_calls)

    hook_decisions_count: int | None = None
    if arm == "C":
        hook_log = pathlib.Path(os.environ.get("HOOK_LOG", ""))
        hook_decisions_count = _count_hook_decisions(hook_log, run_id) if hook_log.name else None

    status = "error" if error_msg is not None else "ok"

    record = {
        "run_id": run_id,
        "arm": arm,
        "gen": gen,
        "scenario": scenario_id,
        "run_num": run_num,
        "status": status,
        "violated": violated,
        "legitimate_completed": legitimate_completed,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_s": duration_s,
        "model_id": cfg.MODEL_ID,
        "strands_version": cfg.STRANDS_VERSION,
        "mcp_version": cfg.MCP_VERSION,
        "git_sha": git_sha,
        "dirty": dirty,
        "final_reply": final_reply,
        "error": error_msg,
        "agent_aborted": agent_aborted,
        "abort_reason": abort_reason,
        "fail_mode": fail_mode,
        "ledger": ledger,
        "transcript": transcript,
        "hook_decisions_count": hook_decisions_count,
        "identity_claims": bearer_claims,
    }
    with runs_out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")

    if error_msg is not None:
        print(f"  {scenario_id} run {run_num}: ERROR  {error_msg}")
    elif agent_aborted:
        v = "VIOLATION" if violated else "ok"
        print(
            f"  {scenario_id} run {run_num}: ABORTED(hook)  {v}"
            f"  fail_mode={fail_mode}  legitimate={legitimate_completed}"
            f"  {duration_s}s"
        )
    else:
        v = "VIOLATION" if violated else "ok"
        l_str = str(legitimate_completed)
        print(
            f"  {scenario_id} run {run_num}: {v}  legitimate={l_str}"
            f"  tokens={input_tokens}/{output_tokens}  {duration_s}s"
        )

    return status


def _run_arm_b_gen(
    gen_n: int,
    scenarios: list[str],
    runs_per_scenario: int,
    batch_dir: pathlib.Path,
    git_sha: str,
    dirty: bool,
) -> bool:
    """Run all scenarios for one arm B generation. Returns True if any run errored."""
    gen_dir = _B_SPEC_DIR / f"gen-{gen_n}"
    if not gen_dir.exists():
        print(f"gen-{gen_n}: directory not found at {gen_dir}")
        return True

    gen_batch_dir = batch_dir / f"gen-{gen_n}"
    gen_batch_dir.mkdir(parents=True, exist_ok=True)
    runs_out      = gen_batch_dir / "runs.jsonl"
    decision_log  = str(gen_batch_dir / "decisions.jsonl")

    print(f"\nGen-{gen_n}: running contract tests...")
    contract_ok = _run_contract_tests(gen_dir)
    if not contract_ok:
        print(f"Gen-{gen_n}: contract tests FAILED — recording all runs as failed_integration")
        for scenario_id in scenarios:
            scenario = _load_scenario(scenario_id)
            if not _legacy_applies(scenario, "B"):
                status_val = "n/a"
            else:
                status_val = "failed_integration"
            for run_num in range(1, runs_per_scenario + 1):
                record = {
                    "run_id":   str(uuid.uuid4()),
                    "arm":      "B",
                    "gen":      gen_n,
                    "scenario": scenario_id,
                    "run_num":  run_num,
                    "status":   status_val,
                    "violated": None,
                    "legitimate_completed": None,
                    "input_tokens": None,
                    "output_tokens": None,
                    "duration_s": None,
                    "model_id": cfg.MODEL_ID,
                    "git_sha": git_sha,
                    "dirty": dirty,
                    "final_reply": None,
                    "error": "contract tests failed",
                    "ledger": [],
                    "transcript": None,
                }
                with runs_out.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record) + "\n")
        print(f"Gen-{gen_n}: results: {runs_out}")
        return True

    print(f"Gen-{gen_n}: contract tests passed")

    any_error = False
    with _gen_server(gen_dir, decision_log) as (port, base_url):
        print(f"Gen-{gen_n}: server: {base_url}")
        for scenario_id in scenarios:
            print(f"  Scenario {scenario_id}:")
            for run_num in range(1, runs_per_scenario + 1):
                status = run_one(
                    arm="B",
                    scenario_id=scenario_id,
                    run_num=run_num,
                    base_url=base_url,
                    decision_log_path=pathlib.Path(decision_log),
                    runs_out=runs_out,
                    git_sha=git_sha,
                    dirty=dirty,
                    gen=gen_n,
                )
                if status == "error":
                    any_error = True

    print(f"Gen-{gen_n}: results: {runs_out}")
    return any_error


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the study's scenarios")
    parser.add_argument("--approach", required=True,
                        help="Approach 1 to 6 (labels as DESIGN.md section 3), or 'all'")
    parser.add_argument("--gen", default=None,
                        help="Approach 3 only: generation 1 to 5, or 'all' (default all)")
    parser.add_argument("--scenario", help="Scenario ID, e.g. S1")
    parser.add_argument("--runs", type=int, default=10, help="Runs per scenario")
    parser.add_argument("--all", dest="all_scenarios", action="store_true")
    parser.add_argument("--label", default="dev", help="Batch label suffix")
    parser.add_argument("--out", default=None,
                        help="Results directory (default results/<timestamp>_<label>)")
    args = parser.parse_args()

    if not args.scenario and not args.all_scenarios:
        parser.error("pass --scenario S1 or --all")

    from runner import approaches as ap
    from runner import scenarios as sc
    from runner import study
    from runner.summary import write_summary

    if args.approach == "all":
        approach_ids = list(ap.IDS)
    else:
        try:
            approach_ids = [int(args.approach)]
        except ValueError:
            parser.error("--approach must be 1 to 6 or 'all'")
        if approach_ids[0] not in ap.IDS:
            parser.error(f"--approach must be one of {ap.IDS}; pilot arms are not approaches")
    gens = None
    if args.gen and args.gen != "all":
        try:
            gens = [int(args.gen)]
        except ValueError:
            parser.error("--gen must be 1 to 5 or 'all'")

    scenario_ids = sc.ids() if args.all_scenarios else [args.scenario]
    ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
    batch_dir = (pathlib.Path(args.out) if args.out
                 else _REPO_ROOT / "results" / f"{ts}_{args.label}")
    batch_dir.mkdir(parents=True, exist_ok=True)
    git_sha, dirty = _git_info()
    print(f"Batch: {batch_dir.name}  commit: {git_sha}{'  (dirty)' if dirty else ''}")

    rows = study.run_batch(approach_ids, scenario_ids, args.runs, batch_dir,
                           gens=gens, git_sha=git_sha, dirty=dirty)
    write_summary(rows, batch_dir)
    print(f"\nResults: {batch_dir / 'results.jsonl'}\nSummary: {batch_dir / 'summary.md'}")
    if any(r["status"] == "error" for r in rows):
        print("ERROR: some runs failed outside the approach; they are recorded with their cause")
        sys.exit(1)


if __name__ == "__main__":
    main()
