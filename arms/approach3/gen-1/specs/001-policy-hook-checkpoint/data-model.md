# Data Model: PolicyHook Checkpoint Enforcement

**Phase 1 output for feature `001-policy-hook-checkpoint`**

## Entities

### PolicyHook

The single exported class. Carries per-instance configuration loaded at construction time; stateless across calls except for the config snapshot.

| Field | Type | Source | Notes |
|-------|------|--------|-------|
| `_agent_name` | `str` | constructor | Identifies which config file to load |
| `_user` | `str` | constructor | The acting user (canonicalised at call time) |
| `_base_url` | `str` | constructor | Base URL for all directory HTTP calls |
| `_run_id` | `str \| None` | constructor | Propagated to every log entry |
| `_expense_limit` | `int` | config YAML (`expense_limit`) or `limit_override` | Defaults to `500` if absent from config |
| `_allowed_tools` | `set[str]` | config YAML `allowed_tools` | Loaded once at construction |
| `_fingerprints` | `dict[str, str]` | config YAML `fingerprints` | Map of tool name → SHA-256 hex digest |
| `_acts_for` | `str` | config YAML `acts_for` | `"any"` skips role check; any other value is the required role |
| `_bearer_token` | `str` | constructor | Raw JWT string; `""` triggers SCOPE deny |

**State transitions**: None. Every `_before_tool_call` invocation is independent.

**Validation rules**:
- `config_dir/{agent_name}.yaml` must be loadable YAML at construction time; missing file raises at construction.
- `limit_override`, if provided, replaces `expense_limit` from config.

---

### AgentConfig (YAML)

Persisted in `config/agents/{agent_name}.yaml`. Read once per `PolicyHook` construction.

| Field | YAML type | Required | Default | Semantics |
|-------|-----------|----------|---------|-----------|
| `agent` | string | no | — | Informational label |
| `acts_for` | string | yes | — | `"any"` or a role name |
| `allowed_tools` | list[string] | yes | — | Tool names the agent may call |
| `fingerprints` | map[string, string] | yes | `{}` | Tool name → SHA-256 hex digest |
| `expense_limit` | integer | no | `500` | Threshold for P2 check |

---

### DecisionLogEntry (JSON)

One JSON object per line, appended to the file at `HOOK_LOG`. Written **before** the tool runs (allow or deny).

| Field | JSON type | Required | Value |
|-------|-----------|----------|-------|
| `call_id` | string | yes | UUID4 generated per evaluation |
| `run_id` | string \| null | yes | From constructor |
| `timestamp` | string | yes | ISO-8601 UTC (`datetime.utcnow().isoformat() + "Z"`) |
| `sim_time` | number \| null | yes | From directory response `.get("sim_time")`; `null` if no directory call |
| `user` | string | yes | The acting user passed to constructor |
| `agent` | string | yes | `agent_name` |
| `tool` | string | yes | From `event.tool_use["name"]` |
| `arguments` | object | yes | From `event.tool_use["input"]` (raw, not redacted) |
| `decision` | string | yes | `"allow"`, `"deny"`, or `"error"` |
| `rule` | string \| null | yes | `"P1"`–`"P7"`, `"SCOPE"`, or `null` for allow |
| `reason` | string | yes | Human-readable explanation of the decision |

**Constraint**: Exactly one entry per `_before_tool_call` invocation.

---

### DirectoryUser (HTTP response)

Returned by `GET /directory/users/{username}`.

| Field | JSON type | Notes |
|-------|-----------|-------|
| `role` | string | User's organisational role |
| `manager` | string \| null | Username of the user's direct manager |

HTTP 404 → user not found; apply deny rule for the relevant check (P3 or P7).  
HTTP 5xx → **propagate**, do not catch.

---

### DirectoryDelegation (element in HTTP response)

Returned by `GET /directory/delegations` as `{"delegations": [...], "sim_time": <float>}`.

| Field | JSON type | Notes |
|-------|-----------|-------|
| `delegator` | string | The user who granted authority |
| `delegate` | string | The user who received authority |
| `scope` | list[string] | List of delegation scope strings |
| `expires` | string | ISO-8601 datetime (pre-filtered by directory for expiry) |
| `active` | boolean | Checkpoint MUST check this; `false` → invalid even within expiry |

HTTP 5xx → **propagate**.

---

### DirectoryExpense (HTTP response)

Returned by `GET /directory/expenses/{expense_id}`.

| Field | JSON type | Notes |
|-------|-----------|-------|
| `claimant` | string | Username of the expense's claimant |
| `status` | string | Expense status (e.g., `"pending"`) |

HTTP 404 → expense not found; deny with P3.  
HTTP 5xx → **propagate**.

---

### DirectoryVendors (HTTP response)

Returned by `GET /directory/vendors` as a plain JSON array of strings.

Example: `["acme-hotels", "fastair", "reliable-cabs"]`

HTTP 5xx → **propagate**.

---

## Check-Order State Machine

```
event received
      │
      ▼
[blank-user guard]  ───► deny P7 ──► log → cancel_tool
      │ pass
      ▼
[P7: allowed_tools]  ───► deny P7 ──► log → cancel_tool
      │ pass
      ▼
[acts_for role]  (skip if acts_for=="any")
      │           ──► deny P7 ──► log → cancel_tool
      │ pass
      ▼
[P6: fingerprint]  (skip if selected_tool is None or no fingerprint stored)
      │             ──► deny P6 ──► log → cancel_tool
      │ pass
      ▼
[SCOPE: bearer token]  ──► deny SCOPE ──► log → cancel_tool
      │ pass
      ▼
[P1–P5: tool-specific rules]
      │                  ──► deny Px ──► log → cancel_tool
      │ pass
      ▼
  allow → log (decision=allow, rule=null)
```

**P1–P5 tool routing**:
- `submit_expense` → P1, then P2
- `approve_expense` → P3
- `book_travel` → P4 (only if `traveller != user` after canonicalisation)
- `pay_vendor` → P5
- Any other allowed tool → pass through

## Canonicalisation

All user-supplied `claimant`, `traveller`, and `vendor` values are canonicalised via `domain.identity.canonicalise(s)` which performs `s.strip().lower()` before comparison.
