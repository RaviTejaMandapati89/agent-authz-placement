# Research: PolicyHook Checkpoint Enforcement

**Branch**: `001-policy-hook-checkpoint` | **Date**: 2026-10-05

All unknowns resolved from inspecting `.venv` and project source. No external research needed.

---

## R-001: Strands HookProvider and BeforeToolCallEvent Interface

**Decision**: Extend `strands.hooks.HookProvider` and register `_before_tool_call` as a callback
for `BeforeToolCallEvent` via `registry.add_callback(BeforeToolCallEvent, self._before_tool_call)`.

**Rationale**: `HookProvider` is an abstract base class requiring `register_hooks(registry:
HookRegistry) -> None`. `BeforeToolCallEvent` is the sole event relevant to pre-call enforcement.

**Key attributes of `BeforeToolCallEvent`** (confirmed from
`.venv/.../strands/hooks/events.py`):
```python
@dataclass
class BeforeToolCallEvent(HookEvent[_LocalAgentT], _Interruptible):
    selected_tool: AgentTool | None   # tool definition object; None if lookup failed
    tool_use: ToolUse                  # TypedDict: {"name": str, "input": dict, "toolUseId": str}
    invocation_state: dict[str, Any]
    cancel_tool: bool | str = False    # set to a string reason to block the call
```

To deny a call: set `event.cancel_tool = "<reason string>"`. The framework places the string
into a tool result with error status and does not execute the tool.

**Access pattern in `_before_tool_call`**:
```python
tool_name = event.tool_use["name"]     # or event.tool_use.get("name") for safety
arguments = event.tool_use.get("input", {})
selected_tool = event.selected_tool    # AgentTool | None
```

**Alternatives considered**: `BeforeToolsEvent` fires once per batch (not per tool). Rejected
because enforcement must be per-tool with granular per-call logging.

---

## R-002: AgentTool Fingerprint Field Sources

**Decision**: Compute SHA-256 over `json.dumps({"name": ..., "description": ...,
"inputSchema": ...}, sort_keys=True).encode()` using fields from `selected_tool.tool_spec`.

**Rationale**: `AgentTool.tool_spec` returns a `ToolSpec` TypedDict with fields `name`,
`description`, `inputSchema` (confirmed from `.venv/.../strands/types/tools.py`). The
`spec_input.md` specifies fingerprinting over these three fields. Using `sort_keys=True` on JSON
serialization ensures deterministic output.

```python
import hashlib, json

def _fingerprint(tool: AgentTool) -> str:
    spec = tool.tool_spec
    payload = json.dumps(
        {"name": spec["name"], "description": spec["description"],
         "inputSchema": spec["inputSchema"]},
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()
```

**Stored fingerprint format** (from `config/agents/expense-assistant.yaml`):
```yaml
fingerprints:
  read_receipt: 80d48b7490ae5b989880a39a257d8eadf182d3d6bed8e55783b8560a8912eabb
```
Hex string — matches `hashlib.sha256(...).hexdigest()`.

**Skip conditions** (FR-006): `event.selected_tool is None` OR no fingerprint entry for the
tool name in the agent config.

**Alternatives considered**: Fingerprinting only the description — rejected, spec is explicit
that `name + description + inputSchema` are all included.

---

## R-003: Bearer Token Decode and Scope Check

**Decision**: Use `jwt.decode()` from `PyJWT` with `options={"verify_signature": False}` to
extract claims when no JWKS is available at checkpoint construction time, or with full
verification if the issuer's public key is available. Scope is extracted from the `"scope"` claim
as a space-separated string.

**Rationale**: The spec says "if the bearer token is missing (empty string) or cannot be decoded
as a valid JWT, deny with SCOPE." The token is passed by the harness which signs it with the
project's JWKS. However, the checkpoint module cannot assume the JWKS endpoint is reachable at
construction time (it would create a circular dependency with the directory base URL). The
simplest compliant approach: attempt `jwt.decode(..., options={"verify_signature": False})` to
extract the `scope` claim; if that raises any exception (malformed token), deny with SCOPE.

