# Contract: PolicyHook Public Interface

**Version**: 1.0.0 | **Branch**: `001-policyhook-checkpoint` | **Date**: 2026-10-05

This document is the authoritative interface contract for `checkpoint.py`. Anything
not listed here is an implementation detail and must not be relied upon by callers.

---

## 1. Module Import

```python
from checkpoint import PolicyHook
```

The module must be importable from the repo root (where `checkpoint.py` is placed). No
other public symbols are required.

---

## 2. Constructor

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

| Parameter | Type | Required | Description |
|---|---|---|---|
| `agent_name` | `str` | yes | Agent identifier; used to locate `{config_dir}/{agent_name}.yaml` |
| `user` | `str` | yes | Acting user for this agent session |
| `base_url` | `str` | yes | Base URL of the directory service (no trailing slash) |
| `run_id` | `str \| None` | no (default `None`) | Opaque run identifier written to every log entry |
| `limit_override` | `int \| None` | no (default `None`) | Overrides `expense_limit` from the agent config when set |
| `config_dir` | `pathlib.Path \| None` | no (default `None`) | Directory containing agent YAML config files; defaults to `{module_dir}/config/agents` |
| `bearer_token` | `str` | no (default `""`) | JWT bearer token; empty string triggers SCOPE deny |

**Postconditions**:
- Agent YAML config is loaded and validated at construction time
- `httpx.Client` is created and ready for use

---

## 3. HookProvider Protocol

```python
def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None: ...
```

Must call `registry.add_callback(BeforeToolCallEvent, self._before_tool_call)`. The
`BeforeToolCallEvent` type must be imported from `strands.hooks`.

---

## 4. Callback

```python
def _before_tool_call(self, event: BeforeToolCallEvent) -> None: ...
```

Called by the Strands framework before each tool invocation. The method:
1. Reads `event.tool_use["name"]` and `event.tool_use["input"]`
2. Reads `event.selected_tool` (may be `None`)
3. Evaluates all policy checks in the fixed order (see [data-model.md](../data-model.md))
4. On deny: sets `event.cancel_tool = "<reason>"` (non-empty string)
5. On allow: leaves `event.cancel_tool` unchanged (falsy)
6. Always: writes one `DecisionLogEntry` JSON line to `HOOK_LOG`

**Side effects**: appends one JSON line to the file at `os.environ["HOOK_LOG"]`.

**Exceptions propagated (not caught)**:
- `httpx.HTTPStatusError` with status 5xx — directory server error
- `RuntimeError` — `HOOK_LOG` env var not set when a log write is attempted

---

## 5. Agent Config Schema (YAML)

File path: `{config_dir}/{agent_name}.yaml`

```yaml
# Required fields
acts_for: "any"            # or a role name string
allowed_tools:             # list of permitted tool names
  - submit_expense
  - read_receipt
fingerprints:              # map: tool_name -> sha256_hex_digest (may be {})
  submit_expense: "abcdef1234..."

# Optional field
expense_limit: 500         # integer; omit if no limit applies
```

---

## 6. Decision Log Entry Schema (JSONL)

Each line written to `HOOK_LOG` is a JSON object conforming to:

```json
{
  "call_id": "<uuid4>",
  "run_id": "<string or null>",
  "timestamp": "<ISO-8601 UTC, e.g. 2026-10-05T12:00:00.000000Z>",
  "sim_time": "<float or null>",
  "user": "<string>",
  "agent": "<string>",
  "tool": "<string>",
  "arguments": {},
  "decision": "allow | deny | error",
  "rule": "P1 | P2 | P3 | P4 | P5 | P6 | P7 | SCOPE | null",
  "reason": "<human-readable string>"
}
```

- Exactly one entry per `_before_tool_call` invocation.
- `rule` is `null` when `decision` is `"allow"`.
- `sim_time` is `null` when no directory call was made or when the called route does
  not return a `sim_time` field.

---

## 7. Error Propagation Contract

| Error condition | Behaviour |
|---|---|
| Directory returns 5xx | `httpx.HTTPStatusError` propagates uncaught to the framework |
| Directory returns 404 | Treated as "not found"; deny rule applied; no exception raised |
| `HOOK_LOG` not set | `RuntimeError` raised; tool call outcome undefined |
| YAML config missing/invalid | Exception raised at construction time (not at call time) |
| Bearer token malformed | `deny` with `rule: "SCOPE"`; `event.cancel_tool` set; no exception |

---

## 8. Allowed Imports (enforced)

`checkpoint.py` must not import anything outside this set:

- Python standard library (any module)
- `httpx`
- `jwt` (PyJWT)
- `yaml` (PyYAML)
- `strands` (any submodule)
- `domain.identity` (from kit)
- `domain.scopes` (from kit)
