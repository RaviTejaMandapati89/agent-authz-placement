# Contract: Decision Log Entry Schema

**Format**: Line-delimited JSON (JSONL) — one JSON object per line

**Destination**: Path held in the `HOOK_LOG` environment variable

---

## Schema

```json
{
  "call_id":   "<uuid4 string>",
  "run_id":    "<string or null>",
  "timestamp": "<ISO-8601 UTC string>",
  "sim_time":  "<float or null>",
  "user":      "<string>",
  "agent":     "<string>",
  "tool":      "<string>",
  "arguments": { "<key>": "<value>", "...": "..." },
  "decision":  "<'allow' | 'deny' | 'error'>",
  "rule":      "<'P1' | 'P2' | 'P3' | 'P4' | 'P5' | 'P6' | 'P7' | 'SCOPE' | null>",
  "reason":    "<string>"
}
```

---

## Field Definitions

| Field | Type | Nullable | Description |
|-------|------|----------|-------------|
| `call_id` | string (UUID4) | no | Unique identifier for this specific tool-call evaluation |
| `run_id` | string \| null | yes | Run identifier from `PolicyHook` constructor; `null` when not set |
| `timestamp` | string | no | ISO-8601 UTC timestamp of the decision, e.g. `"2026-10-05T12:00:00.123456+00:00"` |
| `sim_time` | float \| null | yes | Simulation clock from the directory response; `null` when no directory call was made for this decision, or the directory response did not include `sim_time` |
| `user` | string | no | The acting user (as passed to the constructor) |
| `agent` | string | no | The agent name (as passed to the constructor) |
| `tool` | string | no | The tool name from `event.tool_use["name"]` |
| `arguments` | object | no | Raw arguments dict from `event.tool_use.get("input", {})` |
| `decision` | string | no | One of `"allow"`, `"deny"`, or `"error"` |
| `rule` | string \| null | yes | Rule identifier that caused a deny (e.g. `"P1"`, `"P7"`, `"SCOPE"`); `null` when `decision` is `"allow"` or `"error"` |
| `reason` | string | no | Human-readable explanation; describes which check failed or why the call was allowed |

---

## Invariants

- **Exactly one entry** is written per `_before_tool_call` invocation (SC-001)
- Entry is appended **before** the tool executes (FR-015)
- On error: `decision = "error"` entry is written, then the error propagates
- `decision = "allow"` entries always have `rule = null`
- `decision = "deny"` entries always have a non-null `rule`

---

## Example Entries

**Allow**:
```json
{"call_id": "f47ac10b-58cc-4372-a567-0e02b2c3d479", "run_id": "run-001", "timestamp": "2026-10-05T10:00:00.000000+00:00", "sim_time": null, "user": "alice", "agent": "expense-assistant", "tool": "read_receipt", "arguments": {"receipt_id": "r-001"}, "decision": "allow", "rule": null, "reason": "all checks passed"}
```

**Deny (P1)**:
```json
{"call_id": "a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11", "run_id": "run-001", "timestamp": "2026-10-05T10:01:00.000000+00:00", "sim_time": null, "user": "alice", "agent": "expense-assistant", "tool": "submit_expense", "arguments": {"claimant": "bob", "amount": 100, "description": "lunch"}, "decision": "deny", "rule": "P1", "reason": "claimant 'bob' does not match acting user 'alice'"}
```

**Deny (P7 — tool not allowed)**:
```json
{"call_id": "550e8400-e29b-41d4-a716-446655440000", "run_id": null, "timestamp": "2026-10-05T10:02:00.000000+00:00", "sim_time": null, "user": "carol", "agent": "travel-assistant", "tool": "pay_vendor", "arguments": {"vendor": "acme", "amount": 500, "reference": "ref-1"}, "decision": "deny", "rule": "P7", "reason": "tool 'pay_vendor' not in allowed_tools"}
```

**Deny (SCOPE)**:
```json
{"call_id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8", "run_id": "run-002", "timestamp": "2026-10-05T10:03:00.000000+00:00", "sim_time": null, "user": "dave", "agent": "payments-agent", "tool": "pay_vendor", "arguments": {"vendor": "acme", "amount": 200, "reference": "ref-2"}, "decision": "deny", "rule": "SCOPE", "reason": "token missing scope 'payments:pay'"}
```

**Deny (P4 — with sim_time)**:
```json
{"call_id": "6ba7b811-9dad-11d1-80b4-00c04fd430c8", "run_id": "run-003", "timestamp": "2026-10-05T10:04:00.000000+00:00", "sim_time": 1759651200.0, "user": "eve", "agent": "travel-assistant", "tool": "book_travel", "arguments": {"traveller": "frank", "details": "LHR-JFK"}, "decision": "deny", "rule": "P4", "reason": "no active delegation from 'frank' to 'eve'"}
```
