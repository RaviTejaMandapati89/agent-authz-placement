"""
Tests for arms/b_spec tooling: make_kit and import_gen.
"""

import hashlib
import json
import pathlib
import shutil
import textwrap

import pytest

from arms.b_spec.make_kit import _check_forbidden, _copy_domain, _strip_arm_d

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_REPO_ROOT = pathlib.Path(__file__).parent.parent


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# make_kit: ARM D block stripping
# ---------------------------------------------------------------------------

def test_make_kit_strips_arm_d_blocks(tmp_path):
    """_copy_domain removes every ARM D block from domain/server.py."""
    src = _REPO_ROOT / "domain"
    dst = tmp_path / "domain"
    _copy_domain(src, dst)

    server_text = (dst / "server.py").read_text(encoding="utf-8")
    assert "# --- ARM D BEGIN ---" not in server_text
    assert "# --- ARM D END ---" not in server_text
    assert "cedar" not in server_text
    assert "d_boundary" not in server_text


def test_strip_arm_d_leaves_surrounding_code():
    """_strip_arm_d keeps lines before and after the block."""
    src = textwrap.dedent("""\
        line_before = 1
        # --- ARM D BEGIN ---
        arm_d_code = True
        # --- ARM D END ---
        line_after = 2
    """)
    result = _strip_arm_d(src)
    assert "line_before" in result
    assert "line_after" in result
    assert "arm_d_code" not in result
    assert "ARM D" not in result


# ---------------------------------------------------------------------------
# make_kit: forbidden-word check
# ---------------------------------------------------------------------------

def test_forbidden_word_check_clean_kit(tmp_path):
    """_check_forbidden returns no hits for a clean directory."""
    (tmp_path / "clean.py").write_text("x = 1\n", encoding="utf-8")
    assert _check_forbidden(tmp_path) == []


def test_forbidden_word_check_detects_planted_word(tmp_path):
    """_check_forbidden flags a file containing a forbidden word."""
    (tmp_path / "bad.py").write_text("# cedar policy\nx = 1\n", encoding="utf-8")
    hits = _check_forbidden(tmp_path)
    assert any("cedar" in h for h in hits)


def test_real_kit_passes_forbidden_word_check(tmp_path):
    """Build a full kit from the current repo files and assert no forbidden words leak in."""
    from arms.b_spec.make_kit import build_kit

    kit = tmp_path / "kit"
    build_kit(kit)
    claude_md = kit / "CLAUDE.md"
    assert claude_md.exists(), "CLAUDE.md missing from kit"
    assert "uv run pytest" in claude_md.read_text(encoding="utf-8")


def test_forbidden_word_check_all_words(tmp_path):
    """_check_forbidden reports every distinct forbidden word it finds."""
    forbidden_words = ["cedar", "d_boundary", "c_hook", "hook", "arms", "PREREG", "central_publisher"]
    content = "\n".join(f"x_{w} = '{w}'" for w in forbidden_words)
    (tmp_path / "multi.py").write_text(content, encoding="utf-8")
    hits = _check_forbidden(tmp_path)
    found = {h.split("contains ")[-1].strip("'") for h in hits}
    assert set(forbidden_words) <= found


# ---------------------------------------------------------------------------
# make_kit: allow list and gateway stripping
# ---------------------------------------------------------------------------

def test_kit_domain_excludes_gateway_and_policy_files(tmp_path):
    """Kit domain/ contains none of the gateway-side or policy files."""
    from arms.b_spec.make_kit import build_kit

    kit = tmp_path / "kit"
    build_kit(kit)

    for rel in (
        "domain/gateway.py",
        "domain/grants.py",
        "domain/central_service.py",
        "domain/central_policy.cedar",
        "domain/fingerprints.json",
    ):
        assert not (kit / rel).exists(), f"{rel} must not appear in the kit"


def test_kit_server_py_contains_no_gateway_references(tmp_path):
    """Kit domain/server.py contains none of the gateway-side strings."""
    from arms.b_spec.make_kit import build_kit

    kit = tmp_path / "kit"
    build_kit(kit)

    text = (kit / "domain" / "server.py").read_text(encoding="utf-8")
    for needle in ("/central/decide", "central_service", "grants", "gateway"):
        assert needle not in text, (
            f"kit/domain/server.py must not contain {needle!r}"
        )


