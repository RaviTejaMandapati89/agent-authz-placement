# Quickstart: Validating PolicyHook Checkpoint Enforcement

This guide documents runnable validation scenarios that prove the feature works end-to-end. Prerequisites and setup are listed first; each scenario follows the spec's user stories.

---

## Prerequisites

- Python 3.12 virtual environment set up: `uv sync`
- `checkpoint.py` exists at the project root
- Directory server running locally (used by integration tests)

---

## Setup

```bash
# 1. Activate the environment
source .venv/bin/activate

# 2. Run the contract tests to verify module structure
uv run pytest tests/test_contract.py -v

# 3. Set HOOK_LOG for manual testing (optional; tests use tmp_path)
export HOOK_LOG=/tmp/hook_decisions.jsonl
```

---

## Scenario 1: Happy Path — Legitimate Tool Call Allowed

**Validates**: User Story 1 (all rules pass → `decision: allow`)

**Setup**: Use `expense-assistant` config (see `config/agents/expense-assistant.yaml`). Construct `PolicyHook` with a valid bearer token that includes the `expenses:submit` scope and a user whose role satisfies `acts_for: any`.

**Steps**:
1. Create a `PolicyHook` for `expense-assistant`, user `"alice"`, with a valid JWT (scope: `expenses:submit`) and a correct fingerprint for `submit_expense`.
2. Construct a fake `BeforeToolCallEvent` with `tool_use = {"name": "submit_expense", "input": {"claimant": "alice", "amount": 100.0, "description": "test"}}`.
3. Call `hook._before_tool_call(event)`.
4. Read the log entry from `HOOK_LOG`.

**Expected outcome**:
- `event.cancel_tool` is falsy (not set or `False`)
- Log entry: `decision == "allow"`, `rule == null`

---

## Scenario 2: Default Deny — Unlisted Tool Blocked

**Validates**: User Story 2 (tool not in `allowed_tools` → deny P7)

**Steps**:
1. Create `PolicyHook` for `expense-assistant`. 
2. Attempt to call `book_travel` (not in `expense-assistant`'s `allowed_tools`).
3. `event.tool_use = {"name": "book_travel", "input": {...}}`
4. Call `hook._before_tool_call(event)`.

**Expected outcome**:
- `event.cancel_tool` is a non-empty string
- Log entry: `decision == "deny"`, `rule == "P7"`

---

## Scenario 3: Scope Check — Missing Token Blocked

**Validates**: User Story 3 (bearer token empty → deny SCOPE)

**Steps**:
1. Create `PolicyHook` with `bearer_token=""`.
2. Attempt to call any allowed tool (e.g. `submit_expense`).
3. Call `hook._before_tool_call(event)`.

**Expected outcome**:
- Log entry: `decision == "deny"`, `rule == "SCOPE"`

---

## Scenario 4: Business Rules P1–P5

**Validates**: User Story 4 (each rule independently)

### P1 — Wrong claimant
- `tool_use = {"name": "submit_expense", "input": {"claimant": "bob", "amount": 100.0, "description": "x"}}`
- User is `"alice"`; `canonicalise("bob") != "alice"` → deny P1

### P2 — Over limit, no approval ref
- `amount = 600.0`, `expense_limit = 500`, no `approval_ref`
- → deny P2

### P3 — Self-approval
- Directory has expense with `claimant: alice`; user is `"alice"` calling `approve_expense`
- → deny P3 (user == claimant)

### P3b — Not the manager
- Expense claimant is `"alice"`, manager is `"bob"`; user is `"carol"`
- → deny P3 (carol is not alice's manager)

### P4 — No active delegation
- `book_travel` with `traveller = "bob"`, `user = "alice"`, no delegation row for `delegator=bob, delegate=alice`
- → deny P4; log entry has non-null `sim_time`

### P5 — Unlisted vendor
- `pay_vendor` with `vendor = "unknown-vendor"` (not in approved list)
- → deny P5

For each sub-scenario: log entry `decision == "deny"` with the correct `rule`.

---

## Scenario 5: Fingerprint Mismatch — Deny P6

**Validates**: User Story 5

**Steps**:
1. Create `PolicyHook` with a config that has a fingerprint for `submit_expense`.
2. Construct a fake `selected_tool` whose `tool_spec` has a modified `description` (so the computed hash differs from the stored one).
3. Call `hook._before_tool_call(event)` with that `selected_tool`.

**Expected outcome**:
- Log entry: `decision == "deny"`, `rule == "P6"`

**Skip path**:
- Set `event.selected_tool = None` → fingerprint check skipped, evaluation continues

---

## Scenario 6: Audit Log Completeness

**Validates**: User Story 6

**Steps**:
1. Make 5 tool calls (mix of allow and deny outcomes).
2. Read all lines from `HOOK_LOG`.

**Expected outcome**:
- Exactly 5 lines, each valid JSON
- Every line has: `call_id`, `run_id`, `timestamp`, `sim_time`, `user`, `agent`, `tool`, `arguments`, `decision`, `rule`, `reason`
- All `call_id` values are distinct UUIDs

Reference field list: [hook-log-schema.md](contracts/hook-log-schema.md)

---

## Scenario 7: Directory 5xx Propagation

**Validates**: User Story 7

**Steps**:
1. Point `base_url` at a server configured to return HTTP 500 for `/directory/users/{user}`.
2. Call an allowed tool where `acts_for` is not `"any"` (so a user lookup is triggered).
3. Call `hook._before_tool_call(event)`.

**Expected outcome**:
- `httpx.HTTPStatusError` (or similar 5xx error) propagates out of `_before_tool_call`
- The test caller receives the exception; it is not swallowed

**Same test** for vendor lookup (P5), delegations (P4), and expense lookup (P3): each 5xx must propagate.

---

## Running the generated test suite

```bash
# Contract tests only (fast, no server required)
uv run pytest tests/test_contract.py -v

# All tests including generated policy tests
uv run pytest -v

# Specific rule tests (once tests/generated/ exists)
uv run pytest tests/generated/ -v -k "p1 or p2 or p3"
```

---

## Acceptance checklist

- [ ] `uv run pytest tests/test_contract.py` passes (6 tests)
- [ ] Allow path produces `decision: allow` with `rule: null`
- [ ] Each deny rule (P1–P7, SCOPE) is triggered by the correct input and recorded correctly
- [ ] Every evaluation produces exactly one log entry with all required fields
- [ ] Directory 5xx errors propagate out of `_before_tool_call`
- [ ] P6 fingerprint check is skipped when `selected_tool is None`
- [ ] `acts_for: any` skips the directory user lookup
- [ ] `sim_time` is non-null only when the delegations endpoint was called
