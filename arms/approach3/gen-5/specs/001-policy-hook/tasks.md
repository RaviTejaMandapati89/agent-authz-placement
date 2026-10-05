---

description: "Task list for PolicyHook Class — Agent Checkpoint Enforcement"
---

# Tasks: PolicyHook Class — Agent Checkpoint Enforcement

**Input**: Design documents from `/specs/001-policy-hook/`

**Prerequisites**: plan.md ✓, spec.md ✓, research.md ✓, data-model.md ✓, contracts/policy-hook-api.md ✓, quickstart.md ✓

**Organization**: Tasks are grouped by user story to enable independent implementation and testing. Tests are included per constitution mandate (§Development & Testing Standards).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no blocking dependencies)
- **[Story]**: Which user story this task belongs to (US1–US6)
- Exact file paths included in every task description

## Path Conventions

Single-project layout: `checkpoint.py` at repo root, tests in `tests/generated/`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Create the single generated source file skeleton so all tests can import the class.

- [X] T001 Create checkpoint.py at repo root with stdlib imports (os, json, uuid, datetime, hashlib, pathlib), third-party imports (yaml, httpx, jwt), strands imports (HookProvider, HookRegistry, BeforeToolCallEvent), domain imports (domain.identity.canonicalise, domain.scopes.TOOL_SCOPE_MAP), PolicyHook class declaration extending HookProvider, and empty stub methods __init__, register_hooks, _before_tool_call

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure all user stories depend on — constructor, hook registration, HTTP helper, and log helper.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T002 Implement PolicyHook.__init__ in checkpoint.py — accept agent_name: str, user: str, base_url: str, run_id: str|None=None, limit_override: int|None=None, config_dir: pathlib.Path|None=None, bearer_token: str=""; default config_dir to pathlib.Path(__file__).parent / "config" / "agents"; load {config_dir}/{agent_name}.yaml via yaml.safe_load; raise FileNotFoundError if YAML file missing; raise ValueError or yaml.YAMLError if unparseable; store self._agent_name, self._user, self._base_url, self._run_id, self._bearer_token, self._allowed_tools (list[str] from YAML), self._fingerprints (dict[str,str] from YAML), self._acts_for (str from YAML), self._expense_limit (limit_override if not None, else config.get("expense_limit"))
- [X] T003 [P] Implement PolicyHook.register_hooks in checkpoint.py — call registry.add_callback(BeforeToolCallEvent, self._before_tool_call)
- [X] T004 [P] Implement _http_get private helper in checkpoint.py — accept url: str; perform synchronous httpx.get(url); call response.raise_for_status() when response.status_code >= 500 to propagate httpx.HTTPStatusError unchanged; return the response object for 2xx and 404 statuses without raising
- [X] T005 [P] Implement _log_entry private helper in checkpoint.py — accept call_id: str, tool_name: str, args: dict, decision: str, rule: str|None, reason: str, sim_time: float|None; assemble dict with all 11 required fields: call_id, run_id (self._run_id), timestamp (datetime.datetime.now(datetime.timezone.utc).isoformat()), sim_time, user (self._user), agent (self._agent_name), tool (tool_name), arguments (args), decision, rule, reason; append json.dumps(entry) + "\n" to open(os.environ["HOOK_LOG"], "a")

**Checkpoint**: Foundation ready — all user stories can now begin in dependency order.

---

## Phase 3: User Story 1 - Permitted Tool Call Allowed (Priority: P1) 🎯 MVP

**Goal**: A well-formed event with valid token, allowed tool, and matching fingerprint executes successfully. One log entry with decision="allow" and rule=null is written.

**Independent Test**: Instantiate hook with expense-assistant.yaml, send event with claimant==user, valid JWT with required scope, tool in allowed_tools — assert event.cancel_tool is falsy and log has decision="allow", rule=null.