def test_kit_server_non_gateway_mode_serves_mcp(tmp_path):
    """Kit server.py starts in non-gateway mode and responds 200 to /mcp."""
    import subprocess
    import sys
    from arms.b_spec.make_kit import build_kit

    kit = tmp_path / "kit"
    build_kit(kit)

    script = f"""
import sys
sys.path.insert(0, {str(kit)!r})
import asyncio, httpx
from domain.server import app, mcp

async def main():
    async with mcp._session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8765",
            headers={{"Host": "localhost:8765"}},
        ) as client:
            resp = await client.post(
                "/mcp",
                json={{
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {{
                        "protocolVersion": "2025-11-25",
                        "capabilities": {{}},
                        "clientInfo": {{"name": "test", "version": "0"}},
                    }},
                }},
                headers={{
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "MCP-Protocol-Version": "2025-11-25",
                }},
            )
            assert resp.status_code == 200, f"status={{resp.status_code}}"
            print("OK")

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"Kit server failed in non-gateway mode.\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "OK" in result.stdout


def test_kit_server_clock_advance_succeeds(tmp_path):
    """Kit domain/server.py must handle POST /control/clock/advance without error.

    The clock-advance route must NOT reference central_publisher (a gateway-only
    module absent from the kit).  If the GATEWAY BEGIN/END markers do not wrap the
    central_publisher calls, build_kit raises RuntimeError (word leak), and if they
    are somehow present without the markers the import fails at runtime.

    Fails right now because the central_publisher calls in the clock route sit
    outside the GATEWAY markers and therefore appear in the built kit.
    """
    import subprocess
    import sys
    from arms.b_spec.make_kit import build_kit

    kit = tmp_path / "kit"
    build_kit(kit)

    script = f"""
import sys
sys.path.insert(0, {str(kit)!r})
import asyncio, httpx
from domain.server import app, mcp

async def main():
    async with mcp._session_manager.run():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost:8765",
            headers={{"Host": "localhost:8765"}},
        ) as client:
            resp = await client.post(
                "/control/clock/advance",
                json={{"seconds": 60}},
                headers={{"Content-Type": "application/json"}},
            )
            assert resp.status_code == 200, f"status={{resp.status_code}} body={{resp.text[:200]}}"
            print("OK")

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, (
        f"Kit server clock advance failed.\n"
        f"STDOUT: {result.stdout}\nSTDERR: {result.stderr}"
    )
    assert "OK" in result.stdout


# ---------------------------------------------------------------------------
# import_gen: manifest matches file hashes; detects a changed file
# ---------------------------------------------------------------------------

def _build_minimal_kit(kit_root: pathlib.Path) -> None:
    """Create a minimal kit directory that import_gen can consume."""
    (kit_root / "domain").mkdir(parents=True)
    (kit_root / "domain" / "server.py").write_text("# generated\nx = 1\n", encoding="utf-8")
    (kit_root / "metadata.json").write_text('{"gen": 99}\n', encoding="utf-8")


def test_import_manifest_matches_hashes(tmp_path, monkeypatch):
    """manifest.json sha256 values match the actual files after import."""
    import json
    from arms.b_spec.import_gen import import_gen

    kit_root = tmp_path / "gvg-arm-b" / "gen-99"
    _build_minimal_kit(kit_root)

    gen_dst = tmp_path / "arms" / "b_spec" / "gen-99"
    # Redirect home() so the kit is found, and patch _B_SPEC so output lands in tmp.
    import arms.b_spec.import_gen as _ig
    monkeypatch.setattr(_ig, "_B_SPEC", tmp_path / "arms" / "b_spec")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    import_gen(99)

    manifest_path = gen_dst / "manifest.json"
    assert manifest_path.exists(), "manifest.json not written"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    for rel, recorded_digest in manifest.items():
        if not isinstance(recorded_digest, str) or len(recorded_digest) != 64:
            continue  # skip non-hash entries like seal_check
        actual = _sha256(gen_dst / rel)
        assert actual == recorded_digest, f"{rel}: manifest hash mismatch"


# ---------------------------------------------------------------------------
# seal_check: step log scanning
# ---------------------------------------------------------------------------

