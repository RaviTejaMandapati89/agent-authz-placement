---

description: "Task list for PolicyHook Checkpoint Enforcement implementation"
---

# Tasks: PolicyHook Checkpoint Enforcement

**Input**: Design documents from `/specs/001-policy-hook-checkpoint/`

**Prerequisites**: plan.md ✓, spec.md ✓, data-model.md ✓, contracts/ ✓, research.md ✓, quickstart.md ✓, constitution.md ✓

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different code sections, no blocking dependency)
- **[Story]**: Which user story this task belongs to (US1–US5)
- Exact file paths are included in all descriptions

## Path Conventions

Single-project flat layout: `checkpoint.py` at repository root, `tests/` at root.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create the `checkpoint.py` module skeleton so all subsequent tasks have a file to edit.

- [X] T001 Create `checkpoint.py` at project root with all required imports (`strands.hooks`, `strands.hooks.events`, `httpx`, `jwt`, `yaml`, `pathlib`, `uuid`, `json`, `datetime`, `hashlib`, `os`) and a `PolicyHook(HookProvider)` class skeleton with stub `__init__`, `register_hooks`, and `_before_tool_call` methods that raise `NotImplementedError`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before any user-story work. All subsequent tasks build on these primitives.

**⚠️ CRITICAL**: No user-story task can begin until this phase is complete.

- [X] T002 Implement `PolicyHook.__init__` in `checkpoint.py`: accept `agent_name: str`, `user: str`, `base_url: str`, `run_id: str | None = None`, `limit_override: int | None = None`, `config_dir: pathlib.Path | None = None`, `bearer_token: str = ""`; default `config_dir` to `pathlib.Path(__file__).parent / "config" / "agents"`; load `{config_dir}/{agent_name}.yaml` with `yaml.safe_load`; store `self.agent_name`, `self.base_url`, `self.run_id`, `self.bearer_token`; store `self.user = canonicalise(user)` (import from `domain.identity`); store `self._allowed_tools = cfg["allowed_tools"]`, `self._fingerprints = cfg.get("fingerprints", {})`, `self._acts_for = cfg["acts_for"]`; set `self._expense_limit = limit_override if limit_override is not None else cfg.get("expense_limit")`; no network calls in constructor

- [X] T003 Implement `register_hooks` in `checkpoint.py`: call `registry.add_callback(BeforeToolCallEvent, self._before_tool_call)` so the pre-call callback is registered with the Strands hook system

- [X] T004 [P] Implement `_fingerprint(tool: AgentTool) -> str` helper in `checkpoint.py`: extract `spec = tool.tool_spec`; return `hashlib.sha256(json.dumps({"name": spec["name"], "description": spec["description"], "inputSchema": spec["inputSchema"]}, sort_keys=True).encode()).hexdigest()`

- [X] T005 [P] Implement `_write_log` helper in `checkpoint.py` with signature `_write_log(self, tool: str, arguments: dict, sim_time: float | None, decision: str, rule: str | None, reason: str) -> None`: open `os.environ["HOOK_LOG"]` in append mode; write `json.dumps({"call_id": str(uuid.uuid4()), "run_id": self.run_id, "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(), "sim_time": sim_time, "user": self.user, "agent": self.agent_name, "tool": tool, "arguments": arguments, "decision": decision, "rule": rule, "reason": reason}) + "\n"`

- [X] T006 Implement `_before_tool_call` skeleton in `checkpoint.py`: extract `tool_name = event.tool_use["name"]`, `arguments = event.tool_use.get("input", {})`, `selected_tool = event.selected_tool`; initialize `sim_time: float | None = None`; wrap the entire rule-evaluation body in a try/except that catches all exceptions, calls `self._write_log(tool_name, arguments, sim_time, "error", None, str(e))`, then re-raises — this skeleton is the container for all six check phases added in later tasks

**Checkpoint**: Foundation ready — constructor, hook registration, fingerprint helper, log writer, and `_before_tool_call` try/except skeleton all in place.

---

## Phase 3: User Story 1 — Policy Enforcement Before Every Tool Call (Priority: P1) 🎯 MVP

**Goal**: Every tool call is evaluated through all six policy phases in fixed order; calls failing any check are blocked before the tool executes.

**Independent Test**: Construct a `PolicyHook` with `allowed_tools: []`, call `_before_tool_call` with any tool, verify `event.cancel_tool` is a non-empty string with `rule="P7"` in the log entry.

### Implementation for User Story 1

- [X] T007 [US1] Implement blank-user guard (check step 1) in `_before_tool_call` in `checkpoint.py`: as the first check inside the try block, `if not self.user:` → call `self._write_log(tool_name, arguments, None, "deny", "P7", "blank user")`, set `event.cancel_tool = "blank user"`, return

