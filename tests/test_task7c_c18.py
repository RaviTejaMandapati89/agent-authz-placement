"""C18: the S11 generation call can write exactly policy.cedar, has only Read and Write,
and generate.sh fails loudly on a missing policy, a permission denial, a wrong model or
extra tools. generate.sh runs for real (pair 0) with a stand-in `claude`: no model, no Bedrock.
S11_GENERATE_SH points the tests at another copy of the script (used to show them failing
against the previous version)."""
import json
import os
import pathlib
import stat
import subprocess

REPO = pathlib.Path(__file__).parent.parent
SCRIPT = pathlib.Path(os.environ.get("S11_GENERATE_SH", REPO / "arms" / "s11gen" / "generate.sh"))
PINNED = "global.anthropic.claude-sonnet-4-6"

_FAKE = """#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then echo "9.9.9 (Claude Code)"; exit 0; fi
cat > /dev/null
printf '%s\\n' "$@" > "$FAKE_ARGS"
[[ -n "${FAKE_POLICY:-}" ]] && printf '%s' "$FAKE_POLICY" > policy.cedar
[[ -n "${FAKE_EMPTY_POLICY:-}" ]] && : > policy.cedar
cat "$FAKE_STREAM"
exit 0
"""


def _stream(tools=("Read", "Write"), reply_model="claude-sonnet-4-6", denials=(), is_error=False):
    lines = [
        {"type": "system", "subtype": "init", "model": PINNED, "tools": list(tools)},
        {"type": "assistant", "message": {"model": reply_model, "content": []}},
        {"type": "result", "subtype": "success", "is_error": is_error, "result": "done",
         "modelUsage": {PINNED: {}},
         "permission_denials": [{"tool_name": "Write", "tool_input": {"file_path": p}} for p in denials]},
    ]
    return "\n".join(json.dumps(x) for x in lines) + "\n"


def _run(tmp_path, stream, policy="permit(principal, action, resource);", empty=False):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(_FAKE)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "stream.jsonl").write_text(stream)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "PATH": f"{bindir}:{os.environ['PATH']}",
           "FAKE_ARGS": str(tmp_path / "args.txt"), "FAKE_STREAM": str(tmp_path / "stream.jsonl")}
    if empty:
        env["FAKE_EMPTY_POLICY"] = "1"
    elif policy:
        env["FAKE_POLICY"] = policy
    r = subprocess.run(["bash", str(SCRIPT), "0", "expenses"], capture_output=True, text=True,
                       timeout=120, env=env, cwd=REPO)
    meta_path = home / "s11-gen" / "logs" / "expenses-0.meta.json"
    return r, (json.loads(meta_path.read_text()) if meta_path.exists() else None)


def _args(tmp_path):
    return (tmp_path / "args.txt").read_text().splitlines()


def test_a_clean_run_succeeds_and_records_the_models_and_no_denials(tmp_path):
    r, meta = _run(tmp_path, _stream())
    assert r.returncode == 0, r.stderr
    assert meta["models_answered"] and meta["permission_denials"] == []
    assert meta["failures"] == []


def test_command_line_restricts_tools_to_read_write_and_writes_to_policy_cedar_only(tmp_path):
    r, _ = _run(tmp_path, _stream())
    assert r.returncode == 0, r.stderr
    a = _args(tmp_path)
    assert a[a.index("--tools") + 1] == "Read,Write"
    allowed = a[a.index("--allowedTools") + 1].split(",")
    assert sorted(allowed) == ["Edit(./policy.cedar)", "Read(./**)"]   # Edit rule covers the Write tool
    assert not any(x.startswith("Write(") for x in allowed)             # Write(path) is never consulted
    assert a[a.index("--permission-prompts") + 1] == "none"


def test_missing_policy_fails_with_a_message(tmp_path):
    r, meta = _run(tmp_path, _stream(), policy="")
    assert r.returncode != 0 and "FAILED" in r.stderr and "policy.cedar" in r.stderr
    assert meta is not None and meta["failures"]


def test_empty_policy_fails_with_a_message(tmp_path):
    r, _ = _run(tmp_path, _stream(), empty=True)
    assert r.returncode != 0 and "FAILED" in r.stderr and "policy.cedar" in r.stderr


def test_a_permission_denial_fails_and_is_recorded(tmp_path):
    r, meta = _run(tmp_path, _stream(denials=["/x/policy.cedar"]))
    assert r.returncode != 0 and "FAILED" in r.stderr and "permission denial" in r.stderr
    assert meta["permission_denials"] == [{"tool_name": "Write", "path": "/x/policy.cedar"}]


def test_a_reply_from_another_model_fails_and_is_recorded(tmp_path):
    r, meta = _run(tmp_path, _stream(reply_model="claude-haiku-4-5"))
    assert r.returncode != 0 and "FAILED" in r.stderr and "pinned" in r.stderr
    assert "claude-haiku-4-5" in meta["models_answered"]


def test_extra_tools_offered_to_the_model_fail(tmp_path):
    r, meta = _run(tmp_path, _stream(tools=("Read", "Write", "Bash")))
    assert r.returncode != 0 and "FAILED" in r.stderr and "Bash" in r.stderr