def _write_step_log(
    logs_dir: pathlib.Path,
    step: str,
    *,
    messages=None,
    permission_denials=None,
    tool_uses_with_ids=None,
) -> None:
    """Write a synthetic step log.

    messages: list of assistant message dicts (tool_use blocks without explicit ids).
    tool_uses_with_ids: list of (id, name, input_dict) — tool_use blocks with explicit ids.
        Pass matching ids in permission_denials to mark those calls as refused.
    permission_denials: list of dicts; use {"tool_use_id": <id>} to participate in
        id-based refusal matching.
    """
    logs_dir.mkdir(parents=True, exist_ok=True)
    events = []
    if messages is not None:
        for msg in messages:
            events.append({"type": "assistant", "message": msg})
    if tool_uses_with_ids is not None:
        content = [
            {"type": "tool_use", "id": uid, "name": name, "input": inp}
            for uid, name, inp in tool_uses_with_ids
        ]
        events.append({"type": "assistant", "message": {"content": content}})
    result: dict = {"type": "result", "subtype": "success", "cost_usd": 0.0, "usage": {}}
    if permission_denials is not None:
        result["permission_denials"] = permission_denials
    events.append(result)
    lines = "\n".join(json.dumps(e) for e in events) + "\n"
    (logs_dir / f"step-{step}.jsonl").write_text(lines, encoding="utf-8")


def test_seal_check_clean(tmp_path):
    """Step log with only kit-local calls returns status 'clean'."""
    from arms.b_spec.import_gen import seal_check

    kit = tmp_path / "kit"
    _write_step_log(kit / "logs", "implement", messages=[
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash",  "input": {"command": "uv run pytest"}},
            {"type": "tool_use", "name": "Write", "input": {"file_path": "domain/server.py"}},
        ]}
    ])
    assert seal_check(kit) == {"status": "clean"}


def test_seal_check_catches_leak_string(tmp_path):
    """A Bash command mentioning guides-vs-gates that executed is a breach."""
    from arms.b_spec.import_gen import seal_check

    kit = tmp_path / "kit"
    bad_cmd = "cat /Users/ravi/guides-vs-gates/domain/server.py"
    _write_step_log(kit / "logs", "implement", messages=[
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": bad_cmd}},
        ]}
    ])
    result = seal_check(kit)
    assert result["status"] == "breached"
    breaches = result["seal_breaches"]
    assert len(breaches) == 1
    assert breaches[0]["tool"] == "Bash"
    assert breaches[0]["command"] == bad_cmd


def test_seal_check_catches_absolute_path_outside_kit(tmp_path):
    """A Read call with an absolute path outside the kit root that executed is a breach."""
    from arms.b_spec.import_gen import seal_check

    kit = tmp_path / "kit"
    outside = str(tmp_path / "secret.py")  # under tmp_path, not under kit
    _write_step_log(kit / "logs", "specify", messages=[
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": outside}},
        ]}
    ])
    result = seal_check(kit)
    assert result["status"] == "breached"
    breaches = result["seal_breaches"]
    assert len(breaches) == 1
    assert breaches[0]["tool"] == "Read"
    assert breaches[0]["path"] == outside


def test_seal_check_absolute_path_inside_kit_is_clean(tmp_path):
    """A Read with an absolute path inside the kit is NOT flagged, even when the
    path and kit_root differ only in symlink resolution (e.g. /var vs /private/var
    on macOS)."""
    from arms.b_spec.import_gen import seal_check

    # kit_root is the RESOLVED form; file_path uses the UNRESOLVED form.
    # On macOS /var -> /private/var, so these strings will differ there.
    kit_unresolved = tmp_path / "kit"
    kit_resolved = kit_unresolved.resolve()
    inside = str(kit_unresolved / "spec_input.md")  # may not equal resolved form
    _write_step_log(kit_unresolved / "logs", "specify", messages=[
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": inside}},
        ]}
    ])
    # Pass resolved kit_root — old code would fail here if path forms diverge.
    assert seal_check(kit_resolved) == {"status": "clean"}


def test_seal_check_bash_abs_inside_kit_with_devnull_is_clean(tmp_path):
    """Bash using an absolute kit path plus 2>/dev/null is not flagged."""
    from arms.b_spec.import_gen import seal_check

    kit = tmp_path / "kit"
    inside = str(kit.resolve() / "domain" / "server.py")
    cmd = f"ls {inside} 2>/dev/null"
    _write_step_log(kit / "logs", "implement", messages=[
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": cmd}},
        ]}
    ])
    assert seal_check(kit.resolve()) == {"status": "clean"}


def test_seal_check_bash_abs_outside_kit_is_flagged(tmp_path):
    """A Bash command with an absolute path outside the kit root that executed is a breach."""
    from arms.b_spec.import_gen import seal_check

    kit = tmp_path / "kit"
    outside_dir = tmp_path / "other"
    outside_dir.mkdir()
    outside_file = outside_dir / "secret.py"
    outside_file.write_text("# secret\n", encoding="utf-8")
    outside = str(outside_file)
    cmd = f"cat {outside}"
    _write_step_log(kit / "logs", "implement", messages=[
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": cmd}},
        ]}
    ])
    result = seal_check(kit.resolve())
    assert result["status"] == "breached"
    breaches = result["seal_breaches"]
    assert len(breaches) == 1
    assert breaches[0]["tool"] == "Bash"
    assert breaches[0]["command"] == cmd


