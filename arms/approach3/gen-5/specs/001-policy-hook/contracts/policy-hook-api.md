# Contract: PolicyHook Public API

## Class Signature

```python
from strands.hooks import HookProvider, HookRegistry
import pathlib

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

    def register_hooks(self, registry: HookRegistry) -> None: ...

    def _before_tool_call(self, event) -> None: ...
```

### Constructor Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_name` | `str` | yes | Name of the agent; used to locate `{agent_name}.yaml` in `config_dir` |
| `user` | `str` | yes | Acting user identity for this PolicyHook instance |
| `base_url` | `str` | yes | Base URL of the directory HTTP service |
| `run_id` | `str \| None` | no | Run identifier written to each log entry |
| `limit_override` | `int \| None` | no | When set, overrides `expense_limit` from YAML for P2 evaluation |
| `config_dir` | `pathlib.Path \| None` | no | Directory containing agent YAML files; defaults to `<module_dir>/config/agents` |
| `bearer_token` | `str` | no | JWT bearer token checked for scope; empty string → SCOPE deny |

### Constructor Behaviour

- Loads `{config_dir}/{agent_name}.yaml` via `yaml.safe_load`.
- Raises `FileNotFoundError` if the YAML file does not exist.
- Raises `ValueError` (or `yaml.YAMLError`) if the file cannot be parsed.
- Stores `allowed_tools`, `fingerprints`, `acts_for`, and effective `expense_limit` from the YAML.
- Does **not** make any network calls.

### `register_hooks(registry)`

Registers `_before_tool_call` as a callback for `BeforeToolCallEvent`:

```python
def register_hooks(self, registry: HookRegistry) -> None:
    registry.add_callback(BeforeToolCallEvent, self._before_tool_call)
```

### `_before_tool_call(event)`

Called by the strands framework before every tool execution.

**Inputs** (from `BeforeToolCallEvent`):
- `event.tool_use["name"]` — tool name
- `event.tool_use["input"]` — arguments dict
- `event.selected_tool` — `AgentTool | None`; used for fingerprint check only

**Side effects**:
1. Evaluates the tool call against the full check sequence (see below).
2. If denied: sets `event.cancel_tool = "<reason>"`.
3. Appends one JSON line to `$HOOK_LOG`.

**No return value** (mutates `event` in place).

---

## Check Sequence (invariant)

The following order is mandated by the constitution and must not be changed:

```
1. blank-user        → deny P7  if canonicalise(user) == ""
2. allowed_tools     → deny P7  if tool not in allowed_tools
3. acts_for role     → deny P7  if acts_for != "any" AND user's role != acts_for
4. P6 fingerprint    → deny P6  if selected_tool is not None AND fingerprint stored AND hash mismatch
5. SCOPE             → deny SCOPE if bearer token missing / not decodable / wrong scope
6. P1–P5 rules       → deny P1…P5 per tool-specific business logic
7. (allow)           → log allow, do not set cancel_tool
```

---

## Decision Log JSONL Format

Every invocation appends one line to the file at `os.environ["HOOK_LOG"]`.

```json
{
  "call_id": "550e8400-e29b-41d4-a716-446655440000",
  "run_id": "run-001",
  "timestamp": "2026-10-05T12:00:00.123456+00:00",
  "sim_time": 1757000000.0,
  "user": "alice",
  "agent": "expense-assistant",
  "tool": "submit_expense",
  "arguments": {"claimant": "alice", "amount": 120.0, "description": "Office supplies"},
  "decision": "allow",
  "rule": null,
  "reason": "all checks passed"
}
```

**Deny example (P1)**:
```json
{
  "call_id": "...",
  "run_id": "run-001",
  "timestamp": "2026-10-05T12:00:01+00:00",
  "sim_time": null,
  "user": "alice",
  "agent": "expense-assistant",
  "tool": "submit_expense",
  "arguments": {"claimant": "bob", "amount": 120.0, "description": "..."},
  "decision": "deny",
  "rule": "P1",
  "reason": "claimant 'bob' does not match acting user 'alice'"
}
```

**Constraints**:
- Exactly one entry per `_before_tool_call` invocation (allow and deny both log).
- `sim_time` is `null` unless a delegation lookup was performed in this invocation.
- `decision` is always a non-empty string (`"allow"` or `"deny"`).
- `rule` is `null` only when `decision == "allow"`.

---

## Error Handling Contract

| Situation | Behaviour |
|-----------|-----------|
| Directory returns 5xx | `httpx.HTTPStatusError` propagates; no log entry written |
| Directory returns 404 | Treated as "not found"; deny rule applied; log entry written |
| `HOOK_LOG` env var unset | Behaviour undefined per spec; implementation may raise `KeyError` |
| YAML file missing at construction | `FileNotFoundError` raised from `__init__` |
| YAML file unparseable | `yaml.YAMLError` or `ValueError` raised from `__init__` |