### Tests for User Story 1 ⚠️ Write FIRST — ensure they FAIL before implementation

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [X] T006 [P] [US1] Write tests/generated/test_allow_path.py — test_allow_own_expense: hook with allowed_tools=["submit_expense"], claimant==user, valid JWT with required scope, fingerprints dict empty (skip P6) → assert event.cancel_tool is falsy and one log line with decision="allow", rule=null; test_travel_self_no_delegation_lookup: book_travel with traveller==user → assert no HTTP call to /directory/delegations and decision="allow"

### Implementation for User Story 1

- [X] T007 [US1] Implement _before_tool_call scaffold in checkpoint.py — generate call_id=str(uuid.uuid4()); extract tool_name=event.tool_use["name"] and args=event.tool_use["input"]; initialize sim_time=None; define ordered check slots (blank-user, allowed_tools, acts_for, P6, SCOPE, P1-P5); add final allow branch that calls self._log_entry(call_id, tool_name, args, "allow", None, "all checks passed", sim_time) and returns without setting event.cancel_tool
- [X] T008 [P] [US1] Implement blank-user guard and allowed_tools check in checkpoint.py _before_tool_call — deny P7 with reason "blank user" if canonicalise(self._user)=="", calling _log_entry and setting event.cancel_tool="P7: blank user" then returning; deny P7 with reason "tool '{tool_name}' not in allowed_tools" if tool_name not in self._allowed_tools
- [X] T009 [P] [US1] Implement acts_for role check in checkpoint.py _before_tool_call — skip entire check if self._acts_for=="any"; otherwise call _http_get(f"{self._base_url}/directory/users/{self._user}"); on 404 deny P7 with reason "user not found in directory"; on 2xx deny P7 if response.json()["role"] != self._acts_for

**Checkpoint**: Allow path functional — US1 tests pass.

---

## Phase 4: User Story 2 - Policy Rules P1–P7 Deny Unwanted Actions (Priority: P1)

**Goal**: Each of the seven policy deny paths fires independently with the correct rule code in the log.

**Independent Test**: For each rule P1–P7, construct a minimal event that should be denied and assert decision="deny", rule="P<N>".

### Tests for User Story 2 ⚠️ Write FIRST — ensure they FAIL before implementation

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [X] T010 [P] [US2] Write tests/generated/test_deny_rules.py — test_p7_blank_user: canonicalise(user)=="" → decision="deny", rule="P7"; test_p7_not_allowed_tool: tool_name not in allowed_tools → decision="deny", rule="P7"; test_p6_fingerprint_mismatch: selected_tool present, stored fingerprint, computed hash differs → decision="deny", rule="P6"; test_p6_skip_when_no_selected_tool: event.selected_tool=None → P6 not triggered
- [X] T011 [P] [US2] Add to tests/generated/test_deny_rules.py — test_p1_claimant_not_user: submit_expense with claimant≠user → rule="P1"; test_p2_over_limit_no_approval: amount>expense_limit, no approval_ref → rule="P2"; test_p2_allow_with_approval_ref: amount>limit but approval_ref present → not denied by P2; test_p3_self_approval: approve_expense where acting_user==claimant → rule="P3"; test_p4_no_active_delegation: book_travel traveller≠user, delegations list empty → rule="P4"; test_p4_inactive_delegation: delegation exists but active=False → rule="P4"; test_p5_vendor_not_in_list: pay_vendor with vendor absent from directory list → rule="P5"

### Implementation for User Story 2