def test_seal_check_sed_tasks_command_is_clean(tmp_path):
    """sed -i '' 's/- \\[ \\]/- [X]/g' <kit>/...tasks.md is not flagged (tokens like /g are not real paths)."""
    from arms.b_spec.import_gen import seal_check

    kit = tmp_path / "kit"
    tasks_md = kit / "specs" / "001-feature" / "tasks.md"
    tasks_md.parent.mkdir(parents=True, exist_ok=True)
    tasks_md.write_text("- [ ] item\n", encoding="utf-8")
    cmd = f"sed -i '' 's/- \\[ \\]/- [X]/g' {tasks_md}"
    _write_step_log(kit / "logs", "implement", messages=[
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": cmd}},
        ]}
    ])
    assert seal_check(kit.resolve()) == {"status": "clean"}


def test_seal_check_bash_guides_vs_gates_is_flagged(tmp_path):
    """A Bash command mentioning guides-vs-gates that executed is a breach."""
    from arms.b_spec.import_gen import seal_check

    kit = tmp_path / "kit"
    cmd = "echo guides-vs-gates"
    _write_step_log(kit / "logs", "implement", messages=[
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": cmd}},
        ]}
    ])
    result = seal_check(kit)
    assert result["status"] == "breached"
    breaches = result["seal_breaches"]
    assert len(breaches) == 1
    assert breaches[0]["tool"] == "Bash"
    assert breaches[0]["command"] == cmd


def test_seal_check_catches_dotdot_escape(tmp_path):
    """A Write with a ../ path that executes and escapes the kit root is a breach."""
    from arms.b_spec.import_gen import seal_check

    kit = tmp_path / "kit"
    _write_step_log(kit / "logs", "implement", messages=[
        {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "../../outside.py"}},
        ]}
    ])
    result = seal_check(kit)
    assert result["status"] == "breached"
    breaches = result["seal_breaches"]
    assert len(breaches) == 1
    assert breaches[0]["tool"] == "Write"
    assert breaches[0]["path"] == "../../outside.py"


def test_seal_check_held_when_all_offences_refused(tmp_path):
    """tool_use with id matching permission_denials tool_use_id → refused → status 'held'."""
    from arms.b_spec.import_gen import seal_check

    kit = tmp_path / "kit"
    bad_cmd = "cat /Users/ravi/guides-vs-gates/domain/server.py"
    uid = "toolu_test_held_01"
    _write_step_log(kit / "logs", "implement",
        tool_uses_with_ids=[(uid, "Bash", {"command": bad_cmd})],
        permission_denials=[{"tool_use_id": uid}],
    )
    result = seal_check(kit)
    assert result["status"] == "held"
    attempts = result["seal_attempts"]
    assert len(attempts) == 1
    assert attempts[0]["tool"] == "Bash"
    assert attempts[0]["command"] == bad_cmd
    assert attempts[0]["tool_use_id"] == uid
    assert "seal_breaches" not in result


def test_seal_check_breached_when_offence_executed(tmp_path):
    """tool_use with no matching denial id → executed → status 'breached'."""
    from arms.b_spec.import_gen import seal_check

    kit = tmp_path / "kit"
    bad_cmd = "echo guides-vs-gates"
    _write_step_log(kit / "logs", "implement",
        tool_uses_with_ids=[("toolu_test_exec_01", "Bash", {"command": bad_cmd})],
    )
    result = seal_check(kit)
    assert result["status"] == "breached"
    assert "seal_breaches" in result
    assert "seal_attempts" not in result


def test_seal_check_breached_even_when_some_were_refused(tmp_path):
    """One offence refused (id in denials), one executed (no id match) → 'breached'; only executed in seal_breaches."""
    from arms.b_spec.import_gen import seal_check

    kit = tmp_path / "kit"
    refused_cmd = "cat /Users/ravi/guides-vs-gates/domain/server.py"
    executed_cmd = "echo guides-vs-gates"
    refused_uid = "toolu_test_refused_01"
    executed_uid = "toolu_test_executed_01"
    _write_step_log(kit / "logs", "implement",
        tool_uses_with_ids=[
            (refused_uid, "Bash", {"command": refused_cmd}),
            (executed_uid, "Bash", {"command": executed_cmd}),
        ],
        permission_denials=[{"tool_use_id": refused_uid}],
    )
    result = seal_check(kit)
    assert result["status"] == "breached"
    breaches = result["seal_breaches"]
    assert any(b["command"] == executed_cmd for b in breaches)
    assert all(b["command"] != refused_cmd for b in breaches)


