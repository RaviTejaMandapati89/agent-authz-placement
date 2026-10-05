# Data Model: PolicyHook

## Entities

---

### PolicyHook (runtime object)

The central enforcement component. One instance per agent process.

| Field | Type | Source | Notes |
|-------|------|--------|-------|
| `_agent_name` | `str` | constructor `agent_name` | Name of the agent; used as YAML filename key and log field |
| `_user` | `str` | constructor `user` | Acting user identity (canonicalised before comparisons) |
| `_base_url` | `str` | constructor `base_url` | Base URL for directory HTTP calls |
| `_run_id` | `str \| None` | constructor `run_id` | Run identifier, written to every log entry |
| `_bearer_token` | `str` | constructor `bearer_token` | JWT for SCOPE check; empty string = missing |
| `_allowed_tools` | `list[str]` | YAML `allowed_tools` | Loaded at construction; immutable after init |
| `_fingerprints` | `dict[str, str]` | YAML `fingerprints` | Map of tool name → expected SHA-256 hex digest |
| `_acts_for` | `str` | YAML `acts_for` | `"any"` or a role name string |
| `_expense_limit` | `int \| None` | `limit_override` or YAML `expense_limit` | `None` means no limit applies |

**Validation rules**:
- YAML file must exist and be parseable at construction time; raise `FileNotFoundError` / `ValueError` on failure.
- `_user` is stored as-is; canonicalisation happens at evaluation time.

**State transitions**: `PolicyHook` is stateless after construction. No mutable state is modified during `_before_tool_call`.

---

### Agent Config YAML

Per-agent file at `{config_dir}/{agent_name}.yaml`. Authoritative source for agent-level policy.

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `agent` | `str` | optional | Agent name (informational) |
| `acts_for` | `str` | yes | `"any"` or a role name (e.g. `"finance"`) |
| `expense_limit` | `int` | conditional | Required for agents that can call `submit_expense` |
| `allowed_tools` | `list[str]` | yes | Tool names permitted for this agent |
| `fingerprints` | `dict[str, str]` | yes | Tool name → SHA-256 hex digest |

**Fingerprint canonical form**:
```
sha256(
  tool_spec["name"]
  + tool_spec["description"]
  + json.dumps(tool_spec["inputSchema"], sort_keys=True, separators=(",", ":"))
).hexdigest()
```

Where `tool_spec` is the `ToolSpec` TypedDict obtained from `event.selected_tool.tool_spec`.

**Example** (`config/agents/payments-agent.yaml`):
```yaml
agent: payments-agent
acts_for: finance
allowed_tools:
  - pay_vendor
  - approve_expense
fingerprints:
  pay_vendor: f1575b00b8dcebc74ab7e131ae0e5724f1d95caeb74562e987ebe7d7320649e1
  approve_expense: fe14fa2f110a789720b2a8b5c3a64b3c45cb57684fabcc9a8af8a101275e378f
```

---

### Decision Log Entry

One JSON line appended to `$HOOK_LOG` per `_before_tool_call` invocation.

| Field | Type | Nullable | Notes |
|-------|------|----------|-------|
| `call_id` | `str` (UUID) | no | Unique ID for this evaluation |
| `run_id` | `str \| None` | yes | From constructor; identifies the experiment run |
| `timestamp` | `str` (ISO-8601 UTC) | no | Wall-clock time of decision |
| `sim_time` | `float \| None` | yes | Simulation clock from `/directory/delegations` response; `null` when no delegation lookup was performed |
| `user` | `str` | no | Acting user (`self._user`) |
| `agent` | `str` | no | Agent name (`self._agent_name`) |
| `tool` | `str` | no | Tool name from `event.tool_use["name"]` |
| `arguments` | `dict \| Any` | no | Raw input from `event.tool_use["input"]` |
| `decision` | `str` | no | One of `"allow"`, `"deny"` |
| `rule` | `str \| None` | yes | `"P1"`–`"P7"`, `"SCOPE"`, or `null` for allow |
| `reason` | `str` | no | Human-readable explanation |

---

### Directory: User Record

Returned by `GET /directory/users/{username}`.

| Field | Type | Notes |
|-------|------|-------|
| `role` | `str` | User's role (e.g. `"employee"`, `"finance"`) |
| `manager` | `str \| None` | Username of direct manager |

Returns HTTP 404 if user not found. Returns HTTP 503 when directory is down.

---

### Directory: Delegation Record

Returned within `GET /directory/delegations` response body.

| Field | Type | Notes |
|-------|------|-------|
| `id` | `str` | Delegation identifier |
| `delegator` | `str` | User who granted the delegation |
| `delegate` | `str` | User who received the delegation |
| `scope` | `list[str]` | Delegation scopes (e.g. `["travel"]`) |
| `expires` | `str` | ISO-8601 expiry timestamp |
| `active` | `bool` | Whether delegation is currently active |

Response envelope: `{"delegations": [...], "sim_time": <float>}`. The hook checks `active` field only; it does not evaluate timestamps.

---

### Directory: Expense Record

Returned by `GET /directory/expenses/{expense_id}`.

| Field | Type | Notes |
|-------|------|-------|
| `claimant` | `str` | User who submitted the expense |
| `status` | `str` | Current status (e.g. `"pending"`, `"approved"`) |

Returns HTTP 404 if expense not found.

---

### Directory: Vendor List

Returned by `GET /directory/vendors`. Response is a JSON array of vendor-name strings (e.g. `["acme-hotels", "fastair", "reliable-cabs"]`).
