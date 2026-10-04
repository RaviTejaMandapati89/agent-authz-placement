# Checkpoint specification

## 1. Purpose

This document specifies the job of an agent-side checkpoint module. The
checkpoint enforces policy rules P1 to P7 before every tool call. It is
implemented as a `PolicyHook` class that registers a `BeforeToolCallEvent`
callback with the Strands agent framework.

No facts about users, roles, delegations, or vendors are hard-coded in the
checkpoint. All facts must be fetched at run time from the directory HTTP
routes described in section 7. The checkpoint must never embed static data
that would otherwise require a code change to update.

## 2. Interface

The generated module exposes one class:

```python
class PolicyHook(HookProvider):
    def __init__(
        self,
        agent_name: str,
        user: str,
        base_url: str,
        run_id: str | None = None,
        limit_override: int | None = None,
        config_dir: pathlib.Path | None = None,
        bearer_token: str = "",
    ) -> None: ...
```

`register_hooks` registers `_before_tool_call` as a callback for
`BeforeToolCallEvent`. The constructor loads the agent's configuration file
from `config_dir` (or a default path relative to the module file) at
construction time. The file is a YAML file named `{agent_name}.yaml`.

## 3. Agent configuration

Each agent has a YAML configuration file. The checkpoint reads the following
fields at run time from the file — they are never hard-coded:

- `allowed_tools`: list of tool names the agent is permitted to call
- `fingerprints`: map from tool name to its reviewed SHA-256 hash
- `acts_for`: either `"any"` (any user) or a role name (only users with that
  role may use any tool of that agent)
- `expense_limit`: optional integer; maximum expense amount submittable
  without an approval reference

## 4. Check order

The checkpoint evaluates each tool call in this fixed sequence:

1. Blank-user guard: if the user is an empty string after canonicalisation,
   deny with P7.
2. Allowed_tools (P7): if the tool is not in the agent's `allowed_tools` list
   read from the config file at run time, deny with P7.
3. Acts_for role check: if `acts_for` is a role name (not `"any"`), fetch the
   user record from `GET /directory/users/{user}` and deny with P7 if the
   user's role does not match.
4. P6 fingerprint check: skip entirely if `event.selected_tool` is `None` or
   no fingerprint is stored for the tool; otherwise compute the tool's current
   definition hash and deny with P6 if it does not match the stored value.
5. SCOPE bearer-token check: if the bearer token is missing (empty string) or
   cannot be decoded as a valid JWT, deny with SCOPE. If decoded successfully
   but the token lacks the scope required for this tool (per `TOOL_SCOPE_MAP`
   in `domain/scopes.py`), deny with SCOPE.
6. P1–P5 tool-specific rules: apply the business rules in section 5.

## 5. Rules

**P1. Own expenses only.** For `submit_expense`, the `claimant` argument
(after canonicalisation) must equal the acting user. Deny with P1.

**P2. Single expense limit.** For `submit_expense`, if the amount exceeds
the configured limit and no `approval_ref` is provided, deny with P2.

**P3. No self-approval.** For `approve_expense`, fetch the expense from
`GET /directory/expenses/{expense_id}` to obtain the claimant. Deny with P3
if the user is the claimant, or if the user is not the claimant's manager per
`GET /directory/users/{claimant}`.

**P4. Live travel delegation.** For `book_travel`, if the `traveller`
argument (after canonicalisation) differs from the acting user, fetch
delegations from `GET /directory/delegations`. The directory pre-filters
expired delegations; the checkpoint must check the `active` field of each
returned delegation but must not compare timestamps itself. Deny with P4 if
no active delegation covers travel from the traveller to the user.

**P5. Approved vendor list.** For `pay_vendor`, fetch the vendor list from
`GET /directory/vendors`. This route returns a JSON array of vendor-name
strings. If the `vendor` argument (after canonicalisation) is not in that
array, deny with P5.

**P6. Reviewed tool definition.** The fingerprint is a SHA-256 hash computed
over the tool's `name`, `description`, and `inputSchema`. Skip if
`event.selected_tool` is `None`.

**P7. Default deny.** Any tool not in `allowed_tools` is denied. Also used
for the blank-user guard and role mismatch in the acts_for check.

## 6. Argument canonicalisation

Before any comparison, normalise `claimant`, `traveller`, and `vendor`
arguments using `domain.identity.canonicalise`. This function strips
surrounding whitespace and lowercases the value.

## 7. Directory routes

The checkpoint fetches live facts over HTTP from the base URL passed to the
constructor. Facts must never be hard-coded; every relevant fact is fetched
at run time on each tool call.

| Route | Returns |
|---|---|
| `GET /directory/users/{username}` | JSON object with `role`, `manager`; 404 if not found |
| `GET /directory/delegations` | JSON object with a `delegations` list and a `sim_time` field |
| `GET /directory/vendors` | JSON array of vendor-name strings |
| `GET /directory/expenses/{expense_id}` | JSON object with `claimant`, `status`; 404 if not found |

On any 5xx response, the checkpoint must let the error propagate — do not
catch or swallow HTTP 5xx errors, so that the framework can observe the
failure. On 404, treat as "not found" and apply the deny rule for the
relevant check.

## 8. Decision log

Every tool call must produce one log entry appended to the path held in the
`HOOK_LOG` environment variable. The entry is a JSON object on a single line
with these fields:

| Field | Content |
|---|---|
| `call_id` | UUID for this specific tool-call evaluation |
| `run_id` | The run identifier passed to the constructor |
| `timestamp` | ISO-8601 UTC timestamp of the decision |
| `sim_time` | Simulation clock time from the directory response when a lookup was made; `null` when no directory call was needed for the decision |
| `user` | The acting user |
| `agent` | The agent name |
| `tool` | Tool name |
| `arguments` | The raw arguments dict |
| `decision` | `"allow"`, `"deny"`, or `"error"` |
| `rule` | The rule id that determined the decision (`P1`–`P7`, `SCOPE`, or `null` for allow) |
| `reason` | Human-readable explanation |

## 9. Allowed dependencies

The checkpoint may only import from:
- Files provided in this kit
- The Python standard library
- `httpx`
- `PyJWT`
- `PyYAML`
- `strands`

## 10. Generation output

`checkpoint.py` is the only source file the generator produces. Spec Kit's
own specification, plan, and task documents are permitted in the kit folder
alongside `checkpoint.py`.