- [X] T012 [US2] Implement P6 fingerprint check in checkpoint.py _before_tool_call — skip if event.selected_tool is None or tool_name not in self._fingerprints; compute digest = hashlib.sha256((spec["name"] + spec["description"] + json.dumps(spec["inputSchema"], sort_keys=True, separators=(",",":"))) .encode()).hexdigest() where spec = event.selected_tool.tool_spec; deny P6 if digest != self._fingerprints[tool_name]
- [X] T013 [P] [US2] Implement P1 rule in checkpoint.py _before_tool_call — for tool_name=="submit_expense": deny P1 if canonicalise(args.get("claimant","")) != canonicalise(self._user)
- [X] T014 [P] [US2] Implement P2 rule in checkpoint.py _before_tool_call — for tool_name=="submit_expense": deny P2 if self._expense_limit is not None and args.get("amount",0) > self._expense_limit and not args.get("approval_ref")
- [X] T015 [US2] Implement P3 rule in checkpoint.py _before_tool_call — for tool_name=="approve_expense": call _http_get(f"{self._base_url}/directory/expenses/{args['expense_id']}"); on 404 deny P3 "expense not found"; canonicalise claimant from response; deny P3 "self-approval" if canonicalise(self._user)==claimant; call _http_get(f"{self._base_url}/directory/users/{claimant}"); deny P3 "not claimant's manager" if response.json().get("manager") != self._user
- [X] T016 [US2] Implement P4 rule in checkpoint.py _before_tool_call — for tool_name=="book_travel": skip if canonicalise(args.get("traveller",""))==canonicalise(self._user); call _http_get(f"{self._base_url}/directory/delegations"); parse body as {"delegations": [...], "sim_time": <float>}; set sim_time=body["sim_time"]; deny P4 if no delegation entry where entry["delegator"]==canonicalise(traveller) and entry["delegate"]==canonicalise(self._user) and entry["active"]==True
- [X] T017 [P] [US2] Implement P5 rule in checkpoint.py _before_tool_call — for tool_name=="pay_vendor": call _http_get(f"{self._base_url}/directory/vendors"); parse body as list of vendor strings; deny P5 if canonicalise(args.get("vendor","")) not in [canonicalise(v) for v in vendor_list]

**Checkpoint**: All seven deny paths independently testable — US2 tests pass.

---

## Phase 5: User Story 3 - Bearer Token Scope Enforcement (Priority: P2)

**Goal**: Missing, malformed, or wrong-scope tokens are denied with rule="SCOPE" before any business-rule check.

**Independent Test**: Pass empty token, non-JWT string, valid JWT missing required scope — assert decision="deny", rule="SCOPE", no HTTP calls made.

### Tests for User Story 3 ⚠️ Write FIRST — ensure they FAIL before implementation

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [X] T018 [P] [US3] Write tests/generated/test_scope.py — test_empty_bearer_token: bearer_token="" → decision="deny", rule="SCOPE"; test_malformed_bearer_token: bearer_token="not-a-jwt" → decision="deny", rule="SCOPE"; test_valid_jwt_missing_scope: valid JWT but scope claim missing required scope for tool → decision="deny", rule="SCOPE"; test_scope_check_before_business_rules: assert no HTTP calls made to directory when SCOPE fires

### Implementation for User Story 3

- [X] T019 [US3] Implement SCOPE check in checkpoint.py _before_tool_call — deny SCOPE if self._bearer_token==""; try: claims=jwt.decode(self._bearer_token, options={"verify_signature":False}, algorithms=["HS256","RS256","ES256"]); except (jwt.DecodeError, jwt.InvalidTokenError): deny SCOPE "invalid JWT"; check required_scope=TOOL_SCOPE_MAP.get(tool_name); if required_scope and required_scope not in claims.get("scope","").split(): deny SCOPE f"token missing required scope '{required_scope}'"

**Checkpoint**: SCOPE enforcement verified independently — US3 tests pass.

---

## Phase 6: User Story 4 - Directory 5xx Errors Propagate (Priority: P2)

**Goal**: HTTP 5xx from any directory route propagates as httpx.HTTPStatusError without being caught or silenced by the hook.

**Independent Test**: Mock directory to return 500/503 on each route; assert httpx.HTTPStatusError propagates and no log entry is written.

### Tests for User Story 4 ⚠️ Write FIRST — ensure they FAIL before implementation

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [X] T020 [P] [US4] Write tests/generated/test_5xx_propagation.py — test_500_on_users_route: mock httpx to return 500 on /directory/users/{user}, trigger acts_for check → assert httpx.HTTPStatusError raised, no log entry written; test_503_on_vendors_route: mock 503 on /directory/vendors, trigger P5 check → HTTPStatusError propagates; test_503_on_delegations_route: mock 503 on /directory/delegations, trigger P4 check → HTTPStatusError propagates

