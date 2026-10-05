# Data Model: PolicyHook Checkpoint Module

**Phase**: 1 | **Branch**: `001-policyhook-checkpoint` | **Date**: 2026-10-05

---

## Entity: PolicyHook

The central runtime object. One instance per agent invocation. Holds all configuration and
state needed for the lifetime of the agent session.

| Field | Type | Source | Notes |
|---|---|---|---|
| `agent_name` | `str` | constructor | Identifies the agent; used to locate the YAML config and populate log entries |
| `user` | `str` | constructor | Acting user (pre-canonicalised at construction) |
| `base_url` | `str` | constructor | Base URL of the directory service |
| `run_id` | `str \| None` | constructor (optional) | Opaque run identifier; written to every log entry |
| `expense_limit` | `int` | YAML config (or `limit_override`) | Maximum expense amount without an approval reference |
| `allowed_tools` | `list[str]` | YAML config | Tools the agent is permitted to call |
| `fingerprints` | `dict[str, str]` | YAML config | Map from tool name → expected SHA-256 hex digest |
| `acts_for` | `str` | YAML config | `"any"` or a role name |
| `bearer_token` | `str` | constructor (optional) | JWT bearer token; validated on every call |
| `_client` | `httpx.Client` | constructed | Shared synchronous HTTP client |

**Validation rules**:
- `config_dir` defaults to `pathlib.Path(__file__).parent / "config" / "agents"` when `None`
- `limit_override`, if provided, replaces the `expense_limit` from the YAML config
- YAML file must be named `{agent_name}.yaml` and be loadable with `yaml.safe_load`

**State transitions**: PolicyHook has no mutable policy state after construction. Configuration
is frozen at construction; bearer token is set at construction and used read-only on every call.

---

## Entity: AgentConfig (YAML schema)

Loaded once at construction from `{config_dir}/{agent_name}.yaml`.

| Field | YAML type | Required | Notes |
|---|---|---|---|
| `allowed_tools` | sequence of strings | yes | List of permitted tool names |
| `fingerprints` | mapping (str → str) | yes | Tool name → SHA-256 hex digest; may be empty `{}` |
| `acts_for` | string | yes | `"any"` or a role name from the directory |
| `expense_limit` | integer | no | Overridable by constructor `limit_override` |

**Validation rules**:
- Missing `allowed_tools` or `fingerprints` → `KeyError` at construction
- Missing `acts_for` → `KeyError` at construction
- Missing `expense_limit` with no `limit_override` → `None`; P2 rule skips the amount check
  if limit is `None` (treated as unlimited). _Note: spec_input.md implies it is always set in
  practice; implementation should handle absence defensively._

---

## Entity: DecisionLogEntry

Written as a single-line JSON object appended to `HOOK_LOG` for every tool-call evaluation.

| Field | Type | Notes |
|---|---|---|
| `call_id` | `str` (UUID4) | Unique per evaluation; generated fresh each call |
| `run_id` | `str \| None` | From constructor |
| `timestamp` | `str` (ISO-8601 UTC) | `datetime.utcnow().isoformat() + "Z"` |
| `sim_time` | `float \| None` | From directory response when a lookup was made; `null` otherwise |
| `user` | `str` | Acting user |
| `agent` | `str` | Agent name |
| `tool` | `str` | Tool name from `event.tool_use["name"]` |
| `arguments` | `dict` | Raw arguments from `event.tool_use["input"]` |
| `decision` | `str` | `"allow"`, `"deny"`, or `"error"` |
| `rule` | `str \| None` | Rule id that determined the decision: `"P1"`–`"P7"`, `"SCOPE"`, or `null` for allow |
| `reason` | `str` | Human-readable explanation |

**Validation rules**:
- All 11 fields must be present in every entry (including `null` values where permitted)
- Serialised with `json.dumps(..., default=str)` and appended with `\n`
- Written before the callback returns; write failure is not caught (propagates to caller)

---

## Entity: DirectoryUserRecord

Response shape from `GET /directory/users/{username}`.

| Field | Type | Notes |
|---|---|---|
| `role` | `str` | User's role name |
| `manager` | `str` | Username of the user's manager |

**On 404**: User not found — apply deny for the relevant check (P7 for acts_for; P3 for approve_expense claimant lookup).

---

## Entity: DirectoryDelegationsResponse

Response shape from `GET /directory/delegations`.

| Field | Type | Notes |
|---|---|---|
| `delegations` | `list[dict]` | List of delegation objects |
| `sim_time` | `float` | Simulation clock time; used to populate `sim_time` in log entry |

Each delegation object in the list:

| Field | Type | Notes |
|---|---|---|
| `delegator` | `str` | The user who granted the delegation |
| `delegate` | `str` | The user who received the delegation |
| `scope` | `list[str]` | Scopes covered (e.g. `["travel"]`) |
| `active` | `bool` | Whether the delegation is currently active (pre-filtered by directory; checkpoint checks this field but must not perform timestamp arithmetic) |

---

## Entity: DirectoryExpenseRecord

Response shape from `GET /directory/expenses/{expense_id}`.

| Field | Type | Notes |
|---|---|---|
| `claimant` | `str` | Username who submitted the expense |
| `status` | `str` | Current status (e.g. `"pending"`, `"approved"`) |

**On 404**: Expense not found — deny with P3 (approver cannot verify ownership).

---

## Entity: DirectoryVendorList

Response from `GET /directory/vendors`.

- Returns a JSON **array** of vendor-name strings (not a wrapped object).
- Used for P5 check: canonicalised `vendor` argument must appear in this list.

---

## Check-Order Summary (state machine)

```
input: (user, tool_name, tool_args, selected_tool, bearer_token)

1. canonicalise(user) == ""        → deny P7  (no directory call; sim_time = null)
2. tool_name not in allowed_tools  → deny P7  (no directory call; sim_time = null)
3. acts_for != "any":
     GET /directory/users/{user}
     → 404 or role mismatch        → deny P7  (sim_time = null; no sim_time on this route)
4. selected_tool is not None AND tool_name in fingerprints:
     compute SHA-256 of tool_spec
     → mismatch                    → deny P6  (sim_time = null)
5. bearer_token == "" OR not decodable JWT → deny SCOPE (sim_time = null)
   decoded scope missing required  → deny SCOPE (sim_time = null)
6a. tool == "submit_expense":      P1, P2
6b. tool == "approve_expense":     P3 (GET /directory/expenses, GET /directory/users)
6c. tool == "book_travel":         P4 (GET /directory/delegations — yields sim_time)
6d. tool == "pay_vendor":          P5 (GET /directory/vendors)
otherwise:                         allow (rule = null)
```
