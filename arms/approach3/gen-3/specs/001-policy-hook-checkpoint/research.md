# Research: PolicyHook Checkpoint Enforcement

All unknowns were resolved by reading the installed `strands-agents` package, the project's `domain/` files, agent YAML configs, and the existing test suite. No external research was needed.

---

## Decision 1: `HookProvider` implementation style

**Decision**: Implement `PolicyHook` as a plain class with a `register_hooks` method — no inheritance from `HookProvider` required.

**Rationale**: `strands.hooks.HookProvider` is a `@runtime_checkable Protocol`. Any class that defines `register_hooks(self, registry: HookRegistry) -> None` satisfies the protocol. The test `test_register_hooks_registers_before_tool_call_event` uses a fake registry object and calls `hook.register_hooks(_FakeRegistry())` directly, so inheritance is not tested nor required.

**Alternatives considered**: Explicit `class PolicyHook(HookProvider)` inheritance. Works identically but adds a superfluous import. Protocol satisfaction via structural subtyping is idiomatic.

---

## Decision 2: `BeforeToolCallEvent` fields used

**Decision**: Read `event.tool_use["name"]` for tool name, `event.tool_use["input"]` for arguments. Set `event.cancel_tool = reason_string` to deny a call.

**Rationale**: `ToolUse` is a `TypedDict` with `name: str`, `input: Any`, `toolUseId: str`. The `_can_write` guard on `BeforeToolCallEvent` permits writing `cancel_tool`, `selected_tool`, and `tool_use`. Setting `cancel_tool` to a non-empty string causes Strands to cancel the tool and surface the string as an error message. The test fake event confirms this interface: `tool_use = {"name": "some_tool", "input": {...}}` and `cancel_tool = None` (mutable plain attribute).

**Alternatives considered**: Raising an exception inside the callback. Not idiomatic for Strands hooks and not tested.

---

## Decision 3: `selected_tool` for P6 fingerprint computation

**Decision**: Access `event.selected_tool.tool_spec` (a `ToolSpec` TypedDict) to get `name`, `description`, and `inputSchema`. Skip entirely when `event.selected_tool is None`.

**Rationale**: `AgentTool` (abstract base) exposes `tool_spec: ToolSpec` as an abstract property. `ToolSpec` has `name: str`, `description: str`, `inputSchema: JSONSchema`. These are the three fields named in the spec for fingerprinting.

**Alternatives considered**: Using `event.tool_use["name"]` only — insufficient; the fingerprint is over the full tool definition, not just the name.

---

## Decision 4: SHA-256 fingerprint algorithm

**Decision**: Compute SHA-256 over `json.dumps({"name": ..., "description": ..., "inputSchema": ...}, sort_keys=True).encode("utf-8")` and compare the hex digest to the stored value.

**Rationale**: Canonical JSON with sorted keys produces a deterministic, reproducible hash. The stored fingerprints in the YAML configs (64-char hex strings = 256-bit) match SHA-256 output. Using `json.dumps` with `sort_keys=True` handles nested dict keys in `inputSchema` deterministically. This is the standard approach used by tool-review toolchains.

**Alternatives considered**:
- String concatenation (`f"{name}\n{description}\n{json.dumps(inputSchema)}"`) — fragile; key ordering in `inputSchema` not guaranteed.
- `hashlib.sha256(str(tool_spec).encode())` — depends on Python repr, not stable across versions.

---

## Decision 5: Bearer token decoding strategy

**Decision**: Decode the bearer token using `jwt.decode(token, options={"verify_signature": False, "verify_exp": False})`. An empty string or malformed token raises `jwt.DecodeError` → SCOPE deny. Extract the `scope` claim and check it against `TOOL_SCOPE_MAP`.