- [X] T008 [US1] Implement allowed-tools check (check step 2) in `_before_tool_call` in `checkpoint.py`: `if tool_name not in self._allowed_tools:` → call `self._write_log(tool_name, arguments, None, "deny", "P7", f"tool '{tool_name}' not in allowed_tools")`, set `event.cancel_tool = f"tool '{tool_name}' not in allowed_tools"`, return

- [X] T009 [US1] Implement role check (check step 3) in `_before_tool_call` in `checkpoint.py`: `if self._acts_for != "any":` → `resp = httpx.get(f"{self.base_url}/directory/users/{self.user}")`; if `resp.status_code == 404` → write deny-P7 log "user not in directory", set `event.cancel_tool`, return; call `resp.raise_for_status()` (5xx propagates); if `resp.json()["role"] != self._acts_for` → write deny-P7 log f"role '{resp.json()['role']}' does not match required '{self._acts_for}'", set `event.cancel_tool`, return

- [X] T010 [US1] Implement fingerprint check (check step 4, P6) in `_before_tool_call` in `checkpoint.py`: `if selected_tool is not None and tool_name in self._fingerprints:` → compute `actual = self._fingerprint(selected_tool)`; if `actual != self._fingerprints[tool_name]` → write deny-P6 log f"fingerprint mismatch for '{tool_name}'", set `event.cancel_tool`, return

- [X] T011 [US1] Implement SCOPE check (check step 5) in `_before_tool_call` in `checkpoint.py`: import `TOOL_SCOPE_MAP` from `domain.scopes`; `if not self.bearer_token:` → write deny-SCOPE log "missing bearer token", set `event.cancel_tool`, return; try `claims = jwt.decode(self.bearer_token, options={"verify_signature": False})`; on any `Exception` → write deny-SCOPE log "invalid bearer token", set `event.cancel_tool`, return; `required = TOOL_SCOPE_MAP.get(tool_name)`; if `required and required not in claims.get("scope", "").split():` → write deny-SCOPE log f"token missing scope '{required}'", set `event.cancel_tool`, return

**Checkpoint**: User Story 1 is fully functional. A PolicyHook with any config can block/allow calls through blank-user, allowed-tools, role, fingerprint, and scope checks independently.

---

## Phase 4: User Story 2 — Complete Audit Trail for Every Decision (Priority: P2)

**Goal**: Every tool-call evaluation — allow, deny, or error — produces exactly one JSONL log entry containing all 11 required fields, written before the tool executes.

**Independent Test**: Trigger any tool call via a configured PolicyHook with `HOOK_LOG` set to a temp file; after the call, read the file and verify exactly one new line was appended containing all 11 required fields (`call_id`, `run_id`, `timestamp`, `sim_time`, `user`, `agent`, `tool`, `arguments`, `decision`, `rule`, `reason`).

### Implementation for User Story 2

- [X] T012 [US2] Wire allow-path log write in `_before_tool_call` in `checkpoint.py`: after all six check phases pass (at the end of the try block before returning), call `self._write_log(tool_name, arguments, sim_time, "allow", None, "all checks passed")` — this ensures every passing call produces a log entry with `decision="allow"` and `rule=null`

- [X] T013 [US2] Confirm error-path log write in the `_before_tool_call` exception handler in `checkpoint.py`: in the outer `except` clause, call `self._write_log(tool_name, arguments, sim_time, "error", None, str(e))` before re-raising; verify that `tool_name`, `arguments`, and `sim_time` are defined before the try block so they are always available in the except clause (initialize them to `""`, `{}`, `None` before entering the try)

**Checkpoint**: Every evaluation path — allow, deny from any of the six check steps, and uncaught exception — produces exactly one log entry.

---

## Phase 5: User Story 3 — Runtime Configuration Without Code Changes (Priority: P3)

**Goal**: Authorized tools, fingerprints, role restriction, and expense limit are read from the agent config YAML at construction time. Two PolicyHook instances with different configs enforce only their own config.

**Independent Test**: Construct two `PolicyHook` instances pointing to two different config files, each listing a different allowed tool. Verify that calling the tool allowed by instance A is denied by instance B (P7) and vice versa.

### Implementation for User Story 3

- [X] T014 [US3] Audit `PolicyHook.__init__` in `checkpoint.py` to confirm all four config-driven values are loaded correctly: `_allowed_tools` is always a list (may be empty); `_fingerprints` defaults to `{}` when absent from YAML; `_acts_for` is loaded as-is (string); `_expense_limit` is `limit_override` when not None, else `cfg.get("expense_limit")` which is `None` when the YAML key is absent — no value is hard-coded; confirm the default `config_dir` resolves to `pathlib.Path(__file__).parent / "config" / "agents"` so the module works from any working directory

