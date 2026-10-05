# Data Model: PolicyHook Checkpoint Enforcement

**Branch**: `001-policy-hook-checkpoint` | **Date**: 2026-10-05

---

## Entities

### 1. PolicyHook (runtime object)

The enforcement component. One instance per agent session.

| Field | Type | Source | Notes |
|-------|------|--------|-------|
| `agent_name` | `str` | constructor arg | Name of the agent; used for config file lookup and log entries |
| `user` | `str` | constructor arg | Acting user for this session; canonicalised at construction |
| `base_url` | `str` | constructor arg | Directory service base URL (e.g. `http://127.0.0.1:8765`) |
| `run_id` | `str \| None` | constructor arg | Optional run identifier for log correlation |
| `bearer_token` | `str` | constructor arg | JWT bearer token for SCOPE check; default `""` |
| `_allowed_tools` | `list[str]` | agent config YAML | Tools this agent may call; loaded at init |
| `_fingerprints` | `dict[str, str]` | agent config YAML | Tool name → hex SHA-256; loaded at init |
| `_acts_for` | `str` | agent config YAML | `"any"` or a role name; loaded at init |
| `_expense_limit` | `int \| None` | agent config YAML or `limit_override` | Max expense amount without approval_ref; `None` = no limit |

**Relationships**: Reads `AgentConfig` at construction. Produces `LogEntry` on every
`BeforeToolCallEvent`. Makes HTTP calls to the `Directory` for live facts.

**State transitions**: `PolicyHook` is stateless per call. No mutable state after `__init__`.

---

### 2. AgentConfig (loaded from YAML)

A per-agent YAML file at `{config_dir}/{agent_name}.yaml`.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `agent` | `str` | yes | Agent name (informational) |
| `acts_for` | `str` | yes | `"any"` or role name (e.g. `"finance"`) |
| `allowed_tools` | `list[str]` | yes | Tool names this agent may call |
| `fingerprints` | `dict[str, str]` | no | Tool name → SHA-256 hex; absent = no fingerprint check for that tool |
| `expense_limit` | `int` | no | Max `submit_expense` amount without `approval_ref`; absent = no limit |

**Validation rules**:
- `allowed_tools` must be a list (may be empty)
- `fingerprints` must be a map if present; individual entries are optional
- `expense_limit` is interpreted as an integer; if absent, P2 limit check is skipped

**Example** (from `config/agents/expense-assistant.yaml`):
```yaml
agent: expense-assistant
acts_for: any
expense_limit: 500
allowed_tools:
  - read_receipt
  - submit_expense
  - approve_expense
fingerprints:
  read_receipt: 80d48b7490ae5b989880a39a257d8eadf182d3d6bed8e55783b8560a8912eabb
  submit_expense: 4548fbca0d1440ee2f3bb0aa3fdc4514a38e452fd5b421c898528a0a5b486207
  approve_expense: fe14fa2f110a789720b2a8b5c3a64b3c45cb57684fabcc9a8af8a101275e378f
```

---

### 3. LogEntry (JSONL audit record)

One record per tool call evaluation, appended to `$HOOK_LOG` before the tool executes.

| Field | Type | Nullable | Content |
|-------|------|----------|---------|
| `call_id` | `str` (UUID4) | no | Unique identifier for this evaluation |
| `run_id` | `str \| null` | yes | Run identifier from constructor; `null` if not set |
| `timestamp` | `str` (ISO-8601 UTC) | no | Decision time, e.g. `"2026-10-05T12:00:00.000Z"` |
| `sim_time` | `float \| null` | yes | Simulation clock from directory response; `null` if no directory call or response lacked `sim_time` |
| `user` | `str` | no | Acting user from constructor |
| `agent` | `str` | no | Agent name from constructor |
| `tool` | `str` | no | Tool name from `event.tool_use["name"]` |
| `arguments` | `dict` | no | Raw arguments from `event.tool_use.get("input", {})` |
| `decision` | `str` | no | One of `"allow"`, `"deny"`, `"error"` |
| `rule` | `str \| null` | yes | Rule that triggered deny: `"P1"`–`"P7"`, `"SCOPE"`, or `null` for allow |
| `reason` | `str` | no | Human-readable explanation of the decision |

**Invariants**:
- Exactly one entry per evaluation (SC-001)
- Entry is written before the tool executes (FR-015)
- On error in `_before_tool_call`, `decision = "error"` entry is written before re-raising

---

### 4. DirectoryUser (live, per-call)

Fetched from `GET /directory/users/{username}`. Never cached.

| Field | Type | Notes |
|-------|------|-------|
| `role` | `str` | User's current role |
| `manager` | `str` | User's current manager (canonicalised username) |

**404 handling**: Treat as "user not found" → deny with applicable rule (P3, P7).

---

### 5. Delegation (live, per-call)

Each item in the `delegations` list from `GET /directory/delegations`.

| Field | Type | Notes |
|-------|------|-------|
| `from` | `str` | Delegating user (canonicalised) |
| `to` | `str` | Delegate user (canonicalised) |
| `active` | `bool` | Whether the delegation is currently active |
| `expires` | `str` (ISO-8601) | Expiry timestamp (pre-filtered by directory; checkpoint ignores it) |

**Response envelope**:
```json
{"delegations": [...], "sim_time": 1234567890.0}
```

`sim_time` from this response populates the log entry's `sim_time` field.

**Active check**: Only `active == True` delegations count. Checkpoint does not compare
timestamps.

---

### 6. VendorList (live, per-call)

Fetched from `GET /directory/vendors`. Never cached.

The response is a JSON object whose `vendors` key (or the object itself, depending on server
implementation) contains a list of approved vendor name strings (canonicalised).

**Implementation note**: Inspect actual server response format at call time.
The server returns `JSONResponse(state.vendors)` where `state.vendors` is a dict with a
`"vendors"` key containing a list of strings.

---

### 7. ExpenseRecord (live, per-call)

Fetched from `GET /directory/expenses/{expense_id}` for the P3 check. Never cached.

| Field | Type | Notes |
|-------|------|-------|
| `claimant` | `str` | Expense claimant (canonicalised) |
| `status` | `str` | Expense status (informational; not used in P3 check) |

**404 handling**: Treat as "expense not found" → deny with P3.

---

## Check-Order State Machine

```
START
  │
  ▼
[1] user.strip().lower() == "" ?
      yes → DENY P7 "blank user"
      no  ↓
[2] tool_name in allowed_tools ?
      no  → DENY P7 "tool not allowed"
      yes ↓
[3] acts_for != "any" ?
      yes → GET /directory/users/{user}
              404 → DENY P7 "user not in directory"
              5xx → PROPAGATE
              role != acts_for → DENY P7 "role mismatch"
      any/pass ↓
[4] selected_tool is not None AND fingerprint stored for tool ?
      yes → compute SHA-256(name+description+inputSchema)
              mismatch → DENY P6 "fingerprint mismatch"
      skip ↓
[5] bearer_token empty or invalid JWT ?
      yes → DENY SCOPE "missing/invalid token"
      no  → required_scope = TOOL_SCOPE_MAP.get(tool_name)
              required_scope not None AND not in token scopes → DENY SCOPE
      pass ↓
[6] tool-specific business rules:
      submit_expense  → P1, P2
      approve_expense → P3 (GET /directory/expenses, GET /directory/users)
      book_travel     → P4 (GET /directory/delegations if traveller != user)
      pay_vendor      → P5 (GET /directory/vendors)
      other tools     → pass
  │
  ▼
ALLOW
```
