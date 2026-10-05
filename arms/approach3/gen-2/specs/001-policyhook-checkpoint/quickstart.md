# Quickstart Validation Guide: PolicyHook Checkpoint Module

**Branch**: `001-policyhook-checkpoint` | **Date**: 2026-10-05

This guide documents how to validate that `checkpoint.py` is working end-to-end. It covers
prerequisites, how to start the directory service, and the key scenarios to verify. See
[contracts/policyhook-interface.md](contracts/policyhook-interface.md) for interface details
and [data-model.md](data-model.md) for the full check-order logic.

---

## Prerequisites

- Python 3.12 environment via `uv` (`.venv` present at repo root)
- All dependencies installed: `uv sync` or `uv run python -c "import httpx, jwt, yaml, strands"`
- `checkpoint.py` exists at the repo root
- `config/agents/` directory contains at least one `.yaml` config file
- `HOOK_LOG` environment variable set to a writable file path

---

## Start the Directory Service

The directory service is provided as `domain/server.py`. Start it in a separate terminal:

```bash
cd /path/to/repo
uv run python -m domain.server
# Server listens on http://localhost:8000 by default
```

Confirm it is running:

```bash
curl http://localhost:8000/directory/users/alice
# Expected: {"role": "employee", "manager": "bob"}
```

---

## Scenario 1 — Allow (all checks pass)

**Setup**: Use `expense-assistant` agent config (acts_for: any, has `submit_expense` in allowed_tools).

```python
import os, pathlib
os.environ["HOOK_LOG"] = "/tmp/hook.jsonl"

from checkpoint import PolicyHook
from domain.tokens import issue_user_token

token = issue_user_token("alice", "mcp-server", ["expenses:submit"])
hook = PolicyHook(
    agent_name="expense-assistant",
    user="alice",
    base_url="http://localhost:8000",
    run_id="run-001",
    config_dir=pathlib.Path("config/agents"),
    bearer_token=token,
)

class FakeEvent:
    tool_use = {"name": "submit_expense", "input": {"claimant": "alice", "amount": 100}}
    selected_tool = None
    cancel_tool = None

hook._before_tool_call(FakeEvent())
```

**Expected**: `FakeEvent.cancel_tool` remains falsy; `/tmp/hook.jsonl` contains one entry
with `"decision": "allow"` and `"rule": null`.

---

## Scenario 2 — Deny P7 (tool not in allowed_tools)

```python
class FakeEvent:
    tool_use = {"name": "pay_vendor", "input": {}}
    selected_tool = None
    cancel_tool = None

hook._before_tool_call(FakeEvent())
```

**Expected**: `FakeEvent.cancel_tool` is a non-empty string; log entry has
`"decision": "deny"`, `"rule": "P7"`.

---

## Scenario 3 — Deny SCOPE (missing bearer token)

```python
hook_no_token = PolicyHook(
    agent_name="expense-assistant",
    user="alice",
    base_url="http://localhost:8000",
    config_dir=pathlib.Path("config/agents"),
    bearer_token="",
)

class FakeEvent:
    tool_use = {"name": "submit_expense", "input": {"claimant": "alice", "amount": 50}}
    selected_tool = None
    cancel_tool = None

hook_no_token._before_tool_call(FakeEvent())
```

**Expected**: log entry has `"decision": "deny"`, `"rule": "SCOPE"`.

---

## Scenario 4 — Deny P1 (claimant ≠ user)

```python
class FakeEvent:
    tool_use = {"name": "submit_expense", "input": {"claimant": "bob", "amount": 50}}
    selected_tool = None
    cancel_tool = None

hook._before_tool_call(FakeEvent())
```

**Expected**: log entry has `"decision": "deny"`, `"rule": "P1"`.

---

## Scenario 5 — Directory 5xx Propagation

1. Temporarily configure the directory service to return 500 on `/directory/users/alice`
   (or use a mock that returns 500).
2. Trigger a tool call that requires a user lookup (acts_for role check or P3).

**Expected**: `httpx.HTTPStatusError` is raised and propagates to the caller; no
`"allow"` log entry is written.

---

## Run the Contract Test Suite

```bash
uv run pytest tests/test_contract.py -v
```

All 7 tests must pass. These tests verify format and interface only (not policy logic).

---

## Run Generated Tests (when available)

```bash
uv run pytest tests/generated/ -v
```

Generated tests cover each of P1–P7 and SCOPE independently.

---

## Validate: No Hardcoded Policy Data

```bash
grep -n "allowed_tools\|fingerprint\|acts_for\|expense_limit" checkpoint.py
```

**Expected**: Zero matches for literal policy values. The only matches allowed are
variable names referencing values loaded from YAML/config at runtime.

---

## Validate: Allowed Imports Only

```bash
python -c "
import ast, sys
tree = ast.parse(open('checkpoint.py').read())
imports = [n for n in ast.walk(tree) if isinstance(n, (ast.Import, ast.ImportFrom))]
for i in imports:
    print(ast.dump(i))
"
```

**Expected**: All import nodes reference only: `stdlib`, `httpx`, `jwt`, `yaml`,
`strands`, `domain.identity`, `domain.scopes`.
