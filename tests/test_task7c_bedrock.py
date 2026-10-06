"""C17: the S11 generation call runs on Bedrock (scoped to that one call, no secret
in the repo or the logs), and a failed stream result makes generate.sh fail loudly.

generate.sh is run for real with a stand-in `claude` on PATH, so the model and
Bedrock are never called. Pair 0 (the pilot path) is used: it needs no frozen tag.
"""
import json
import os
import pathlib
import stat
import subprocess

REPO = pathlib.Path(__file__).parent.parent
SCRIPT = REPO / "arms" / "s11gen" / "generate.sh"
SECRET = "test-secret-value-must-never-appear"

_FAKE = """#!/usr/bin/env bash
if [[ "${1:-}" == "--version" ]]; then echo "9.9.9 (Claude Code)"; exit 0; fi
cat > /dev/null
echo 'permit(principal, action, resource);' > policy.cedar
echo "BEDROCK=${CLAUDE_CODE_USE_BEDROCK:-unset} REGION=${AWS_REGION:-unset}" >> "$FAKE_ENV_LOG"
echo '{"type":"system","subtype":"init"}'
echo '%s'
exit 0
"""


def _run(tmp_path, result_line):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(_FAKE % result_line)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    home = tmp_path / "home"
    home.mkdir()
    env = {**os.environ, "HOME": str(home), "PATH": f"{bindir}:{os.environ['PATH']}",
           "FAKE_ENV_LOG": str(tmp_path / "env.log"), "AWS_SECRET_ACCESS_KEY": SECRET}
    env.pop("CLAUDE_CODE_USE_BEDROCK", None)
    r = subprocess.run(["bash", str(SCRIPT), "0", "expenses"], capture_output=True,
                       text=True, timeout=120, env=env, cwd=REPO)
    return r, home / "s11-gen"


def test_generation_command_carries_the_bedrock_route_for_that_call_only():
    text = SCRIPT.read_text()
    call = text.index("| claude --print")
    scope = text.rfind("(", 0, call)
    assert "CLAUDE_CODE_USE_BEDROCK=1" in text[scope:call]       # set inside the call's own subshell
    assert "CLAUDE_CODE_USE_BEDROCK" not in text[:scope]          # never exported script-wide
    for secret_name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        assert secret_name not in text


def test_the_claude_call_sees_bedrock_and_the_region_and_no_secret_is_recorded(tmp_path):
    ok = '{"type":"result","subtype":"success","is_error":false,"result":"done"}'
    r, root = _run(tmp_path, ok)
    assert r.returncode == 0, r.stderr
    seen = (tmp_path / "env.log").read_text()
    assert seen.startswith("BEDROCK=1 REGION=") and "REGION=unset" not in seen
    meta = json.loads((root / "logs" / "expenses-0.meta.json").read_text())
    assert meta["provider"] == "bedrock" and meta["aws_region"]
    assert meta["aws_region"] in seen
    for path in (root / "logs").iterdir():
        assert SECRET not in path.read_text()
    assert SECRET not in r.stdout + r.stderr


def test_an_error_result_line_makes_generate_sh_exit_non_zero_with_a_clear_message(tmp_path):
    bad = '{"type":"result","subtype":"success","is_error":true,"result":"Model not supported"}'
    r, root = _run(tmp_path, bad)
    assert r.returncode != 0
    assert "result is an error" in r.stderr and "Model not supported" in r.stderr
    meta = json.loads((root / "logs" / "expenses-0.meta.json").read_text())
    assert meta["result_is_error"] is True


def test_a_stream_with_no_result_line_also_fails(tmp_path):
    r, _ = _run(tmp_path, '{"type":"assistant"}')
    assert r.returncode != 0 and "no result line" in r.stderr