def test_seal_check_same_id_in_tool_use_and_denial_is_held(tmp_path):
    """Same tool_use_id in assistant block and permission_denials → refused, not executed → 'held'."""
    from arms.b_spec.import_gen import seal_check

    kit = tmp_path / "kit"
    bad_cmd = "echo guides-vs-gates"
    uid = "toolu_test_same_id_01"
    _write_step_log(kit / "logs", "implement",
        tool_uses_with_ids=[(uid, "Bash", {"command": bad_cmd})],
        permission_denials=[{"tool_use_id": uid}],
    )
    result = seal_check(kit)
    assert result["status"] == "held"
    assert result["seal_attempts"][0]["tool_use_id"] == uid
    assert result["seal_attempts"][0]["command"] == bad_cmd


def test_import_manifest_detects_changed_file(tmp_path, monkeypatch):
    """Modifying an imported file makes its hash diverge from manifest.json."""
    import json
    from arms.b_spec.import_gen import import_gen

    kit_root = tmp_path / "gvg-arm-b" / "gen-98"
    _build_minimal_kit(kit_root)

    gen_dst = tmp_path / "arms" / "b_spec" / "gen-98"
    import arms.b_spec.import_gen as _ig
    monkeypatch.setattr(_ig, "_B_SPEC", tmp_path / "arms" / "b_spec")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    import_gen(98)

    manifest = json.loads((gen_dst / "manifest.json").read_text(encoding="utf-8"))

    # Tamper with the server file after import.
    server_path = gen_dst / "domain" / "server.py"
    server_path.write_text("# tampered\n", encoding="utf-8")

    changed = [
        rel for rel, digest in manifest.items()
        if isinstance(digest, str) and len(digest) == 64
        and _sha256(gen_dst / rel) != digest
    ]
    assert "domain/server.py" in changed


# ---------------------------------------------------------------------------
# import_gen: new file groups (specs/, logs/step-*.jsonl, tests/, CLAUDE.md,
#             spec_input.md) and --replace flag
# ---------------------------------------------------------------------------

def _build_full_kit(kit_root: pathlib.Path) -> None:
    """Kit with all the new file groups alongside the existing ones."""
    _build_minimal_kit(kit_root)

    # CLAUDE.md and spec_input.md at kit root
    (kit_root / "CLAUDE.md").write_text("# kit instructions\n", encoding="utf-8")
    (kit_root / "spec_input.md").write_text("## spec input\n", encoding="utf-8")

    # specs/ tree
    feature = kit_root / "specs" / "001-feature"
    feature.mkdir(parents=True)
    (feature / "spec.md").write_text("# spec\n", encoding="utf-8")
    (feature / "plan.md").write_text("# plan\n", encoding="utf-8")
    (feature / "tasks.md").write_text("# tasks\n", encoding="utf-8")
    (feature / "data-model.md").write_text("# data model\n", encoding="utf-8")
    (feature / "contracts").mkdir()
    (feature / "contracts" / "api.md").write_text("# contract\n", encoding="utf-8")
    (feature / "checklists").mkdir()
    (feature / "checklists" / "requirements.md").write_text("# checklist\n", encoding="utf-8")

    # logs/step-*.jsonl
    logs = kit_root / "logs"
    logs.mkdir()
    (logs / "step-specify.jsonl").write_text('{"type":"result"}\n', encoding="utf-8")
    (logs / "step-implement.jsonl").write_text('{"type":"result"}\n', encoding="utf-8")
    (logs / "other-log.txt").write_text("not matched\n", encoding="utf-8")  # must not be copied

    # tests/
    tests = kit_root / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("", encoding="utf-8")
    (tests / "test_contract.py").write_text("# agent tests\n", encoding="utf-8")


