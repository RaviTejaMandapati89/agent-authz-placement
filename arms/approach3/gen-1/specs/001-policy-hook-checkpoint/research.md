# Research: PolicyHook Checkpoint Enforcement

**Phase 0 output for feature `001-policy-hook-checkpoint`**

## Decision 1: JWT Scope Verification Strategy

**Decision**: Decode the bearer token using `jwt.decode` with `options={"verify_signature": False, "verify_exp": False, "verify_aud": False}` to extract the `scope` claim for enforcement; do not verify the signature inside `checkpoint.py`.

**Rationale**: The `domain/tokens.py` module generates EC P-256 key pairs **in-process at import time** and never writes them to disk. `checkpoint.py` has no reliable path to the public key at call time without importing `domain.tokens`, which would create an import-time side-effect (key generation) in a policy module. The spec explicitly states "key management and issuance are out of scope for checkpoint.py". Scope enforcement — verifying the `scope` claim value — is the policy goal; signature verification of the JWT is the identity layer's job, done upstream before the token reaches the agent.

**Alternatives considered**:
- Import `domain.tokens.verify_bearer` — rejected because `verify_bearer` requires an `act` claim that non-delegated calls lack, and it generates keys at import time as a side-effect.
- Require a JWKS URL parameter — rejected because it adds infrastructure coupling the spec explicitly excludes.

## Decision 2: Fingerprint Serialization Format

**Decision**: Compute `hashlib.sha256(json.dumps({"description": spec["description"], "inputSchema": spec["inputSchema"], "name": spec["name"]}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()` where `spec = event.selected_tool.tool_spec`.

**Rationale**: The `ToolSpec` TypedDict has exactly three review-relevant fields: `name`, `description`, and `inputSchema`. Using `sort_keys=True` and compact separators `(",", ":")` produces a canonical, whitespace-free serialization. Keys are fixed (only three) so `sort_keys` is deterministic. `ensure_ascii=False` preserves any Unicode in descriptions. The fingerprints already stored in `config/agents/*.yaml` (64-char hex) confirm SHA-256 hex output format.

**Alternatives considered**:
- Hash each field separately and concatenate — rejected because order-dependent and not reproducible across implementations.
- Use `repr()` — rejected because language-version-dependent.

## Decision 3: sim_time Extraction from Directory Responses

**Decision**: Initialize `sim_time = None` before any directory calls. After each HTTP response, attempt `response.json().get("sim_time")` and update `sim_time` if a non-None value is found. Use the last collected value in the log entry.

**Rationale**: Only `GET /directory/delegations` includes a `sim_time` field in its response. `GET /directory/users/{name}`, `GET /directory/vendors`, and `GET /directory/expenses/{id}` do not return `sim_time`. The spec says "sim_time from the directory response when a lookup was made; null when no directory call was needed". For calls where only user/vendor/expense lookups occur, `sim_time` will remain `null` (as the spec intends for the lookup case). This approach collects the value opportunistically without hard-coding which route provides it.

**Alternatives considered**:
- Always call simclock directly — rejected because it would circumvent the intent of recording what the directory reported (not what the local clock says).
- Only extract from delegations responses — rejected because this would need updating if other routes gain `sim_time` later.

## Decision 4: HookProvider Registration Pattern

**Decision**: Subclass `HookProvider` from `strands.hooks`; override `register_hooks(self, registry: HookRegistry) -> None` to call `registry.add_callback(BeforeToolCallEvent, self._before_tool_call)`.

**Rationale**: This is the canonical pattern shown in the `strands.hooks` module docstring. `BeforeToolCallEvent` has `selected_tool`, `tool_use` (dict with `name`, `input`, `toolUseId`), and `cancel_tool` (settable to a string to cancel the call). Setting `event.cancel_tool` to a non-empty string triggers cancellation.

**Alternatives considered**:
- Using `BeforeToolsEvent` to cancel the entire batch — rejected because P6 and SCOPE need per-tool evaluation.

## Decision 5: HOOK_LOG Absence Handling

**Decision**: If `os.environ.get("HOOK_LOG")` is `None` or empty, skip the file write entirely (no-op). The evaluation still proceeds and makes its allow/deny decision; only logging is skipped.

**Rationale**: The spec says "if unset, the module handles the absence gracefully without crashing". Writing to `/dev/null` or a tempfile is unnecessary complexity when a no-op is sufficient.

## Decision 6: event.tool_use Access Pattern

**Decision**: Access tool name via `event.tool_use["name"]` and arguments via `event.tool_use["input"]` (or `event.tool_use.get("input", {})` for safety). `ToolUse` is a `TypedDict` (i.e., a dict at runtime).

**Rationale**: `BeforeToolCallEvent.tool_use` is typed as `ToolUse`, which is a `TypedDict` with keys `name`, `input`, `toolUseId`. Tests use plain dict literals for `tool_use`, confirming runtime dict access. The `event.selected_tool.tool_name` is available but only when `selected_tool is not None`.

## Decision 7: Default config_dir Path

**Decision**: Default to `pathlib.Path(__file__).parent / "config" / "agents"` when `config_dir` is not provided.

**Rationale**: The spec says "a `config/agents/` directory relative to the module file". `checkpoint.py` will live in the project root (`/checkpoint-gen/run-1/`), so the default path resolves to `/checkpoint-gen/run-1/config/agents/`, which is exactly where the YAML files are stored (`config/agents/expense-assistant.yaml`, etc.).

## Resolved NEEDS CLARIFICATION items

| Item | Resolution |
|------|------------|
| Python version | 3.12 (`.venv`); spec requires ≥ 3.11 |
| strands HookProvider location | `strands.hooks.HookProvider`, `strands.hooks.BeforeToolCallEvent` |
| AgentTool.tool_spec fields | `ToolSpec` TypedDict: `name`, `description`, `inputSchema` (required); `outputSchema`, `annotations` (optional) |
| sim_time source | Only `GET /directory/delegations` returns `sim_time`; others do not |
| JWT verification depth | Soft decode (no signature) for scope check only |
| Directory vendors response format | Plain JSON array of strings (no sim_time, no wrapper object) |