**Checkpoint**: PolicyHook is fully configurable via YAML. No policy fact is hard-coded. Two instances with different configs enforce independently.

---

## Phase 6: User Story 4 — Business Rule Enforcement for Financial and Travel Tools (Priority: P4)

**Goal**: Rules P1–P5 block fraudulent expense submissions, self-approvals, unauthorized travel bookings, and payments to unapproved vendors using live directory data.

**Independent Test**: For each of P1–P5, construct a minimal tool call that violates only that rule (with all prior checks passing). Verify `event.cancel_tool` is set with the correct rule identifier.

### Implementation for User Story 4

- [X] T015 [P] [US4] Implement P1 rule in the business-rules section (check step 6) of `_before_tool_call` in `checkpoint.py`: `if tool_name == "submit_expense":` → `claimant = canonicalise(arguments.get("claimant", ""))`; if `claimant != self.user:` → write deny-P1 log f"claimant '{claimant}' does not match acting user '{self.user}'", set `event.cancel_tool`, return; import `canonicalise` from `domain.identity`

- [X] T016 [US4] Implement P2 rule immediately after P1 in the `submit_expense` block of `_before_tool_call` in `checkpoint.py`: `if self._expense_limit is not None and arguments.get("amount", 0) > self._expense_limit and not arguments.get("approval_ref"):` → write deny-P2 log f"amount {arguments['amount']} exceeds limit {self._expense_limit} without approval_ref", set `event.cancel_tool`, return; note: equal to limit is allowed, only strictly greater triggers P2

- [X] T017 [US4] Implement P3 rule in `_before_tool_call` in `checkpoint.py`: `elif tool_name == "approve_expense":` → `resp = httpx.get(f"{self.base_url}/directory/expenses/{arguments['expense_id']}")`; if `resp.status_code == 404` → write deny-P3 log "expense not found", set `event.cancel_tool`, return; call `resp.raise_for_status()` (5xx propagates); `claimant = resp.json()["claimant"]`; if `claimant == self.user` → write deny-P3 log "self-approval: acting user is the expense claimant", set `event.cancel_tool`, return; `resp2 = httpx.get(f"{self.base_url}/directory/users/{claimant}")`; if `resp2.status_code == 404` → write deny-P3 log "claimant not in directory", set `event.cancel_tool`, return; call `resp2.raise_for_status()` (5xx propagates); if `resp2.json()["manager"] != self.user` → write deny-P3 log f"acting user '{self.user}' is not manager of '{claimant}'", set `event.cancel_tool`, return

- [X] T018 [US4] Implement P4 rule in `_before_tool_call` in `checkpoint.py`: `elif tool_name == "book_travel":` → `traveller = canonicalise(arguments.get("traveller", ""))`; if `traveller != self.user:` → `resp = httpx.get(f"{self.base_url}/directory/delegations")`; call `resp.raise_for_status()` (5xx propagates); `data = resp.json()`; `sim_time = data.get("sim_time")`; if not `any(d.get("from") == traveller and d.get("to") == self.user and d.get("active") for d in data.get("delegations", []))` → write deny-P4 log f"no active delegation from '{traveller}' to '{self.user}'", set `event.cancel_tool`, return

- [X] T019 [US4] Implement P5 rule in `_before_tool_call` in `checkpoint.py`: `elif tool_name == "pay_vendor":` → `resp = httpx.get(f"{self.base_url}/directory/vendors")`; call `resp.raise_for_status()` (5xx propagates); `vendor = canonicalise(arguments.get("vendor", ""))`; if `vendor not in resp.json().get("vendors", []):` → write deny-P5 log f"vendor '{vendor}' not in approved vendor list", set `event.cancel_tool`, return

**Checkpoint**: All business rules P1–P5 are enforced. Each can be triggered independently by the corresponding tool call pattern.

---

## Phase 7: User Story 5 — Transparent Directory Service Error Handling (Priority: P5)

**Goal**: HTTP 5xx errors from the directory propagate to the calling framework; no 5xx is caught or converted to a clean allow or deny.

**Independent Test**: Configure the PolicyHook with `acts_for != "any"` and point `base_url` at a server returning 503. Call `_before_tool_call`. Confirm `httpx.HTTPStatusError` propagates and the log entry written before propagation has `decision="error"`.

### Implementation for User Story 5

- [X] T020 [US5] Audit every `httpx.get(...)` call in `checkpoint.py` to confirm the 5xx propagation pattern: for each directory call, check 404 via `resp.status_code == 404` (deny with applicable rule), then call `resp.raise_for_status()` — this raises `httpx.HTTPStatusError` for 5xx which propagates through `_before_tool_call` to the outer try/except which writes an error log entry and re-raises; confirm there is no bare `except Exception` wrapping any individual httpx call that could swallow the `HTTPStatusError`