**Checkpoint**: Fail-closed behavior verified — US4 tests pass.

---

## Phase 7: User Story 5 - Decision Log Written for Every Evaluation (Priority: P2)

**Goal**: Every tool call evaluation — allow or deny — appends exactly one JSON object with all 11 required fields to HOOK_LOG.

**Independent Test**: Execute hook ten times with varied outcomes; assert exactly ten JSONL entries each containing all required keys.

### Tests for User Story 5 ⚠️ Write FIRST — ensure they FAIL before implementation

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [X] T021 [P] [US5] Write tests/generated/test_log_completeness.py — test_ten_evaluations: run hook 10 times with varied decisions (allow+deny variants); read HOOK_LOG, assert exactly 10 newline-delimited valid JSON objects; assert each has all keys: {call_id, run_id, timestamp, sim_time, user, agent, tool, arguments, decision, rule, reason}; test_sim_time_from_delegation: assert sim_time is a float when P4 delegation lookup is performed; test_sim_time_null_otherwise: assert sim_time is null when no delegation lookup occurs; test_deny_always_has_rule: assert rule is non-null for every deny decision; test_allow_rule_is_null: assert rule is null for every allow decision

**Checkpoint**: Auditability confirmed — US5 tests pass.

---

## Phase 8: User Story 6 - Agent Config Loaded from YAML at Construction (Priority: P3)

**Goal**: PolicyHook reads all policy values from YAML at construction. Changing the YAML and reinstantiating the hook changes policy without any source code modification.

**Independent Test**: Create two YAML files with different allowed_tools; instantiate PolicyHook with each and assert the correct tools are allowed/denied.

### Tests for User Story 6 ⚠️ Write FIRST — ensure they FAIL before implementation

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [X] T022 [P] [US6] Write tests/generated/test_yaml_config.py — test_allowed_tools_from_yaml: YAML with allowed_tools=["submit_expense"], evaluate book_travel → deny P7; test_reinstantiate_with_new_yaml: reinstantiate with YAML adding book_travel → passes P7 check; test_missing_yaml_raises: nonexistent YAML path → FileNotFoundError raised at construction (no network call); test_unparseable_yaml_raises: YAML with invalid syntax → yaml.YAMLError or ValueError raised at construction; test_limit_override_supersedes_yaml: yaml expense_limit=500, limit_override=1000, submit 600 without approval_ref → not denied by P2 (1000 limit applies)

**Checkpoint**: Operator config control verified — US6 tests pass.

---

## Phase 9: Polish & Cross-Cutting Concerns

**Purpose**: Final validation and compliance checks across all user stories.

- [X] T023 [P] Verify no hard-coded policy values in checkpoint.py — run: `grep -n '"alice"\|"bob"\|"acme"\|"employee"\|"finance"\|500\b' checkpoint.py`; assert zero matches
- [X] T024 Run complete test suite via `uv run pytest -v` and confirm zero failures across tests/test_contract.py and tests/generated/

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Phase 1 — **blocks all user stories**
- **US1 + US2 (Phases 3–4)**: Both P1 priority; start after Phase 2; US1 before US2 (US2 adds rule checks to skeleton built in US1)
- **US3–US5 (Phases 5–7)**: P2 priority; depend on Phase 2; can proceed in parallel after US1 scaffold (T007) is done
- **US6 (Phase 8)**: P3 priority; depends on Phase 2; constructor impl is already in T002, Phase 8 adds tests
- **Polish (Phase 9)**: Depends on all user stories complete

### User Story Dependencies