def test_import_copies_specs(tmp_path, monkeypatch):
    """specs/ tree is imported and every file appears in the manifest."""
    from arms.b_spec.import_gen import import_gen

    kit_root = tmp_path / "gvg-arm-b" / "gen-97"
    _build_full_kit(kit_root)
    gen_dst = tmp_path / "arms" / "b_spec" / "gen-97"

    import arms.b_spec.import_gen as _ig
    monkeypatch.setattr(_ig, "_B_SPEC", tmp_path / "arms" / "b_spec")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    import_gen(97)

    manifest = json.loads((gen_dst / "manifest.json").read_text(encoding="utf-8"))

    assert "specs/001-feature/spec.md" in manifest
    assert "specs/001-feature/plan.md" in manifest
    assert "specs/001-feature/tasks.md" in manifest
    assert "specs/001-feature/data-model.md" in manifest
    assert "specs/001-feature/contracts/api.md" in manifest
    assert "specs/001-feature/checklists/requirements.md" in manifest

    # Verify file was actually copied and hash matches
    for rel in ["specs/001-feature/spec.md", "specs/001-feature/contracts/api.md"]:
        assert _sha256(gen_dst / rel) == manifest[rel]


def test_import_copies_step_logs_only(tmp_path, monkeypatch):
    """logs/step-*.jsonl files are imported; other files in logs/ are not."""
    from arms.b_spec.import_gen import import_gen

    kit_root = tmp_path / "gvg-arm-b" / "gen-96"
    _build_full_kit(kit_root)
    gen_dst = tmp_path / "arms" / "b_spec" / "gen-96"

    import arms.b_spec.import_gen as _ig
    monkeypatch.setattr(_ig, "_B_SPEC", tmp_path / "arms" / "b_spec")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    import_gen(96)

    manifest = json.loads((gen_dst / "manifest.json").read_text(encoding="utf-8"))

    assert "logs/step-specify.jsonl" in manifest
    assert "logs/step-implement.jsonl" in manifest
    assert "logs/other-log.txt" not in manifest

    for rel in ["logs/step-specify.jsonl", "logs/step-implement.jsonl"]:
        assert _sha256(gen_dst / rel) == manifest[rel]


def test_import_copies_tests(tmp_path, monkeypatch):
    """tests/ directory is imported wholesale and appears in the manifest."""
    from arms.b_spec.import_gen import import_gen

    kit_root = tmp_path / "gvg-arm-b" / "gen-95"
    _build_full_kit(kit_root)
    gen_dst = tmp_path / "arms" / "b_spec" / "gen-95"

    import arms.b_spec.import_gen as _ig
    monkeypatch.setattr(_ig, "_B_SPEC", tmp_path / "arms" / "b_spec")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    import_gen(95)

    manifest = json.loads((gen_dst / "manifest.json").read_text(encoding="utf-8"))

    assert "tests/__init__.py" in manifest
    assert "tests/test_contract.py" in manifest
    assert _sha256(gen_dst / "tests" / "test_contract.py") == manifest["tests/test_contract.py"]


def test_import_copies_claude_md_and_spec_input(tmp_path, monkeypatch):
    """CLAUDE.md and spec_input.md are imported and recorded in the manifest."""
    from arms.b_spec.import_gen import import_gen

    kit_root = tmp_path / "gvg-arm-b" / "gen-94"
    _build_full_kit(kit_root)
    gen_dst = tmp_path / "arms" / "b_spec" / "gen-94"

    import arms.b_spec.import_gen as _ig
    monkeypatch.setattr(_ig, "_B_SPEC", tmp_path / "arms" / "b_spec")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    import_gen(94)

    manifest = json.loads((gen_dst / "manifest.json").read_text(encoding="utf-8"))

    assert "CLAUDE.md" in manifest
    assert "spec_input.md" in manifest
    assert _sha256(gen_dst / "CLAUDE.md") == manifest["CLAUDE.md"]
    assert _sha256(gen_dst / "spec_input.md") == manifest["spec_input.md"]


def test_import_refuses_existing_without_replace(tmp_path, monkeypatch):
    """import_gen exits non-zero when destination exists and --replace is not set."""
    from arms.b_spec.import_gen import import_gen

    kit_root = tmp_path / "gvg-arm-b" / "gen-93"
    _build_full_kit(kit_root)

    import arms.b_spec.import_gen as _ig
    monkeypatch.setattr(_ig, "_B_SPEC", tmp_path / "arms" / "b_spec")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    import_gen(93)

    # Second call without replace= should exit.
    with pytest.raises(SystemExit) as exc_info:
        import_gen(93)
    assert exc_info.value.code != 0


