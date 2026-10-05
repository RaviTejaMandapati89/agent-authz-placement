# Quickstart: Validating PolicyHook

This guide describes how to run and validate the `PolicyHook` implementation end-to-end.

## Prerequisites

- Python environment bootstrapped: `uv sync` or `.venv` already present.
- `checkpoint.py` exists at the repo root.
- Agent YAML configs exist in `config/agents/` (already present in the kit).
- The directory server is either live (for integration tests) or mocked (for unit tests).

## Running the Test Suite

```bash
uv run pytest
```

This runs both `tests/test_contract.py` (format/signature checks) and `tests/generated/` (policy-rule checks).

To run a specific test file:
```bash
uv run pytest tests/test_contract.py -v
uv run pytest tests/generated/ -v
```

---

## Validation Scenarios

For each scenario, set `HOOK_LOG` to a temp file and inspect its contents after the call.

### Allow Path (User Story 1)

**Setup**: `expense-assistant.yaml` with `allowed_tools: [submit_expense]` and a matching fingerprint.

```python
import os, json, tempfile, pathlib
from checkpoint import PolicyHook

with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as f:
    log_path = f.name
os.environ["HOOK_LOG"] = log_path

hook = PolicyHook(
    agent_name="expense-assistant",
    user="alice",
    base_url="http://localhost:8765",
    run_id="quickstart-001",
    bearer_token="<valid-JWT-with-expenses:submit-scope>",
    config_dir=pathlib.Path("config/agents"),
)

class FakeEvent:
    tool_use = {"name": "submit_expense", "input": {"claimant": "alice", "amount": 100.0, "description": "test"}}
    selected_tool = None
    cancel_tool = False

hook._before_tool_call(FakeEvent())

entry = json.loads(open(log_path).read().strip())
assert entry["decision"] == "allow"
assert entry["rule"] is None
```

**Expected**: One log line with `decision: "allow"`, `rule: null`.

---

### Deny Paths (User Story 2) — P1–P7 and SCOPE

For each deny path, run the hook with a minimal event that should trigger the rule and assert the log entry.

| Rule | Tool | Trigger condition |
|------|------|-------------------|
| P1 | `submit_expense` | `claimant` ≠ acting user |
| P2 | `submit_expense` | `amount > expense_limit` and no `approval_ref` |
| P3 | `approve_expense` | acting user is the claimant |
| P4 | `book_travel` | `traveller` ≠ user and no active delegation |
| P5 | `pay_vendor` | vendor not in approved list |
| P6 | any | `selected_tool` present and fingerprint mismatch |
| P7 | any | tool not in `allowed_tools`, or blank user |
| SCOPE | any | missing/malformed/wrong-scope bearer token |

See [contracts/policy-hook-api.md](contracts/policy-hook-api.md) for the exact log format expected.

---

### Bearer Token Scope (User Story 3)

Run with three variants:
1. `bearer_token=""` → expect `rule: "SCOPE"`
2. `bearer_token="not-a-jwt"` → expect `rule: "SCOPE"`
3. `bearer_token=<valid-JWT-missing-scope>` → expect `rule: "SCOPE"`

---

### Directory 5xx Propagation (User Story 4)

Mock the directory to return 503 on user/delegation/vendor/expense routes. Call `_before_tool_call` for a tool that requires a directory lookup (e.g. `book_travel` with a different traveller). Assert that `httpx.HTTPStatusError` is raised (no log entry written for the failing call).

```python
import httpx, pytest

with pytest.raises(httpx.HTTPStatusError):
    hook._before_tool_call(event_requiring_delegation_lookup)
```

---

### Decision Log Completeness (User Story 5)

Run the hook ten times with different outcomes and verify:
```python
lines = [json.loads(ln) for ln in open(log_path).read().splitlines() if ln.strip()]
assert len(lines) == 10
required = {"call_id", "run_id", "timestamp", "sim_time", "user", "agent",
            "tool", "arguments", "decision", "rule", "reason"}
for entry in lines:
    assert required.issubset(entry.keys())
```

---

### YAML Config Reload (User Story 6)

1. Create YAML A with `allowed_tools: [submit_expense]`. Instantiate hook. Verify `book_travel` → P7 deny.
2. Create YAML B adding `book_travel`. Reinstantiate hook. Verify `book_travel` passes P7.

```python
# YAML A
hook_a = PolicyHook("test-agent", "alice", base_url, config_dir=yaml_a_dir)
assert event_deny("book_travel", hook_a) is True  # P7

# YAML B  
hook_b = PolicyHook("test-agent", "alice", base_url, config_dir=yaml_b_dir)
assert event_deny("book_travel", hook_b) is False  # passes P7
```

---

## Inspecting Log Output

```bash
# View last decision
tail -n 1 "$HOOK_LOG" | python3 -m json.tool

# Count decisions by rule
python3 -c "
import json, sys, collections
counts = collections.Counter(
    json.loads(l)['rule'] for l in open(sys.argv[1]) if l.strip()
)
print(dict(counts))
" "$HOOK_LOG"
```

---

## Validating Against the Constitution

After running the full test suite, confirm:

| Check | Command | Expected |
|-------|---------|----------|
| No hard-coded policy values in source | `grep -n '\"alice\"\|\"bob\"\|\"acme\"\|500\|\"employee\"\|\"finance\"' checkpoint.py` | No matches |
| Check order enforced | `grep -n 'blank.*user\|allowed_tools\|acts_for\|fingerprint\|SCOPE\|P1\|P2\|P3\|P4\|P5' checkpoint.py` | Appears in spec order |
| All tests pass | `uv run pytest -v` | Zero failures |
