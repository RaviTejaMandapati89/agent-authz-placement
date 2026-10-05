# Research: PolicyHook Checkpoint Module

**Phase**: 0 | **Branch**: `001-policyhook-checkpoint` | **Date**: 2026-10-05

All decisions below were resolved by inspecting the `.venv` SDK source, the `domain/` kit files,
the existing `tests/test_contract.py`, and `spec_input.md`. No unknowns remain.

---

## Decision 1 — BeforeToolCallEvent Cancellation Mechanism

**Decision**: Set `event.cancel_tool = "<reason_string>"` to deny a tool call.

**Rationale**: Inspecting
`.venv/lib/python3.12/site-packages/strands/hooks/events.py` shows that
`BeforeToolCallEvent` inherits `_Interruptible` and exposes a `cancel_tool: bool | str = False`
field. Setting it to a non-empty string cancels the tool invocation and surfaces the string as the
error message. No return value is used; the callback is registered via
`HookRegistry.add_callback(BeforeToolCallEvent, callback)` and invoked with the event as sole
argument.

**Alternatives considered**:
- Raising an exception from the callback — not supported; would propagate as an unhandled error
  rather than a policy deny.
- Returning a value — the callback return value is ignored by the registry.

---

## Decision 2 — JWT Decode Strategy (SCOPE Check)

**Decision**: Decode bearer tokens with `jwt.decode(token, options={"verify_signature": False})`
to extract the `scope` claim without verifying the cryptographic signature.

**Rationale**: The spec states "Bearer token JWT decoding uses the PyJWT library without
signature verification (scope check only)." The checkpoint does not have access to the issuer's
public key at runtime (keys are generated in-process by `domain/tokens.py` during tests and
never written to disk). Using `verify_signature=False` is the correct no-verification mode in
PyJWT ≥ 2.x; it still validates structure and raises `jwt.DecodeError` for malformed tokens.

**Alternatives considered**:
- Full signature verification — requires distributing public keys to the checkpoint; not viable
  given the spec constraint that keys are ephemeral.
- Inspecting the `domain/tokens.py` `verify_bearer` helper — this function requires an `act`
  claim and performs expiry checks against `simclock`, making it unsuitable as the scope-only
  gate in the checkpoint.

---

## Decision 3 — Fingerprint Computation

**Decision**: Compute `hashlib.sha256(json.dumps({"description": spec["description"], "inputSchema": spec["inputSchema"], "name": spec["name"]}, sort_keys=True).encode()).hexdigest()` where `spec = event.selected_tool.tool_spec`.

**Rationale**: `AgentTool.tool_spec` is a `ToolSpec` TypedDict (defined in
`strands/types/tools.py`) with fields `name: str`, `description: str`, and
`inputSchema: JSONSchema`. The spec defines the fingerprint as "SHA-256 hash over the tool's
`name`, `description`, and `inputSchema`." Using `sort_keys=True` ensures deterministic
serialisation regardless of dict insertion order.

**Alternatives considered**:
- Using `tool_name` property separately — it is identical to `tool_spec["name"]`; using
  `tool_spec` directly is authoritative.
- Including `outputSchema` or `annotations` — not specified; including extra fields would
  invalidate stored fingerprints.

---

## Decision 4 — httpx Client Lifecycle

**Decision**: Store a single `httpx.Client(base_url=base_url)` on the PolicyHook instance;
reuse it for all directory calls within the instance's lifetime.

**Rationale**: `_before_tool_call` is a synchronous callback; async HTTP is not applicable.
A shared client avoids per-request connection overhead. The client is created at construction
time alongside config loading.

**Alternatives considered**:
- Per-request `httpx.get(url)` calls — simpler but creates a new connection pool on every
  tool call, wasteful for high-frequency agents.
- Context manager per call — incompatible with a synchronous callback that does not own
  a lifecycle boundary.

---

## Decision 5 — HOOK_LOG Behaviour When Unset

**Decision**: Raise `RuntimeError` with a clear message if `HOOK_LOG` is not set in the
environment when a log write is attempted.

**Rationale**: Constitution Principle IV ("No Silent Failures") requires that every evaluation
produce a log entry. Silently discarding the log entry would violate the constitution. The spec
states the variable "will be set to a writable file path in production" and that behaviour when
unset "may raise or no-op." Raising loudly is consistent with the no-silent-failures principle
and makes misconfiguration visible immediately.

**Alternatives considered**:
- Writing to stderr — does not satisfy "appended to the file path held in `HOOK_LOG`" semantics
  and is invisible to monitoring infrastructure.
- No-op — violates Constitution Principle IV.

---

## Decision 6 — acts_for 404 Handling

**Decision**: If `GET /directory/users/{user}` returns 404 during the acts_for check, deny
with P7 (user not found in directory → role cannot be confirmed → deny).

**Rationale**: The spec says "on 404, treat as not found and apply the deny rule for the
relevant check." For the acts_for check the relevant check is P7 (role mismatch / unknown user).

---

## Decision 7 — sim_time Population

**Decision**: `sim_time` is taken from the first directory response that includes a `sim_time`
field. The `/directory/delegations` route returns `{"delegations": [...], "sim_time": ...}`.
For routes that do not return a `sim_time` (users, vendors, expenses), set `sim_time = None`.
If multiple directory calls are made, use the last non-null `sim_time` observed.

**Rationale**: The spec says populate `sim_time` "from the directory response when a lookup was
made." Only the delegations route explicitly returns a `sim_time`. Setting null for other routes
is the safe, spec-compliant default.

---

## Decision 8 — Canonical Import Path for Kit Files

**Decision**: Import as `from domain.identity import canonicalise` and
`from domain.scopes import TOOL_SCOPE_MAP`. These are importable because `checkpoint.py` lives
at the repo root alongside the `domain/` package.

**Rationale**: `domain/__init__.py` exists (verified by the Explore agent); `domain/identity.py`
exports `canonicalise(s: str) -> str`; `domain/scopes.py` exports
`TOOL_SCOPE_MAP: dict[str, str]`.

---

## Summary Table

| # | Topic | Decision |
|---|-------|----------|
| 1 | Deny mechanism | `event.cancel_tool = reason_string` |
| 2 | JWT decode | `jwt.decode(..., options={"verify_signature": False})` |
| 3 | Fingerprint | SHA-256 over `json.dumps(tool_spec_fields, sort_keys=True)` |
| 4 | httpx client | Shared `httpx.Client` on instance, created at construction |
| 5 | HOOK_LOG unset | Raise `RuntimeError` |
| 6 | acts_for 404 | Deny P7 (user not found) |
| 7 | sim_time | From delegations response; `null` for other routes |
| 8 | Kit imports | `from domain.identity import canonicalise`, `from domain.scopes import TOOL_SCOPE_MAP` |
