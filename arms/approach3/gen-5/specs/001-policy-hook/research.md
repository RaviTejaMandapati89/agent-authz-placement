# Research: PolicyHook — Phase 0 Findings

## 1. Strands Hook API

**Decision**: Use `strands.hooks.HookProvider` as base class; register via `registry.add_callback(BeforeToolCallEvent, self._before_tool_call)`.

**Rationale**: `BeforeToolCallEvent` is the only hook event fired before a tool executes. Setting `event.cancel_tool = "reason"` cancels execution and surfaces a tool-result error to the agent. This is the correct mechanism to deny a tool call without raising an exception from within the hook.

**Key fields on `BeforeToolCallEvent`** (from `strands/hooks/events.py`):
- `event.tool_use: ToolUse` — TypedDict with `name: str`, `input: Any`, `toolUseId: str`
- `event.selected_tool: AgentTool | None` — has `.tool_spec: ToolSpec` with `name`, `description`, `inputSchema`; may be `None` if tool lookup failed
- `event.cancel_tool: bool | str` — set to a non-empty string to cancel the tool call

**Alternatives considered**: Raising an exception from `_before_tool_call` — rejected because it bubbles as an unhandled framework error rather than a clean tool-result cancellation.

---

## 2. JWT Decode Without Signature Verification

**Decision**: Use `jwt.decode(token, options={"verify_signature": False}, algorithms=["HS256", "RS256", "ES256"])`.

**Rationale**: The spec assumption states tokens are decoded without signature verification. `PyJWT >= 2.x` requires an explicit `algorithms` list even when `verify_signature=False`. Listing the algorithms present in the test environment (ES256 from `domain/tokens.py`) plus common extras avoids `DecodeError` on algorithm mismatch. Malformed tokens that cannot be parsed at all by PyJWT will raise `jwt.DecodeError` or `jwt.InvalidTokenError`, which map to a SCOPE deny.

**Scope check**: After decoding, check `required_scope = TOOL_SCOPE_MAP.get(tool_name)`. If a required scope exists, verify it appears in `claims.get("scope", "").split()`. Missing or non-matching → SCOPE deny.

**Alternatives considered**: Full signature verification using `domain/tokens.py`'s private keys — rejected because `checkpoint.py` cannot depend on in-process key material; it receives only the opaque token string at construction.

---

## 3. Fingerprint Canonical Form

**Decision**: `sha256((spec["name"] + spec["description"] + json.dumps(spec["inputSchema"], sort_keys=True, separators=(",", ":"))).encode()).hexdigest()`

**Rationale**: The spec says SHA-256 over `name`, `description`, and `inputSchema`. `json.dumps` with `sort_keys=True, separators=(",",":")` produces a deterministic, compact encoding regardless of dict insertion order. Concatenating all three fields without a separator is safe because the JSON encoding of `inputSchema` is unambiguous. The pre-computed fingerprints in the agent YAML files were produced by this same formula.

**Alternatives considered**:
- Separating fields with `\n` — unnecessary if the canonical form is defined consistently.
- Using `repr()` for the schema — rejected because it is Python-version-dependent.

**P6 skip conditions** (from spec §4 step 4): skip entirely when `event.selected_tool is None` **or** when no fingerprint is stored for the tool name in the YAML.

---

## 4. Directory HTTP Access

**Decision**: Use synchronous `httpx.Client` (or module-level `httpx.get/post`) for all directory calls. Do not use async httpx.

**Rationale**: `_before_tool_call` is a synchronous callback (strands hook registry invokes it synchronously). Async httpx inside a sync function would require a new event loop, which is fragile. Synchronous httpx is the right match.

**5xx handling**: After a directory response, check `if response.status_code >= 500: response.raise_for_status()`. This raises `httpx.HTTPStatusError`, which propagates to the caller unchanged (spec FR-007). The hook has no `try/except` around directory calls.

**404 handling**: `if response.status_code == 404:` apply the matching deny rule per the spec.

**sim_time tracking**: Only `GET /directory/delegations` returns a `sim_time` field. For all other routes (users, vendors, expenses), no sim_time is available. `sim_time` in the log entry is set from the delegations response when P4 is evaluated; it is `None` for all other decision paths. If multiple directory calls happen in a single evaluation (e.g., P3 fetches both expense and user), and none return sim_time, the log entry has `sim_time: null`.

**Alternatives considered**: Caching user/vendor data per call to reduce HTTP round-trips — explicitly prohibited by FR-006.

---

## 5. Log Entry Construction

**Decision**: Assemble the log entry as a dict and append as a single JSON line to `open(os.environ["HOOK_LOG"], "a")` at the end of `_before_tool_call`, in every code path (early deny and allow).

**Fields**:
| Field | Source |
|-------|--------|
| `call_id` | `uuid.uuid4()` generated at start of `_before_tool_call` |
| `run_id` | `self._run_id` (constructor param) |
| `timestamp` | `datetime.datetime.now(datetime.timezone.utc).isoformat()` |
| `sim_time` | From directory delegations response, or `None` |
| `user` | `self._user` |
| `agent` | `self._agent_name` |
| `tool` | `event.tool_use["name"]` |
| `arguments` | `event.tool_use["input"]` |
| `decision` | `"allow"` or `"deny"` |
| `rule` | Rule code (`"P1"`–`"P7"`, `"SCOPE"`) or `None` for allow |
| `reason` | Human-readable explanation string |

**Alternatives considered**: Structured logging via the `logging` module — rejected because the spec requires a specific JSONL file at `$HOOK_LOG`, not Python log streams.

---

## 6. YAML Config Loading

**Decision**: In `__init__`, load `{config_dir}/{agent_name}.yaml` using `yaml.safe_load`. Raise a clear `FileNotFoundError` or `ValueError` if the file is absent or unparseable (edge case: YAML file missing).

**`config_dir` default**: `pathlib.Path(__file__).parent / "config" / "agents"` — this resolves to `config/agents/` relative to `checkpoint.py` at repo root.

**Effective expense limit**: `limit_override` if not `None`, otherwise `config["expense_limit"]` if present, otherwise unbounded (treat as no limit).

**Alternatives considered**: Loading the YAML on each tool call — rejected because FR-002 specifies construction-time loading.

---

## 7. acts_for Role Check

**Decision**: If `acts_for == "any"` (from YAML), skip the role check entirely. Otherwise fetch `GET /directory/users/{user}` and compare `response["role"]` against `acts_for`. Deny with P7 on mismatch or 404.

**Rationale**: Matches spec §4 step 3 and constitution §P7. The `acts_for: finance` in `payments-agent.yaml` means only users with `role: finance` may use that agent's tools.

**Alternatives considered**: Role embedded in the bearer token — the spec is clear that the directory is the authoritative source; token claims are not used for role checks.