- **US1 (P1)**: Start after Phase 2 — provides _before_tool_call scaffold that US2/US3 extend
- **US2 (P1)**: Start after US1 T007 (scaffold) — adds P6 and P1–P5 rule branches
- **US3 (P2)**: Start after US1 T007 (scaffold) — adds SCOPE check branch
- **US4 (P2)**: Start after Phase 2 — _http_get 5xx behavior already implemented; Phase 6 writes tests only
- **US5 (P2)**: Start after Phase 2 — _log_entry already implemented; Phase 7 writes tests only
- **US6 (P3)**: Start after Phase 2 — __init__ YAML loading already implemented; Phase 8 writes tests only

### Within Each User Story

- Test tasks MUST be written first (TDD) and MUST fail before implementation begins
- Check sequence order in _before_tool_call is MANDATED by constitution: blank-user → allowed_tools → acts_for → P6 → SCOPE → P1–P5
- T007 (scaffold) must complete before T008/T009/T012–T019 extend _before_tool_call
- Within US2: T012 (P6) must be placed before T013–T017 in the check sequence (code order, not task order)

### Parallel Opportunities

- T003, T004, T005 run in parallel (Phase 2 — different helpers)
- T008, T009 run in parallel (US1 — different branches of _before_tool_call)
- T010, T011 run in parallel (US2 — same test file, separate sections)
- T013, T014, T017 run in parallel (US2 — independent rule branches for P1, P2, P5)
- T018, T020, T021, T022 run in parallel (test files for US3/US4/US5/US6 — separate files)
- After Phase 2: US3 test (T018) and US4 test (T020) can be written in parallel with US2 implementation

---

## Parallel Example: Phase 4 (User Story 2)

```bash
# Write tests first (can run in parallel):
Task T010: tests/generated/test_deny_rules.py — P7 and P6 tests
Task T011: tests/generated/test_deny_rules.py — P1-P5 tests

# After T007 (scaffold) is done, implement rules in parallel:
Task T013: P1 rule in checkpoint.py (submit_expense claimant check)
Task T014: P2 rule in checkpoint.py (expense limit check)
Task T017: P5 rule in checkpoint.py (vendor list check)
# T015 (P3) and T016 (P4) require sequential HTTP calls; run those after P1/P2/P5
```

---

## Implementation Strategy

### MVP First (User Stories 1 + 2 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: US1 (allow path + P7 checks)
4. Complete Phase 4: US2 (P1–P7 deny rules)
5. **STOP and VALIDATE**: `uv run pytest tests/generated/test_allow_path.py tests/generated/test_deny_rules.py`
6. Deploy/demo if ready

### Incremental Delivery

1. Setup + Foundational → skeleton importable
2. US1 + US2 → core enforcement working → validate with `uv run pytest tests/generated/`
3. US3 → SCOPE gating added → validate
4. US4 + US5 → resilience + auditability confirmed → validate
5. US6 → operator config control confirmed → full suite passes: `uv run pytest -v`

### Parallel Team Strategy

After Phase 2 is complete:
- Developer A: US1 (T006–T009) then US2 (T010–T017)
- Developer B: US3 (T018–T019) + US4 (T020)
- Developer C: US5 (T021) + US6 (T022)
- All merge → Phase 9 (T023–T024) together

---

## Notes

- [P] tasks = different files or independent branches, no blocking dependencies on each other
- [Story] label maps each task to its user story for traceability
- Check order in _before_tool_call is MANDATED by constitution — reordering requires a constitution amendment
- T007 establishes the full check sequence skeleton; T008/T009/T012–T019 fill in specific branches
- sim_time is float only when GET /directory/delegations is called (P4 path); null in all other paths
- Fingerprint formula: `hashlib.sha256((spec["name"] + spec["description"] + json.dumps(spec["inputSchema"], sort_keys=True, separators=(",",":"))) .encode()).hexdigest()`
- acts_for=="any" skips the directory role lookup entirely
- limit_override at construction supersedes YAML expense_limit for P2 evaluation
- HTTP 404 from directory: treated as "not found", apply corresponding deny rule (P3, P4, or P7 as appropriate)
- HTTP 5xx from directory: must propagate as httpx.HTTPStatusError — no try/except around _http_get calls
