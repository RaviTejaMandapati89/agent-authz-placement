# Contract: Hook Log Entry Schema

Every tool-call evaluation produces exactly one JSON object appended as a single line (JSONL) to the file path held in the `HOOK_LOG` environment variable.

---

## Schema

```json
{
  "call_id":   "<UUID4 string>",
  "run_id":    "<string or null>",
  "timestamp": "<ISO-8601 UTC string>",
  "sim_time":  "<float or null>",
  "user":      "<string>",
  "agent":     "<string>",
  "tool":      "<string>",
  "arguments": {},
  "decision":  "allow | deny | error",
  "rule":      "<string or null>",
  "reason":    "<string>"
}
```

---

## Field definitions

| Field | Type | Description |
|-------|------|-------------|
| `call_id` | string (UUID4) | Unique identifier for this evaluation; generated fresh per call |
| `run_id` | string \| null | Value passed to `PolicyHook.__init__` as `run_id`; `null` if not provided |
| `timestamp` | string | ISO-8601 UTC timestamp of the log write (e.g. `"2026-10-05T12:00:00.000000+00:00"`) |
| `sim_time` | float \| null | Simulation clock from `GET /directory/delegations` response; `null` when that endpoint was not called |
| `user` | string | Acting user; value passed to `PolicyHook.__init__` as `user` |
| `agent` | string | Agent name; value passed to `PolicyHook.__init__` as `agent_name` |
| `tool` | string | Tool name from `event.tool_use["name"]` |
| `arguments` | object | Raw input dict from `event.tool_use["input"]` |
| `decision` | string | `"allow"` on success, `"deny"` on policy violation, `"error"` on unexpected exception |
| `rule` | string \| null | Rule identifier for denials: `"P1"`–`"P7"` or `"SCOPE"`; `null` for allow or error |
| `reason` | string | Human-readable justification; `"allow"` for allowed calls |

---

## Valid `decision` / `rule` combinations

| decision | rule | Trigger |
|----------|------|---------|
| `"allow"` | `null` | All checks passed |
| `"deny"` | `"P1"` | `claimant` ≠ acting user in `submit_expense` |
| `"deny"` | `"P2"` | Amount exceeds limit and no `approval_ref` |
| `"deny"` | `"P3"` | Self-approval or approver not claimant's manager |
| `"deny"` | `"P4"` | No active travel delegation for the traveller |
| `"deny"` | `"P5"` | Vendor not in approved vendor list |
| `"deny"` | `"P6"` | Tool fingerprint mismatch |
| `"deny"` | `"P7"` | Blank user, tool not allowed, or role mismatch |
| `"deny"` | `"SCOPE"` | Missing/invalid JWT or missing required scope |
| `"error"` | `null` | Unexpected exception (logged before re-raise or with best-effort; 5xx from directory propagates) |

---

## Example entries

**Allow**:
```json
{"call_id": "a1b2c3d4-...", "run_id": "run-001", "timestamp": "2026-10-05T10:00:00+00:00", "sim_time": null, "user": "alice", "agent": "expense-assistant", "tool": "submit_expense", "arguments": {"claimant": "alice", "amount": 120.0, "description": "Office supplies"}, "decision": "allow", "rule": null, "reason": "allow"}
```

**Deny P7 (tool not allowed)**:
```json
{"call_id": "b2c3d4e5-...", "run_id": "run-001", "timestamp": "2026-10-05T10:00:01+00:00", "sim_time": null, "user": "alice", "agent": "expense-assistant", "tool": "book_travel", "arguments": {"traveller": "alice", "details": "..."}, "decision": "deny", "rule": "P7", "reason": "tool 'book_travel' not in allowed_tools"}
```

**Deny SCOPE (missing token)**:
```json
{"call_id": "c3d4e5f6-...", "run_id": null, "timestamp": "2026-10-05T10:00:02+00:00", "sim_time": null, "user": "alice", "agent": "expense-assistant", "tool": "submit_expense", "arguments": {}, "decision": "deny", "rule": "SCOPE", "reason": "bearer token missing or invalid"}
```

**Deny P4 (no delegation, sim_time captured)**:
```json
{"call_id": "d4e5f6a7-...", "run_id": "run-002", "timestamp": "2026-10-05T10:00:03+00:00", "sim_time": 1759737600.0, "user": "dan", "agent": "travel-assistant", "tool": "book_travel", "arguments": {"traveller": "bob", "details": "..."}, "decision": "deny", "rule": "P4", "reason": "no active travel delegation from 'bob' to 'dan'"}
```

---

## Invariants

1. Exactly one entry is appended per `_before_tool_call` invocation.
2. The entry is written before `cancel_tool` is set (deny) or before the call returns (allow).
3. On 5xx propagation, a best-effort error entry is appended before the exception escapes (decision: `"error"`, rule: `null`).
4. `call_id` is unique across all entries in the log.
5. `sim_time` is non-null if and only if `GET /directory/delegations` was called during that evaluation.
