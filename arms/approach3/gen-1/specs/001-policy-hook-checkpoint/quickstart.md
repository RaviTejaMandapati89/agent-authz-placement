# Quickstart Validation Guide: PolicyHook Checkpoint Enforcement

**Feature**: `001-policy-hook-checkpoint`

## Prerequisites

- Python ≥ 3.11 with `uv` installed
- Project dependencies installed: `uv sync`
- `checkpoint.py` placed in the project root (alongside `domain/`, `config/`, `tests/`)

## Validation Scenarios

### 1. Contract Tests (Format Verification)

Run the provided contract tests to verify that `PolicyHook` exports the correct interface and produces correctly structured log entries:

```bash
uv run pytest tests/test_contract.py -v
```

**Expected outcome**: All 6 tests pass.

```
tests/test_contract.py::test_policyhook_class_exists PASSED
tests/test_contract.py::test_constructor_accepts_agent_name_user_base_url PASSED
tests/test_contract.py::test_constructor_accepts_optional_args PASSED
tests/test_contract.py::test_constructor_instantiates_with_config_dir PASSED
tests/test_contract.py::test_register_hooks_registers_before_tool_call_event PASSED
tests/test_contract.py::test_decision_log_entry_has_required_fields PASSED
tests/test_contract.py::test_decision_log_decision_field_is_string PASSED
```

### 2. Full Test Suite

```bash
uv run pytest -v
```

**Expected outcome**: All tests in `tests/` pass (including any generated tests in `tests/generated/`).

### 3. Golden Path — Allow Decision (Manual Smoke Test)

```python
import pathlib, json, os, tempfile
os.environ["HOOK_LOG"] = "/tmp/smoke_test.jsonl"

from checkpoint import PolicyHook

config_dir = pathlib.Path("config/agents")  # uses expense-assistant.yaml
hook = PolicyHook(
    agent_name="expense-assistant",
    user="alice",
    base_url="http://localhost:9999",   # any value; no directory call for this test
    run_id="smoke-001",
    bearer_token="",                     # will trigger SCOPE deny before P1-P5
    config_dir=config_dir,
)

class FakeEvent:
    tool_use = {"name": "read_receipt", "input": {"receipt_id": "rcpt-001"}, "toolUseId": "t-001"}
    selected_tool = None
    cancel_tool = None

hook._before_tool_call(FakeEvent())
entry = json.loads(open("/tmp/smoke_test.jsonl").readlines()[-1])
print(entry)
assert entry["tool"] == "read_receipt"
assert entry["decision"] in ("deny", "allow", "error")
assert entry["rule"] in ("SCOPE", "P1", "P2", "P3", "P4", "P5", "P6", "P7", None)
```

**Expected outcome**: Log entry written with all required fields; `decision` is `"deny"` with `rule="SCOPE"` because bearer_token is empty.

### 4. Runtime Config Reload (SC-004)

```python
import pathlib, tempfile, yaml
from checkpoint import PolicyHook

with tempfile.TemporaryDirectory() as d:
    config_dir = pathlib.Path(d)

    # Config A: allows submit_expense
    (config_dir / "agent-a.yaml").write_text(yaml.dump({
        "agent": "agent-a", "acts_for": "any",
        "allowed_tools": ["submit_expense"], "fingerprints": {}
    }))
    hook_a = PolicyHook("agent-a", "alice", "http://localhost:9999", config_dir=config_dir)

    # Config B: allows only read_receipt
    (config_dir / "agent-b.yaml").write_text(yaml.dump({
        "agent": "agent-b", "acts_for": "any",
        "allowed_tools": ["read_receipt"], "fingerprints": {}
    }))
    hook_b = PolicyHook("agent-b", "alice", "http://localhost:9999", config_dir=config_dir)

    assert "submit_expense" in hook_a._allowed_tools
    assert "submit_expense" not in hook_b._allowed_tools
    assert "read_receipt" in hook_b._allowed_tools
    print("SC-004 PASSED: different configs produce different enforcement")
```

### 5. Directory 5xx Propagation (SC-005)

Start a mock HTTP server that returns 500 for `/directory/users/alice`, then:

```python
from checkpoint import PolicyHook
import pathlib, tempfile, yaml, pytest

with tempfile.TemporaryDirectory() as d:
    config_dir = pathlib.Path(d)
    (config_dir / "payments-agent.yaml").write_text(yaml.dump({
        "agent": "payments-agent", "acts_for": "finance",
        "allowed_tools": ["pay_vendor"], "fingerprints": {}
    }))
    hook = PolicyHook("payments-agent", "erin", "http://localhost:PORT_THAT_RETURNS_500",
                      config_dir=config_dir, bearer_token="")

    class FakeEvent:
        tool_use = {"name": "pay_vendor", "input": {"vendor": "acme-hotels"}, "toolUseId": "t-500"}
        selected_tool = None
        cancel_tool = None

    # Must raise an httpx.HTTPStatusError or similar, not silently deny
    try:
        hook._before_tool_call(FakeEvent())
        print("FAIL: no exception raised")
    except Exception as e:
        print(f"SC-005 PASSED: exception propagated: {type(e).__name__}: {e}")
```

**Expected outcome**: An HTTP exception propagates; no log entry records an allow decision.

## References

- API contract: [`contracts/policyhook-api.md`](contracts/policyhook-api.md)
- Log entry schema: [`contracts/decision-log-schema.json`](contracts/decision-log-schema.json)
- Data model: [`data-model.md`](data-model.md)
- Spec: [`spec.md`](spec.md)
- Constitution: [`.specify/memory/constitution.md`](../../.specify/memory/constitution.md)
