# Quickstart Validation Guide: PolicyHook Checkpoint

**Branch**: `001-policy-hook-checkpoint` | **Date**: 2026-10-05

This guide describes how to validate that the implemented `checkpoint.py` works end-to-end.
It covers prerequisites, setup, and test scenarios for each policy rule.

---

## Prerequisites

- Python 3.11+ with `uv` installed
- All dependencies installed: `uv sync`
- `checkpoint.py` present at the project root

---

## Setup

1. **Install dependencies**:
   ```bash
   uv sync
   ```

2. **Run the existing contract tests** to verify format compliance:
   ```bash
   uv run pytest tests/test_contract.py -v
   ```
   Expected: all 6 tests pass.

3. **Run all tests** (including generated tests if present):
   ```bash
   uv run pytest
   ```

---

## Validation Scenarios

Each scenario below can be executed as a standalone Python snippet or as a pytest test.
All snippets assume the directory service is running on `http://127.0.0.1:8765` and the
`HOOK_LOG` environment variable is set.

### Setup helpers (used across scenarios)

```python
import json, os, pathlib, tempfile
from checkpoint import PolicyHook

def write_config(config_dir, agent_name="test-agent", **overrides):
    cfg = {
        "agent": agent_name,
        "acts_for": "any",
        "allowed_tools": ["submit_expense"],
        "fingerprints": {},
        **overrides,
    }
    import yaml
    (config_dir / f"{agent_name}.yaml").write_text(yaml.dump(cfg))

class FakeEvent:
    def __init__(self, tool_name, args=None, selected_tool=None):
        self.tool_use = {"name": tool_name, "input": args or {}}
        self.selected_tool = selected_tool
        self.cancel_tool = False
```

---

### S-01: P7 — Tool not in allowed_tools

```python
with tempfile.TemporaryDirectory() as d:
    config_dir = pathlib.Path(d)
    write_config(config_dir, allowed_tools=[])  # nothing allowed
    hook = PolicyHook("test-agent", "alice", "http://127.0.0.1:8765", config_dir=config_dir)

    event = FakeEvent("submit_expense", {"claimant": "alice", "amount": 50, "description": "x"})
    hook._before_tool_call(event)
    assert event.cancel_tool, "should be denied"
    # Read log — last entry should have decision=deny, rule=P7
```

**Expected**: `event.cancel_tool` is a non-empty string; log entry has `decision="deny"`, `rule="P7"`.

---

### S-02: P7 — Blank user guard

```python
with tempfile.TemporaryDirectory() as d:
    config_dir = pathlib.Path(d)
    write_config(config_dir, allowed_tools=["submit_expense"])
    hook = PolicyHook("test-agent", "   ", "http://127.0.0.1:8765", config_dir=config_dir)

    event = FakeEvent("submit_expense", {"claimant": "alice", "amount": 50, "description": "x"})
    hook._before_tool_call(event)
    assert event.cancel_tool
    # Log entry: decision=deny, rule=P7
```

---

### S-03: P1 — Claimant mismatch

```python
# Requires: submit_expense in allowed_tools; valid bearer token with expenses:submit scope;
#           no fingerprint check (fingerprints: {})
hook = PolicyHook("test-agent", "alice", "http://127.0.0.1:8765",
                  bearer_token=VALID_TOKEN, config_dir=config_dir)
event = FakeEvent("submit_expense", {"claimant": "bob", "amount": 50, "description": "lunch"})
hook._before_tool_call(event)
assert event.cancel_tool
# Log entry: decision=deny, rule=P1
```

**Expected**: Denied P1 because `canonicalise("bob") != "alice"`.

---

### S-04: P2 — Amount exceeds limit, no approval_ref

```python
# Requires: expense_limit: 100 in config; claimant == user; valid token
hook = PolicyHook("test-agent", "alice", "http://127.0.0.1:8765",
                  bearer_token=VALID_TOKEN, config_dir=config_dir)
event = FakeEvent("submit_expense", {"claimant": "alice", "amount": 200, "description": "laptop"})
hook._before_tool_call(event)
assert event.cancel_tool
# Log entry: decision=deny, rule=P2
```

**Boundary**: Amount = 100 (equal to limit) → allow. Amount = 101 → deny P2.

---

### S-05: P3 — Self-approval

