# Data Model: PolicyHook Checkpoint Enforcement

---

## Entity: PolicyHook

The central enforcement component. One instance per agent-session. Loaded once at construction; evaluates every tool call before execution.

| Field | Type | Source | Notes |
|-------|------|--------|-------|
| `_agent_name` | `str` | Constructor param `agent_name` | Used as log `agent` field and to locate the YAML config |
| `_user` | `str` | Constructor param `user` | The acting user for all calls in this session; used as log `user` field and in policy checks |
| `_base_url` | `str` | Constructor param `base_url` | Base URL of the directory HTTP service (e.g. `http://localhost:8765`) |
| `_run_id` | `str \| None` | Constructor param `run_id`, default `None` | Propagated to every log entry |
| `_bearer_token` | `str` | Constructor param `bearer_token`, default `""` | JWT decoded on every call for SCOPE check |
| `_config` | `AgentConfig` (dict) | Loaded from YAML at construction | Contains `acts_for`, `expense_limit`, `allowed_tools`, `fingerprints` |
| `_http` | `httpx.Client` | Created at construction | Reused for all directory calls within the session |

**Constructor signature** (from `spec_input.md`):
```python
def __init__(
    self,
    agent_name: str,
    user: str,
    base_url: str,
    run_id: str | None = None,
    limit_override: int | None = None,
    config_dir: pathlib.Path | None = None,
    bearer_token: str = "",
) -> None
```

`limit_override` is accepted but not used for policy (retained for interface compatibility).

---

## Entity: AgentConfig (YAML)

Per-agent static configuration. Read once at construction from `{config_dir}/{agent_name}.yaml`. Never re-read during a session.

| Field | Type | Required | Example |
|-------|------|----------|---------|
| `agent` | `str` | yes | `"expense-assistant"` |
| `acts_for` | `str` | yes | `"any"` or `"finance"` |
| `expense_limit` | `int` | no | `500` |
| `allowed_tools` | `list[str]` | yes | `["submit_expense", "approve_expense"]` |
| `fingerprints` | `dict[str, str]` | yes | `{"submit_expense": "<sha256hex>"}` |

`acts_for` semantics: `"any"` → skip role check; any other string → require that role from `/directory/users/{user}`.

`expense_limit` absent → no P2 limit applies; expenses of any amount pass P2.

Fingerprint values are 64-character lowercase hex strings (SHA-256 digest).

---

## Entity: DecisionLogEntry

A single JSON-on-a-line record appended to the file at `$HOOK_LOG` for every tool-call evaluation, regardless of outcome.

| Field | Type | Allow | Deny | Error |
|-------|------|-------|------|-------|
| `call_id` | `str` (UUID4) | set | set | set |
| `run_id` | `str \| null` | from constructor | from constructor | from constructor |
| `timestamp` | `str` (ISO-8601 UTC) | set | set | set |
| `sim_time` | `float \| null` | from delegations response or null | from delegations response or null | null |
| `user` | `str` | acting user | acting user | acting user |
| `agent` | `str` | agent name | agent name | agent name |
| `tool` | `str` | tool name | tool name | tool name |
| `arguments` | `dict` | raw input dict | raw input dict | raw input dict |
| `decision` | `"allow"` | `"deny"` | `"error"` |
| `rule` | `null` | `"P1"`–`"P7"`, `"SCOPE"` | `null` |
| `reason` | `str` | `"allow"` | human-readable | exception message |

`sim_time` is populated from `GET /directory/delegations` response only (the only directory endpoint that returns it). It is `null` for all other decision paths.

---

## Entity: CheckResult (internal)

Transient value object used internally during a single `_before_tool_call` invocation. Never persisted.

| Field | Type | Description |
|-------|------|-------------|
| `decision` | `Literal["allow", "deny", "error"]` | Outcome of evaluation |
| `rule` | `str \| None` | Rule that triggered deny; `None` for allow |
| `reason` | `str` | Human-readable justification |
| `sim_time` | `float \| None` | Captured from delegations response if fetched |

---

## Directory Data Shapes (fetched live, never stored)

### User record (`GET /directory/users/{user}`)
```json
{"role": "employee", "manager": "bob"}
```
- `role`: the user's role string (compared against `acts_for` config field)
- `manager`: username of the user's direct manager (used in P3 check); may be `null`

### Delegation record (element of `GET /directory/delegations` → `.delegations[]`)
```json
{
  "id": "del-001",
  "delegator": "carol",
  "delegate": "dan",
  "scope": ["travel"],
  "expires": "2027-12-31T23:59:59Z",
  "active": true
}
```
- Checkpoint checks `active == True` and `"travel" in scope`; does **not** compare `expires`
- `delegator` is the person being booked for; `delegate` is the person booking

### Vendor list (`GET /directory/vendors`)
```json
["acme-hotels", "fastair", "reliable-cabs"]
```
Plain JSON array of canonicalised vendor name strings.

### Expense record (`GET /directory/expenses/{id}`)
```json
{"claimant": "alice", "status": "pending"}
```
Used in P3 only; `claimant` is compared against the acting user and their manager.

---

## Evaluation State Transitions

```text
_before_tool_call(event)
    │
    ├─ canonicalise(user) == "" ────────────────────────────────────> deny P7 (blank-user)
    │
    ├─ tool not in allowed_tools ───────────────────────────────────> deny P7
    │
    ├─ acts_for != "any"
    │     └─ GET /directory/users/{user}
    │           ├─ 404 ─────────────────────────────────────────────> deny P7
    │           └─ role != acts_for ───────────────────────────────── deny P7
    │
    ├─ selected_tool is not None AND fingerprint stored
    │     └─ sha256(tool_spec) != stored ───────────────────────────> deny P6
    │
    ├─ jwt.decode(bearer_token) fails ──────────────────────────────> deny SCOPE
    │     OR required_scope not in token.scope ─────────────────────> deny SCOPE
    │
    ├─ tool == "submit_expense"
    │     ├─ canonicalise(claimant) != user ────────────────────────> deny P1
    │     └─ amount > expense_limit AND no approval_ref ────────────> deny P2
    │
    ├─ tool == "approve_expense"
    │     └─ GET /directory/expenses/{expense_id}
    │           ├─ 404 ─────────────────────────────────────────────> deny P3
    │           ├─ user == claimant ───────────────────────────────── deny P3
    │           └─ GET /directory/users/{claimant}
    │                 ├─ 404 ───────────────────────────────────────> deny P3
    │                 └─ manager != user ─────────────────────────── deny P3
    │
    ├─ tool == "book_travel" AND canonicalise(traveller) != user
    │     └─ GET /directory/delegations  [captures sim_time]
    │           └─ no active delegation (delegator=traveller, delegate=user, "travel" in scope) ─> deny P4
    │
    ├─ tool == "pay_vendor"
    │     └─ GET /directory/vendors
    │           └─ canonicalise(vendor) not in list ─────────────── deny P5
    │
    └─ (all checks passed) ────────────────────────────────────────> allow
```

Every path (including 5xx exception propagation after logging an error entry) appends exactly one log entry to `HOOK_LOG`.