def test_import_excludes_pycache(tmp_path, monkeypatch):
    """__pycache__ dirs and *.pyc files are never copied or listed in the manifest."""
    from arms.b_spec.import_gen import import_gen

    kit_root = tmp_path / "gvg-arm-b" / "gen-91"
    _build_full_kit(kit_root)

    # Plant pycache artifacts that must be excluded.
    pycache = kit_root / "tests" / "__pycache__"
    pycache.mkdir()
    (pycache / "test_contract.cpython-312.pyc").write_bytes(b"\x00\x00\x00\x00")
    (kit_root / "tests" / "stray.pyc").write_bytes(b"\x00\x00\x00\x00")

    gen_dst = tmp_path / "arms" / "b_spec" / "gen-91"

    import arms.b_spec.import_gen as _ig
    monkeypatch.setattr(_ig, "_B_SPEC", tmp_path / "arms" / "b_spec")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    import_gen(91)

    manifest = json.loads((gen_dst / "manifest.json").read_text(encoding="utf-8"))

    for key in manifest:
        assert "__pycache__" not in key, f"pycache leaked into manifest: {key}"
        assert not key.endswith(".pyc"), f".pyc file leaked into manifest: {key}"

    assert not (gen_dst / "tests" / "__pycache__").exists()


def test_import_replace_flag_overwrites(tmp_path, monkeypatch):
    """import_gen with replace=True re-imports cleanly over an existing destination."""
    from arms.b_spec.import_gen import import_gen

    kit_root = tmp_path / "gvg-arm-b" / "gen-92"
    _build_full_kit(kit_root)
    gen_dst = tmp_path / "arms" / "b_spec" / "gen-92"

    import arms.b_spec.import_gen as _ig
    monkeypatch.setattr(_ig, "_B_SPEC", tmp_path / "arms" / "b_spec")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    import_gen(92)

    # Plant a stale file that should be gone after replace.
    stale = gen_dst / "stale_file.txt"
    stale.write_text("stale\n", encoding="utf-8")

    import_gen(92, replace=True)

    assert not stale.exists(), "stale file should have been removed by --replace"

    manifest = json.loads((gen_dst / "manifest.json").read_text(encoding="utf-8"))
    assert "CLAUDE.md" in manifest
    assert "specs/001-feature/spec.md" in manifest


# ---------------------------------------------------------------------------
# import_gen: full domain/ folder and pyproject.toml
# ---------------------------------------------------------------------------

def test_import_copies_full_domain_folder(tmp_path, monkeypatch):
    """All .py files in domain/ are copied — not just server.py."""
    from arms.b_spec.import_gen import import_gen

    kit_root = tmp_path / "gvg-arm-b" / "gen-89"
    (kit_root / "domain").mkdir(parents=True)
    (kit_root / "domain" / "server.py").write_text("# server\n", encoding="utf-8")
    (kit_root / "domain" / "identity.py").write_text("# identity\n", encoding="utf-8")
    (kit_root / "domain" / "state.py").write_text("# state\n", encoding="utf-8")
    (kit_root / "domain" / "__init__.py").write_text("", encoding="utf-8")
    (kit_root / "metadata.json").write_text('{"gen": 89}\n', encoding="utf-8")

    gen_dst = tmp_path / "arms" / "b_spec" / "gen-89"
    import arms.b_spec.import_gen as _ig
    monkeypatch.setattr(_ig, "_B_SPEC", tmp_path / "arms" / "b_spec")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    import_gen(89)

    manifest = json.loads((gen_dst / "manifest.json").read_text(encoding="utf-8"))

    for rel in ["domain/server.py", "domain/identity.py", "domain/state.py", "domain/__init__.py"]:
        assert rel in manifest, f"{rel} missing from manifest"
        assert (gen_dst / rel).exists(), f"{rel} not copied to disk"
        assert _sha256(gen_dst / rel) == manifest[rel], f"{rel} hash mismatch"


def test_import_copies_pyproject_toml(tmp_path, monkeypatch):
    """pyproject.toml is copied from the kit and recorded in the manifest."""
    from arms.b_spec.import_gen import import_gen

    kit_root = tmp_path / "gvg-arm-b" / "gen-88"
    (kit_root / "domain").mkdir(parents=True)
    (kit_root / "domain" / "server.py").write_text("# server\n", encoding="utf-8")
    (kit_root / "metadata.json").write_text('{"gen": 88}\n', encoding="utf-8")
    (kit_root / "pyproject.toml").write_text('[project]\nname = "test"\n', encoding="utf-8")

    gen_dst = tmp_path / "arms" / "b_spec" / "gen-88"
    import arms.b_spec.import_gen as _ig
    monkeypatch.setattr(_ig, "_B_SPEC", tmp_path / "arms" / "b_spec")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    import_gen(88)

    manifest = json.loads((gen_dst / "manifest.json").read_text(encoding="utf-8"))

    assert "pyproject.toml" in manifest
    assert (gen_dst / "pyproject.toml").exists()
    assert _sha256(gen_dst / "pyproject.toml") == manifest["pyproject.toml"]


