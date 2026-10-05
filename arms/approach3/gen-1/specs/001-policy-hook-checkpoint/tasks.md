---

description: "Task list for PolicyHook checkpoint enforcement implementation"
---

# Tasks: PolicyHook Checkpoint Enforcement

**Input**: Design documents from `/specs/001-policy-hook-checkpoint/`

**Prerequisites**: plan.md ✅, spec.md ✅, research.md ✅, data-model.md ✅, contracts/ ✅, quickstart.md ✅

**Organization**: Tasks grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story this task belongs to (US1–US5)

## Path Conventions

Single project layout: `checkpoint.py` at repo root, `config/agents/`, `domain/`, `tests/`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create `checkpoint.py` skeleton with all required imports and the `PolicyHook` class stub.

- [X] T001 Create `checkpoint.py` at repo root with all required imports (`hashlib`, `json`, `os`, `pathlib`, `uuid`, `datetime`, `httpx`, `jwt`, `yaml`, `strands.hooks.BeforeToolCallEvent`, `strands.hooks.HookProvider`, `strands.hooks.HookRegistry`, `domain.identity.canonicalise`, `domain.scopes.TOOL_SCOPE_MAP`) and empty `class PolicyHook(HookProvider): pass` stub in `checkpoint.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core class infrastructure that MUST be complete before any user story can be implemented.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T002 Implement `PolicyHook.__init__(self, agent_name: str, user: str, base_url: str, run_id: str | None = None, limit_override: int | None = None, config_dir: pathlib.Path | None = None, bearer_token: str = "")` in `checkpoint.py`: default `config_dir = pathlib.Path(__file__).parent / "config" / "agents"`; load `(config_dir / f"{agent_name}.yaml")` via `yaml.safe_load`; raise `FileNotFoundError` if missing; let `yaml.YAMLError` propagate if invalid; set `_agent_name`, `_user`, `_base_url`, `_run_id`, `_bearer_token`, `_allowed_tools = set(config["allowed_tools"])`, `_fingerprints = config.get("fingerprints", {})`, `_acts_for = config["acts_for"]`, `_expense_limit = limit_override if limit_override is not None else config.get("expense_limit", 500)`
- [X] T003 [P] Implement `register_hooks(self, registry: HookRegistry) -> None` in `checkpoint.py`: body is `registry.add_callback(BeforeToolCallEvent, self._before_tool_call)` (exactly one registration)
- [X] T004 [P] Implement `_write_log(self, entry: dict) -> None` in `checkpoint.py`: `log_path = os.environ.get("HOOK_LOG")`; if falsy, return immediately (no-op); otherwise open `log_path` in `"a"` mode and write `json.dumps(entry) + "\n"`
- [X] T005 [P] Implement `_make_log_entry(self, *, tool: str, arguments: dict, decision: str, rule: str | None, reason: str, sim_time: float | None = None) -> dict` in `checkpoint.py`: return dict with `call_id=str(uuid.uuid4())`, `run_id=self._run_id`, `timestamp=datetime.utcnow().isoformat() + "Z"`, `sim_time=sim_time`, `user=self._user`, `agent=self._agent_name`, `tool=tool`, `arguments=arguments`, `decision=decision`, `rule=rule`, `reason=reason`

**Checkpoint**: Foundation ready — user story implementation can now begin.

---

## Phase 3: User Story 1 — Tool Call Allowed Under All Rules (Priority: P1) 🎯 MVP

**Goal**: Implement the full `_before_tool_call` check sequence for the golden path: blank-user guard → P7 allowed_tools → P6 fingerprint → SCOPE → allow log entry. The acts-for role check (US5) is deferred; configs use `acts_for: "any"` for this phase.

**Independent Test**: Construct a PolicyHook with `acts_for: "any"`, present a valid bearer token with correct scope, call `_before_tool_call` with a non-fingerprinted tool and compliant arguments; verify one log entry with `"decision": "allow"` and `"rule": null` is written to `HOOK_LOG`.

### Implementation for User Story 1

- [X] T006 [US1] Scaffold `_before_tool_call(self, event: BeforeToolCallEvent) -> None` in `checkpoint.py`: extract `tool_name = event.tool_use["name"]` and `arguments = event.tool_use.get("input", {})`; initialize `sim_time = None`; implement blank-user guard — `u = canonicalise(self._user)`: if not `u`, call `self._write_log(self._make_log_entry(tool=tool_name, arguments=arguments, decision="deny", rule="P7", reason="blank user"))`, set `event.cancel_tool = "blank user"`, and return
- [X] T007 [US1] Implement P7 allowed_tools check in `_before_tool_call` in `checkpoint.py`: if `tool_name not in self._allowed_tools`, write deny log with `rule="P7"`, `reason=f"tool {tool_name!r} not in allowed_tools"`, set `event.cancel_tool = reason`, return
- [X] T008 [US1] Implement P6 fingerprint check in `_before_tool_call` in `checkpoint.py`: skip entirely if `event.selected_tool is None` or `tool_name not in self._fingerprints`; otherwise compute `hashlib.sha256(json.dumps({"description": event.selected_tool.tool_spec["description"], "inputSchema": event.selected_tool.tool_spec["inputSchema"], "name": event.selected_tool.tool_spec["name"]}, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")).hexdigest()`; deny P6 if computed hash != `self._fingerprints[tool_name]`
- [X] T009 [US1] Implement SCOPE bearer token check in `_before_tool_call` in `checkpoint.py`: if `self._bearer_token` is empty string, deny SCOPE immediately; otherwise try `payload = jwt.decode(self._bearer_token, options={"verify_signature": False, "verify_exp": False, "verify_aud": False})`; catch `jwt.exceptions.DecodeError` and deny SCOPE; extract `token_scopes = set(payload.get("scope", "").split())`; `required = TOOL_SCOPE_MAP.get(tool_name, "")`; if `required` and `required not in token_scopes`, deny SCOPE with `reason=f"missing scope {required!r}"`
- [X] T010 [US1] Implement allow path in `_before_tool_call` in `checkpoint.py`: after all checks pass, call `self._write_log(self._make_log_entry(tool=tool_name, arguments=arguments, decision="allow", rule=None, reason="all checks passed", sim_time=sim_time))`; do NOT set `event.cancel_tool`

**Checkpoint**: User Story 1 (golden path with `acts_for: "any"`) should be fully functional and testable independently.

---

## Phase 4: User Story 2 — Denied Calls Are Blocked and Logged (Priority: P1)

**Goal**: Implement all P1–P5 business-rule deny paths (P1 claimant check, P2 amount limit, P3 self-approval, P4 delegation, P5 vendor list). Each deny must set `event.cancel_tool` and write exactly one log entry with the correct rule ID.

**Independent Test**: Construct a PolicyHook with minimal config; for each rule trigger in isolation, verify `event.cancel_tool` is set and the log entry records `"decision": "deny"` with the expected `"rule"` field.

### Implementation for User Story 2

- [X] T011 [P] [US2] Implement P1 check in `_before_tool_call` in `checkpoint.py`: after SCOPE check, for `tool_name == "submit_expense"`, deny P1 if `canonicalise(arguments.get("claimant", "")) != canonicalise(self._user)`; write deny log with `rule="P1"`, `reason="claimant does not match acting user"`, set `event.cancel_tool`, return
- [X] T012 [P] [US2] Implement P2 check in `_before_tool_call` in `checkpoint.py`: for `tool_name == "submit_expense"` (evaluated after P1 passes), deny P2 if `arguments.get("amount", 0) > self._expense_limit` and not `arguments.get("approval_ref")`; write deny log with `rule="P2"`, `reason=f"amount {arguments.get('amount')} exceeds limit {self._expense_limit} without approval_ref"`, set `event.cancel_tool`, return
- [X] T013 [US2] Implement P3 check in `_before_tool_call` in `checkpoint.py`: for `tool_name == "approve_expense"`, fetch `httpx.get(f"{self._base_url}/directory/expenses/{arguments['expense_id']}")`; if `status_code >= 500` call `response.raise_for_status()`; if `status_code == 404` deny P3 with reason "expense not found", set `event.cancel_tool`, return; `expense = response.json()`; update `sim_time = expense.get("sim_time", sim_time)`; fetch `httpx.get(f"{self._base_url}/directory/users/{expense['claimant']}")`; if 5xx propagate, if 404 deny P3; `claimant_record = response.json()`; deny P3 if `canonicalise(self._user) == canonicalise(expense["claimant"])` or `canonicalise(self._user) != canonicalise(claimant_record.get("manager", ""))`; write deny log with `rule="P3"`, set `event.cancel_tool`, return
- [X] T014 [US2] Implement P4 check in `_before_tool_call` in `checkpoint.py`: for `tool_name == "book_travel"`, skip P4 entirely if `canonicalise(arguments.get("traveller", "")) == canonicalise(self._user)`; otherwise fetch `httpx.get(f"{self._base_url}/directory/delegations")`; if 5xx propagate; `data = response.json()`; update `sim_time = data.get("sim_time", sim_time)`; deny P4 if no delegation `d` in `data["delegations"]` satisfies `d["active"] == True and canonicalise(d["delegator"]) == canonicalise(arguments["traveller"]) and canonicalise(d["delegate"]) == canonicalise(self._user)`; write deny log with `rule="P4"`, set `event.cancel_tool`, return
- [X] T015 [US2] Implement P5 check in `_before_tool_call` in `checkpoint.py`: for `tool_name == "pay_vendor"`, fetch `httpx.get(f"{self._base_url}/directory/vendors")`; if 5xx propagate; `vendors = response.json()` (plain list); deny P5 if `canonicalise(arguments.get("vendor", ""))` not in `{canonicalise(v) for v in vendors}`; write deny log with `rule="P5"`, `reason=f"vendor {arguments.get('vendor')!r} not in approved list"`, set `event.cancel_tool`, return

**Checkpoint**: All deny paths for US2 should be functional; each rule fires independently and logs correctly.

---

## Phase 5: User Story 3 — Runtime Config Load (No Hard-Coded Facts) (Priority: P2)

**Goal**: Create the three agent YAML config files so PolicyHook instances constructed with different configs produce different enforcement outcomes without any code change.

**Independent Test**: Construct two PolicyHook instances with different configs in a temp dir; verify `_allowed_tools` and `_expense_limit` reflect each config file's contents independently (quickstart.md §4 scenario).

### Implementation for User Story 3

- [X] T016 [US3] Create `config/agents/expense-assistant.yaml` with `agent: "expense-assistant"`, `acts_for: "any"`, `allowed_tools: [submit_expense, read_receipt, approve_expense]`, `fingerprints: {}`, `expense_limit: 500`
- [X] T017 [P] [US3] Create `config/agents/payments-agent.yaml` with `agent: "payments-agent"`, `acts_for: "finance"`, `allowed_tools: [pay_vendor]`, `fingerprints: {}`
- [X] T018 [P] [US3] Create `config/agents/travel-assistant.yaml` with `agent: "travel-assistant"`, `acts_for: "any"`, `allowed_tools: [book_travel]`, `fingerprints: {}`

**Checkpoint**: Runtime config load works; different YAML files produce different enforcement. Verify `config_dir` default resolves to `pathlib.Path(__file__).parent / "config" / "agents"` and `limit_override` overrides YAML `expense_limit`.

---

## Phase 6: User Story 4 — Directory 5xx Errors Propagate (Priority: P2)

**Goal**: Audit all directory HTTP calls in `checkpoint.py` to ensure 5xx errors propagate to the caller with no swallowing, while 404 is handled as "not found."

**Independent Test**: Point PolicyHook at a mock server returning HTTP 500; call a tool that triggers a directory lookup (e.g., `pay_vendor`); confirm an exception propagates and no allow decision is logged (quickstart.md §5 scenario).

### Implementation for User Story 4

- [X] T019 [US4] Audit and fix HTTP error handling for all five directory calls in `checkpoint.py` (P3 expense fetch, P3 user fetch, P4 delegations fetch, P5 vendors fetch, and the US5 acts_for user fetch): after each `response = httpx.get(...)`, add explicit `if response.status_code >= 500: response.raise_for_status()` before any 404 handling; verify no `try/except` blocks wrap these calls; 404 must be checked as `if response.status_code == 404:` and handled with the appropriate deny rule, never suppressed

**Checkpoint**: SC-005 satisfied — no 5xx responses are silently absorbed.

---

## Phase 7: User Story 5 — Acts-For Role Check (Priority: P3)

**Goal**: Implement the acts_for role check in `_before_tool_call` — skipped when `acts_for == "any"`, otherwise fetches the user record live and denies with P7 if the role does not match.

**Independent Test**: Construct a PolicyHook with `acts_for: "finance"` (payments-agent.yaml); call `_before_tool_call` with a non-finance user; verify deny P7 is logged and `event.cancel_tool` is set; call with a finance-role user (mocked directory response); verify the call proceeds.

### Implementation for User Story 5

- [X] T020 [US5] Implement acts_for role check in `_before_tool_call` in `checkpoint.py` (insert between P7 allowed_tools check and P6 fingerprint check): if `self._acts_for == "any"`, skip entirely; otherwise fetch `httpx.get(f"{self._base_url}/directory/users/{canonicalise(self._user)}")`; if `status_code >= 500` call `response.raise_for_status()`; if `status_code == 404` write deny log with `rule="P7"`, `reason="user not found in directory"`, set `event.cancel_tool`, return; `user_record = response.json()`; if `user_record.get("role") != self._acts_for`, write deny log with `rule="P7"`, `reason=f"user role {user_record.get('role')!r} does not match acts_for {self._acts_for!r}"`, set `event.cancel_tool`, return

**Checkpoint**: All five user stories independently functional. SC-001 through SC-006 should now be satisfiable.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Validate against contract tests, run full test suite, and perform quickstart smoke tests.

- [X] T021 Run `uv run pytest tests/test_contract.py -v` from repo root and fix any failures in `checkpoint.py` until all 7 contract tests pass (do not modify `tests/test_contract.py`)
- [X] T022 Run `uv run pytest -v` from repo root and fix any failures in `tests/` (including `tests/generated/` if present) without modifying test files
- [X] T023 [P] Execute the quickstart.md §3 golden-path smoke test and §4 SC-004 runtime config reload test; verify `decision="deny"` with `rule="SCOPE"` for empty bearer token, and that two PolicyHook instances with different configs enforce differently

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Phase 1 — **blocks all user stories**
- **User Stories (Phases 3–7)**: All depend on Phase 2 completion
  - Phase 3 (US1) must complete before Phase 4 (US2) — US2 builds on the check sequence scaffolded in US1
  - Phase 3 (US1) must complete before Phase 7 (US5) — acts_for inserts into the US1 sequence
  - Phases 5–6 (US3, US4) can proceed in parallel once Phase 2 completes (different concerns)
- **Polish (Phase 8)**: Depends on all user story phases complete

### User Story Dependencies

- **US1 (P1)**: Can start after Phase 2 — no story dependencies
- **US2 (P1)**: Depends on US1 (reuses `_before_tool_call` scaffold from T006)
- **US3 (P2)**: Depends on Phase 2 only (config loading is constructor-only)
- **US4 (P2)**: Depends on US2 (audits the HTTP calls implemented in T013–T015)
- **US5 (P3)**: Depends on US1 (inserts into the check sequence from T006)

### Within Each User Story

- Models before services before endpoint logic
- Core implementation before integration
- Story complete before moving to next priority

### Parallel Opportunities

- Within Phase 2: T003, T004, T005 can run in parallel (different methods, no dependencies on each other beyond T002 stubbing the class)
- Within Phase 4: T011 and T012 can run in parallel (both in `submit_expense` branch, different checks)
- Within Phase 5: T017 and T018 can run in parallel (different config files)
- Phase 5 (US3) and Phase 6 (US4) can overlap once Phase 4 completes

---

## Parallel Example: User Story 2

```bash
# Launch P1 and P2 checks together (both in submit_expense branch, no overlap):
Task: "Implement P1 check for submit_expense claimant in checkpoint.py"  # T011
Task: "Implement P2 check for submit_expense amount limit in checkpoint.py"  # T012

# Then sequentially:
Task: "Implement P3 check for approve_expense in checkpoint.py"   # T013
Task: "Implement P4 check for book_travel in checkpoint.py"       # T014
Task: "Implement P5 check for pay_vendor in checkpoint.py"        # T015
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001)
2. Complete Phase 2: Foundational (T002–T005) — CRITICAL, blocks all stories
3. Complete Phase 5: US3 config files (T016–T018) — needed for constructor to load
4. Complete Phase 3: US1 allow path (T006–T010)
5. **STOP and VALIDATE**: Run `uv run pytest tests/test_contract.py -v`
6. Confirm golden-path smoke test from quickstart.md §3 passes

### Incremental Delivery

1. Setup + Foundational + Config Files → Foundation ready
2. US1 (Phase 3) → allow path works; run contract tests (MVP!)
3. US2 (Phase 4) → all deny paths work
4. US4 (Phase 6) → 5xx propagation audited
5. US5 (Phase 7) → acts_for role check wired
6. Polish (Phase 8) → full test suite passes

### Parallel Team Strategy

With multiple developers after Phase 2 + Phase 5:
- Developer A: US1 (Phase 3) → US2 (Phase 4) → US4 (Phase 6)
- Developer B: US3 config files (Phase 5, if not done) → US5 (Phase 7)
- Both: Polish (Phase 8) together

---

## Notes

- Single file constraint: all logic goes in `checkpoint.py`; no new modules may be created
- `[P]` tasks can run in parallel only when they target different methods or files
- `event.cancel_tool` must be set to a non-empty string to cancel a tool call; do not set it for allow decisions
- `sim_time` is updated opportunistically from any directory response that includes it (only `/directory/delegations` currently does)
- JWT soft-decode: use `options={"verify_signature": False, "verify_exp": False, "verify_aud": False}`; no JWKS needed
- Fingerprint format: SHA-256 hex of `json.dumps({"description":…,"inputSchema":…,"name":…}, sort_keys=True, separators=(",",":"), ensure_ascii=False)`
- Do not modify `tests/test_contract.py` or any provided test files
- Commit after each logical group; stop at any phase checkpoint to validate independently