```python
# Requires: approve_expense in allowed_tools; expense claimant == acting user
# The directory must return claimant=alice for the expense
hook = PolicyHook("test-agent", "alice", "http://127.0.0.1:8765",
                  bearer_token=VALID_TOKEN, config_dir=config_dir)
event = FakeEvent("approve_expense", {"expense_id": "exp-001"})
hook._before_tool_call(event)
assert event.cancel_tool
# Log entry: decision=deny, rule=P3
```

---

### S-06: P4 — No active delegation for travel booking

```python
# Requires: book_travel in allowed_tools; traveller != acting user; no delegation
hook = PolicyHook("test-agent", "alice", "http://127.0.0.1:8765",
                  bearer_token=VALID_TOKEN, config_dir=config_dir)
event = FakeEvent("book_travel", {"traveller": "frank", "details": "LHR-JFK"})
hook._before_tool_call(event)
assert event.cancel_tool
# Log entry: decision=deny, rule=P4, sim_time is not null (delegations lookup was made)
```

---

### S-07: P5 — Vendor not in approved list

```python
# Requires: pay_vendor in allowed_tools; vendor not in directory's approved list
hook = PolicyHook("test-agent", "alice", "http://127.0.0.1:8765",
                  bearer_token=VALID_TOKEN, config_dir=config_dir)
event = FakeEvent("pay_vendor", {"vendor": "unapproved-corp", "amount": 500, "reference": "ref-1"})
hook._before_tool_call(event)
assert event.cancel_tool
# Log entry: decision=deny, rule=P5
```

---

### S-08: P6 — Fingerprint mismatch

```python
# Requires: fingerprint stored for tool; selected_tool has different definition
import hashlib, json

WRONG_FINGERPRINT = "0" * 64  # deliberately wrong
write_config(config_dir, allowed_tools=["read_receipt"],
             fingerprints={"read_receipt": WRONG_FINGERPRINT})
hook = PolicyHook("test-agent", "alice", "http://127.0.0.1:8765",
                  bearer_token=VALID_TOKEN, config_dir=config_dir)

class FakeTool:
    @property
    def tool_spec(self):
        return {"name": "read_receipt", "description": "Read a receipt", "inputSchema": {}}

event = FakeEvent("read_receipt", {"receipt_id": "r-1"}, selected_tool=FakeTool())
hook._before_tool_call(event)
assert event.cancel_tool
# Log entry: decision=deny, rule=P6
```

---

### S-09: SCOPE — Missing or invalid bearer token

```python
hook = PolicyHook("test-agent", "alice", "http://127.0.0.1:8765",
                  bearer_token="",  # empty
                  config_dir=config_dir)
event = FakeEvent("submit_expense", {"claimant": "alice", "amount": 50, "description": "x"})
hook._before_tool_call(event)
assert event.cancel_tool
# Log entry: decision=deny, rule=SCOPE
```

---

### S-10: 5xx propagation

```python
# Requires: directory returning 503 for any route
import pytest, httpx
# Point base_url at an endpoint that returns 500
hook = PolicyHook("test-agent", "alice", "http://127.0.0.1:8765",
                  bearer_token=VALID_TOKEN,
                  config_dir=config_dir_with_role_restriction)
event = FakeEvent("submit_expense", {"claimant": "alice", "amount": 50, "description": "x"})
with pytest.raises(httpx.HTTPStatusError):
    hook._before_tool_call(event)
# Log entry written with decision=error before the error propagated
```

---

### S-11: Allow path (all checks pass)

```python
# Requires: all checks configured to pass
hook = PolicyHook("expense-assistant", "alice", "http://127.0.0.1:8765",
                  bearer_token=VALID_TOKEN,
                  config_dir=pathlib.Path("config/agents"))
event = FakeEvent("read_receipt", {"receipt_id": "r-001"})
hook._before_tool_call(event)
assert not event.cancel_tool
# Log entry: decision=allow, rule=null
```

---

## Log Verification

After each scenario, verify the last log entry:

```python
log_entries = [
    json.loads(line)
    for line in pathlib.Path(os.environ["HOOK_LOG"]).read_text().splitlines()
    if line.strip()
]
entry = log_entries[-1]
assert all(f in entry for f in
    ("call_id", "run_id", "timestamp", "sim_time", "user", "agent",
     "tool", "arguments", "decision", "rule", "reason"))
```

All 11 required fields must be present in every entry. See
[log-entry-schema.md](./contracts/log-entry-schema.md) for field definitions.