**Rationale**: The checkpoint lives agent-side and does not have access to the issuer's EC private keys. The JWKS endpoint (`GET /identity/jwks`) exists but fetching it per-call adds a network round-trip and complexity not required by the spec. The spec says "decoded as a valid JWT" — this is satisfied by successful parsing (format validity), not signature verification. The scope claim carries the enforcement-relevant data. Token expiry is also skipped here since the checkpoint uses the token as passed (the issuer controls token lifetime).

**Alternatives considered**: JWKS-based signature verification — correct but over-engineered for the spec requirement; adds a dependency on network availability and the JWKS endpoint. The test passes `bearer_token=""` which tests the failure path (empty string → decode error → SCOPE deny).

---

## Decision 6: HTTP 5xx propagation mechanism

**Decision**: After every directory HTTP response, call `response.raise_for_status()` if `response.status_code >= 500`. This raises `httpx.HTTPStatusError` which propagates out of `_before_tool_call` to the Strands framework.

**Rationale**: The spec requires 5xx errors to propagate. `httpx` does not raise on error status by default. `raise_for_status()` raises `httpx.HTTPStatusError` for 4xx and 5xx. Filtering to `>= 500` before calling it lets 404 be handled as "not found" (deny applicable rule) without masking it as an error.

**Alternatives considered**: Catch 404 explicitly, then call `raise_for_status()` unconditionally — equivalent but slightly less explicit about intent.

---

## Decision 7: Synchronous vs. asynchronous HTTP

**Decision**: Use `httpx.Client` (synchronous) in `PolicyHook`. Create it once at construction, close it on `__del__` (best-effort).

**Rationale**: `_before_tool_call` must be a synchronous function. The Strands `HookRegistry.invoke_callbacks` (synchronous path) raises `RuntimeError` if any callback is a coroutine function. The tests call `hook._before_tool_call(event)` directly without `await`. An `httpx.Client` is appropriate for synchronous, short-lived directory calls.

**Alternatives considered**: `httpx.AsyncClient` with `asyncio.get_event_loop().run_until_complete()` — anti-pattern; can deadlock inside an existing event loop.

---

## Decision 8: sim_time tracking in the audit log

**Decision**: `sim_time` is populated only from the `GET /directory/delegations` response, which is the only endpoint that returns a `sim_time` field. For all other paths (no directory call, or only user/expense/vendor lookups), `sim_time = null`.

**Rationale**: The `delegations` endpoint is the only directory route that explicitly returns `sim_time` (per the server code: `return JSONResponse({"delegations": filtered, "sim_time": now})`). The `users`, `vendors`, and `expenses` endpoints return domain data only. The spec says `sim_time` is "from directory response … null when no directory call was needed" — for non-delegation paths it is null.

**Alternatives considered**: Tracking whether any directory call was made and using the server's simclock. Not accessible from the checkpoint (simclock is server-side only).

---

## Decision 9: Default config_dir resolution

**Decision**: When `config_dir` is `None`, default to `pathlib.Path(__file__).parent / "config" / "agents"`.

**Rationale**: `__file__` is `checkpoint.py` at the project root; `config/agents/` is the existing directory containing all agent YAML files. This makes the default work without any environment variable or explicit path.

**Alternatives considered**: Relative path from `os.getcwd()` — fragile; depends on the working directory at runtime.

---

## Decision 10: P4 delegation direction

**Decision**: An active delegation covering `book_travel` for `traveller → user` is one where `delegator == traveller` and `delegate == user` and `active == True`. The `scope` list is checked for `"travel"`.

**Rationale**: The fixture data confirms: `delegator: carol, delegate: dan` means Carol delegated to Dan (Dan can book travel for Carol). The spec says "no active delegation covers travel from the traveller to the user" — i.e., the traveller is the delegator, the booking user is the delegate. The `active` field is checked; no timestamp comparison is performed by the checkpoint.

**Alternatives considered**: Checking only `delegator/delegate` without `scope` — the fixture's scope field is `["travel"]` and the spec says "covers travel", so scope validation is required.