# ---------------------------------------------------------------------------
# import_gen: changed_from_kit
# ---------------------------------------------------------------------------

def test_import_changed_from_kit_unchanged_not_listed(tmp_path, monkeypatch):
    """Pristine kit files (same hashes as build_kit output) are absent from changed_from_kit."""
    from arms.b_spec.import_gen import import_gen
    from arms.b_spec.make_kit import build_kit

    kit_root = tmp_path / "gvg-arm-b" / "gen-87"
    build_kit(kit_root)
    (kit_root / "metadata.json").write_text('{"gen": 87}\n', encoding="utf-8")

    gen_dst = tmp_path / "arms" / "b_spec" / "gen-87"
    import arms.b_spec.import_gen as _ig
    monkeypatch.setattr(_ig, "_B_SPEC", tmp_path / "arms" / "b_spec")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    import_gen(87)

    manifest = json.loads((gen_dst / "manifest.json").read_text(encoding="utf-8"))
    cfl = manifest["changed_from_kit"]

    pristine_kit_files = {
        "domain/server.py", "domain/__init__.py", "domain/identity.py",
        "domain/state.py", "domain/fixtures.py",
        "tests/__init__.py", "tests/conftest.py", "tests/test_contract.py",
        "pyproject.toml", "CLAUDE.md", "spec_input.md",
    }
    for f in pristine_kit_files:
        if f in manifest:
            assert f not in cfl, f"{f} is unchanged from kit but appears in changed_from_kit"


def test_import_changed_from_kit_modified_file_listed(tmp_path, monkeypatch):
    """A modified domain/server.py is in changed_from_kit; unchanged domain files are not."""
    from arms.b_spec.import_gen import import_gen
    from arms.b_spec.make_kit import build_kit

    kit_root = tmp_path / "gvg-arm-b" / "gen-86"
    build_kit(kit_root)
    (kit_root / "domain" / "server.py").write_text(
        "# MODIFIED BY AGENT\ndef check_rule(): raise PermissionError('no')\n",
        encoding="utf-8",
    )
    (kit_root / "metadata.json").write_text('{"gen": 86}\n', encoding="utf-8")

    gen_dst = tmp_path / "arms" / "b_spec" / "gen-86"
    import arms.b_spec.import_gen as _ig
    monkeypatch.setattr(_ig, "_B_SPEC", tmp_path / "arms" / "b_spec")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    import_gen(86)

    manifest = json.loads((gen_dst / "manifest.json").read_text(encoding="utf-8"))
    cfl = manifest["changed_from_kit"]

    assert "domain/server.py" in cfl, "modified server.py must be in changed_from_kit"
    for unchanged in ["domain/__init__.py", "domain/identity.py", "domain/state.py",
                      "pyproject.toml", "CLAUDE.md"]:
        if unchanged in manifest:
            assert unchanged not in cfl, f"{unchanged} was not modified; must not be in changed_from_kit"


def test_import_changed_from_kit_new_file_listed(tmp_path, monkeypatch):
    """A file added by the agent (not in build_kit output) appears in changed_from_kit."""
    from arms.b_spec.import_gen import import_gen
    from arms.b_spec.make_kit import build_kit

    kit_root = tmp_path / "gvg-arm-b" / "gen-85"
    build_kit(kit_root)
    feature = kit_root / "specs" / "001-inline-rule-enforcement"
    feature.mkdir(parents=True)
    (feature / "spec.md").write_text("# spec written by agent\n", encoding="utf-8")
    (kit_root / "metadata.json").write_text('{"gen": 85}\n', encoding="utf-8")

    gen_dst = tmp_path / "arms" / "b_spec" / "gen-85"
    import arms.b_spec.import_gen as _ig
    monkeypatch.setattr(_ig, "_B_SPEC", tmp_path / "arms" / "b_spec")
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))

    import_gen(85)

    manifest = json.loads((gen_dst / "manifest.json").read_text(encoding="utf-8"))
    cfl = manifest["changed_from_kit"]

    assert "specs/001-inline-rule-enforcement/spec.md" in cfl, "new spec file must be in changed_from_kit"
    for unchanged in ["domain/__init__.py", "pyproject.toml", "spec_input.md"]:
        if unchanged in manifest:
            assert unchanged not in cfl, f"{unchanged} was not modified; must not be in changed_from_kit"