**Checkpoint**: All directory 5xx errors surface to the Strands framework as `httpx.HTTPStatusError`. No silent allow or deny substitution occurs.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Validation and final checks across all user stories.

- [X] T021 [P] Run `uv run pytest tests/test_contract.py -v` from the project root to confirm all existing contract tests pass against the new `checkpoint.py`; resolve any failures before proceeding

- [X] T022 Run `uv run pytest` from the project root to verify the full test suite passes, including any tests in `tests/generated/` if present

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: Depends on T001 — BLOCKS all user stories
  - T004 and T005 are marked [P] and can be written concurrently
- **User Story 1 (Phase 3)**: Depends on Phase 2 (all of T002–T006)
  - T007–T011 must be written in order (sequential check steps in one function)
- **User Story 2 (Phase 4)**: Depends on Phase 3 (T012 requires all deny paths in place, T013 requires the skeleton from T006)
- **User Story 3 (Phase 5)**: Depends on Phase 2 (constructor complete in T002)
- **User Story 4 (Phase 6)**: Depends on Phase 3 (SCOPE check must be in place before business rules run); T015 marked [P] because P1 check operates on submit_expense args independently before P2 logic
- **User Story 5 (Phase 7)**: Depends on Phase 6 (audits all directory calls added in T009, T017, T018, T019)
- **Polish (Phase 8)**: Depends on all user-story phases complete

### User Story Dependencies

- **US1 (P1)**: Can start after Phase 2 — no dependency on other stories
- **US2 (P2)**: Depends on US1 (all deny paths must exist before wiring allow-path log write)
- **US3 (P3)**: Depends on Phase 2 only (constructor audit, no runtime dependency on US1)
- **US4 (P4)**: Depends on US1 (SCOPE check must execute before business rules in the same function)
- **US5 (P5)**: Depends on US4 (audits directory calls added in US4)

### Within Each User Story

- Models (constructor/config): before services (check logic)
- Check step N in the fixed order: before check step N+1
- Log writer (`_write_log`) and error-path wiring (`T006`): before any deny-path task
- Business rules (US4): after structural checks (US1) are in place

### Parallel Opportunities

- T004 (`_fingerprint` helper) and T005 (`_write_log` helper) can be written concurrently
- T021 and T022 (test runs) can be deferred until all phases complete

---

## Parallel Example: Phase 2 Foundational

```bash
# These two helper functions have no mutual dependency — write concurrently:
Task T004: "_fingerprint helper in checkpoint.py"
Task T005: "_write_log helper in checkpoint.py"

# Then proceed to T006 (skeleton) which depends on both helpers being present
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001)
2. Complete Phase 2: Foundational (T002–T006) — CRITICAL, blocks all stories
3. Complete Phase 3: User Story 1 (T007–T011)
4. **STOP and VALIDATE**: Confirm `event.cancel_tool` is set for each of blank-user, tool-not-allowed, role-mismatch, fingerprint-mismatch, scope-missing cases
5. Add US2 audit trail (T012–T013) — ensures every US1 decision is logged

### Incremental Delivery

1. Setup + Foundational → file skeleton and helpers ready
2. US1 → Core enforcement gating working; test independently
3. US2 → Audit trail for every US1 decision
4. US3 → Config isolation verified
5. US4 → Financial/travel business rules layered on
6. US5 → 5xx propagation verified
7. Polish → Full test suite passes

### Single-Developer Strategy

Work phases sequentially in priority order: T001 → T002 → T003 → T004+T005 (parallel) → T006 → T007 → T008 → T009 → T010 → T011 → T012 → T013 → T014 → T015 → T016 → T017 → T018 → T019 → T020 → T021 → T022

---

## Notes

- All tasks edit the single file `checkpoint.py` — no task is truly parallelizable at the file level, but [P] marks helpers that can be drafted by different contributors before wiring
- Each user story is independently verifiable using the corresponding quickstart scenario (S-01 through S-11 in `quickstart.md`)
- `sim_time` must be tracked as a local variable initialized to `None` in `_before_tool_call` and updated from delegation responses (P4 rule, T018); it is passed to all `_write_log` calls in that invocation
- The error-path exception handler must have access to `tool_name` and `arguments` regardless of where the exception is thrown — ensure these are initialized before entering the try block (T013)
- `self.user` is always canonicalized at construction; argument fields (`claimant`, `traveller`, `vendor`) must be canonicalized inline before comparison (using `domain.identity.canonicalise`)
- The `domain.identity.canonicalise` function (strip + lowercase) is the authoritative normalization; do not inline `.strip().lower()` separately