**Token absence**: Empty string `bearer_token` → deny with SCOPE immediately (no decode attempt).

**Scope lookup**: `TOOL_SCOPE_MAP` from `domain.scopes` maps tool names to required scope strings.
If the tool is not in the map (no scope requirement defined), the SCOPE check passes. If the
decoded token's `scope` claim does not contain the required scope string, deny with SCOPE.

```python
from domain.scopes import TOOL_SCOPE_MAP

def _check_scope(bearer_token: str, tool_name: str) -> str | None:
    if not bearer_token:
        return "missing bearer token"
    try:
        claims = jwt.decode(bearer_token, options={"verify_signature": False})
    except Exception:
        return "invalid bearer token"
    required = TOOL_SCOPE_MAP.get(tool_name)
    if required and required not in claims.get("scope", "").split():
        return f"token missing scope {required!r}"
    return None  # pass
```

**Alternatives considered**: Full signature verification — requires JWKS fetch at check time,
adds a network call not in the spec's directory route table; rejected.

---

## R-004: httpx Synchronous Client Usage for Directory Calls

**Decision**: Use `httpx.get(url, ...)` (module-level sync functions) rather than a persistent
`Client`. The checkpoint is called synchronously from the Strands hook callback, and each call
must not cache state between evaluations.

**Rationale**: `httpx.get()` creates and closes a connection per call — consistent with the
"no caching" requirement. A persistent `Client` with connection pooling could inadvertently
cache TCP connections across calls, which is acceptable at the socket level but not at the
application level. The spec requires no fact be cached; connection reuse is fine.

**5xx propagation** (FR-014): Do not wrap httpx calls in try/except for HTTP status. Call
`response.raise_for_status()` — this raises `httpx.HTTPStatusError` for 4xx and 5xx. Catch
only `httpx.HTTPStatusError` with `response.status_code < 500` to handle 404 as "not found".
Let `HTTPStatusError` for 5xx propagate.

```python
resp = httpx.get(f"{base_url}/directory/users/{user}")
if resp.status_code == 404:
    return None  # user not found
resp.raise_for_status()  # propagates 5xx
return resp.json()
```

**Alternatives considered**: Async httpx — the `_before_tool_call` callback is synchronous in
the Strands framework (no `async def` in BeforeToolCallEvent callbacks); rejected.

---

## R-005: Agent Config YAML Loading

**Decision**: Load `{config_dir}/{agent_name}.yaml` at `__init__` time with `yaml.safe_load`.
Store as instance attributes for use in `_before_tool_call`.

**Config fields consumed**:
```yaml
agent:         str               # agent name (informational)
acts_for:      str               # "any" or role name
expense_limit: int | None        # optional; absent means no P2 limit check
allowed_tools: list[str]         # required; tools the agent may call
fingerprints:  dict[str, str]    # optional; tool name → hex SHA-256
```

**Default config_dir**: `pathlib.Path(__file__).parent / "config" / "agents"` — relative to the
checkpoint module file. This matches the existing project layout.

**Alternatives considered**: Re-reading the file on every call — rejected (spec says "initialized
per agent session"; FR-002 says "at initialization time").

---

## R-006: Log Entry — sim_time Field

**Decision**: `sim_time` is set to the `sim_time` field from any directory JSON response that
includes it. Only `GET /directory/delegations` returns a `sim_time` field. For all other checks
(no directory call, or directory calls that don't return `sim_time`), `sim_time` is `null`.

**Rationale**: The spec states "`sim_time`: Simulation clock time from the directory response
when a lookup was made; `null` when no directory call was needed for the decision." The only
route returning `sim_time` in the existing server code is `/directory/delegations`. The
implementation should collect `sim_time` from any directory response that provides it and carry
the last seen value into the log entry.

**Alternatives considered**: Fetching sim_time from a separate clock endpoint — rejected; spec
says it comes from the directory response, not a separate call.
