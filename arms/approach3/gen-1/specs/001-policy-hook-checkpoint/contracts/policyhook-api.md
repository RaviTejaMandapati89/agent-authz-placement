# Contract: PolicyHook Python API

**Feature**: `001-policy-hook-checkpoint`  
**Contract type**: Python module API

## Module Interface

`checkpoint.py` must expose exactly one public class:

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

    def _before_tool_call(self, event: BeforeToolCallEvent) -> None: ...
```

### Constructor Parameters

| Parameter | Type | Required | Default |
|-----------|------|----------|---------|
| `agent_name` | `str` | yes | — |
| `user` | `str` | yes | — |
| `base_url` | `str` | yes | — |
| `run_id` | `str \| None` | no | `None` |
| `limit_override` | `int \| None` | no | `None` |
| `config_dir` | `pathlib.Path \| None` | no | `Path(__file__).parent / "config" / "agents"` |
| `bearer_token` | `str` | no | `""` |

### Constructor Behaviour

- Loads `{config_dir}/{agent_name}.yaml` at construction time; raises if the file is missing or unparseable.
- If `limit_override` is provided, it replaces the `expense_limit` from the YAML.
- All config values (`allowed_tools`, `fingerprints`, `acts_for`, `expense_limit`) are read from the YAML; none are hard-coded.

### `register_hooks(registry)`

- MUST call `registry.add_callback(BeforeToolCallEvent, self._before_tool_call)`.
- Registers exactly one callback.

### `_before_tool_call(event)`

Evaluates all policy rules in order (see data-model.md § Check-Order State Machine):

1. If a deny rule fires: sets `event.cancel_tool` to a non-empty string and writes a deny log entry.
2. If all rules pass: writes an allow log entry; does NOT modify `event.cancel_tool`.
3. Writes exactly one log entry to `HOOK_LOG` per invocation.
4. HTTP 5xx from any directory call propagates out of `_before_tool_call` without being caught.

## Error Contracts

| Situation | Behaviour |
|-----------|-----------|
| Config file missing at construction | `FileNotFoundError` raised at `__init__` time |
| Config file invalid YAML at construction | `yaml.YAMLError` raised at `__init__` time |
| `event.selected_tool` is `None` | P6 fingerprint check is skipped entirely |
| Tool has no fingerprint entry in config | P6 fingerprint check is skipped for that tool |
| Bearer token is `""` (empty string) | SCOPE deny |
| Bearer token is not a valid JWT structure | SCOPE deny |
| Bearer token missing required scope | SCOPE deny |
| Directory returns HTTP 5xx | Exception propagates to caller unchanged |
| Directory returns HTTP 404 (user lookup) | Treated as "not found"; apply deny rule |
| `HOOK_LOG` env var not set | Log write skipped silently; evaluation continues |

## Log Entry Contract

See [decision-log-schema.json](decision-log-schema.json) for the JSON Schema.

Every call to `_before_tool_call` MUST append exactly one JSON line to the `HOOK_LOG` file (if set) with all required fields present and non-null (except `rule` which is `null` for allow decisions, and `sim_time` which is `null` when no directory call was made).
