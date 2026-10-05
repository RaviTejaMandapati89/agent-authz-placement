# Contract: PolicyHook Class

**Module**: `checkpoint.py` (project root)

---

## Class Signature

```python
from strands.hooks import HookProvider, HookRegistry
from strands.hooks.events import BeforeToolCallEvent
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

    def _before_tool_call(self, event: BeforeToolCallEvent) -> None: ...
```

---

## Constructor Contract

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `agent_name` | `str` | yes | Identifies which agent config YAML to load and which name to use in log entries |
| `user` | `str` | yes | Acting user for this session; used in all policy checks and log entries |
| `base_url` | `str` | yes | Directory HTTP base URL (no trailing slash), e.g. `http://127.0.0.1:8765` |
| `run_id` | `str \| None` | no | Run identifier for log correlation; `None` is valid |
| `limit_override` | `int \| None` | no | Overrides `expense_limit` from config when set; `None` = use config value |
| `config_dir` | `pathlib.Path \| None` | no | Directory containing `{agent_name}.yaml`; defaults to `<module_dir>/config/agents` |
| `bearer_token` | `str` | no | JWT bearer token for SCOPE checks; default `""` triggers SCOPE deny for any scoped tool |

**Post-conditions**:
- Agent config YAML loaded and validated; `FileNotFoundError` if config file missing
- `allowed_tools`, `fingerprints`, `acts_for`, effective `expense_limit` stored as instance state
- No network calls occur during construction

---

## register_hooks Contract

```python
def register_hooks(self, registry: HookRegistry) -> None:
    registry.add_callback(BeforeToolCallEvent, self._before_tool_call)
```

**Post-conditions**:
- `BeforeToolCallEvent` has exactly one callback registered: `self._before_tool_call`
- No other event types registered

---

## _before_tool_call Contract

**Trigger**: Called by the Strands framework before every tool invocation.

**Input**:
- `event.tool_use` — dict-like with keys `"name"` (tool name) and `"input"` (arguments dict)
- `event.selected_tool` — `AgentTool | None`; `None` if tool lookup failed
- `event.cancel_tool` — mutable; set to a non-empty string to cancel/deny the call

**Behaviour**:
1. Evaluates all six rule phases in fixed order (see data-model.md check-order state machine)
2. Appends exactly one JSONL log entry to `$HOOK_LOG` before returning
3. On deny: sets `event.cancel_tool = "<reason>"` and returns (does not raise)
4. On allow: leaves `event.cancel_tool` unchanged (falsy) and returns
5. On internal error: logs `decision = "error"` then re-raises

**Log entry**: Written for every outcome (allow, deny, error) — see [log-entry-schema.md](./log-entry-schema.md).

**5xx propagation**: `httpx.HTTPStatusError` for 5xx responses is not caught; it propagates
through `_before_tool_call` to the Strands framework. A `decision = "error"` log entry is
written before propagation.

---

## Usage Example

```python
import pathlib
from checkpoint import PolicyHook
from strands import Agent

hook = PolicyHook(
    agent_name="expense-assistant",
    user="alice",
    base_url="http://127.0.0.1:8765",
    run_id="run-001",
    bearer_token=my_jwt_token,
    config_dir=pathlib.Path("config/agents"),
)

agent = Agent(hooks=[hook], ...)
```
