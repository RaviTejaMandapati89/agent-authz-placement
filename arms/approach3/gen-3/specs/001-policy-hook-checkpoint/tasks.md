---

description: "Task list for PolicyHook Checkpoint Enforcement implementation"
---

# Tasks: PolicyHook Checkpoint Enforcement

**Input**: Design documents from `/specs/001-policy-hook-checkpoint/`

**Prerequisites**: plan.md, spec.md, data-model.md, contracts/, research.md, quickstart.md

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files or independent code paths, no dependencies)
- **[Story]**: Which user story this task belongs to (US1–US7)
- File paths are relative to the project root

## Path Conventions

Single-project layout. The only application source file produced is `checkpoint.py` at the project root. All user story tasks target that single file.

---

## Phase 1: Setup (Verify Existing Infrastructure)

**Purpose**: Confirm that all prerequisite files required by `checkpoint.py` exist with the correct content before starting implementation.

- [X] T001 Verify domain/identity.py exports a `canonicalise(s: str) -> str` function (strip + lowercase) and domain/scopes.py exports a `TOOL_SCOPE_MAP: dict[str, str]` mapping tool names to required scope strings at domain/identity.py and domain/scopes.py
- [X] T002 [P] Verify config/agents/expense-assistant.yaml, travel-assistant.yaml, and payments-agent.yaml each contain the required keys: `agent`, `acts_for`, `allowed_tools` (list), `fingerprints` (dict); confirm payments-agent.yaml omits `expense_limit` (no limit applies) at config/agents/

---

## Phase 2: Foundational (PolicyHook Class Infrastructure)

**Purpose**: Core class skeleton that ALL user story phases depend on — `checkpoint.py` must exist with working constructor, hook registration, and audit log helpers before any policy logic can be added.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T003 Create checkpoint.py with all allowed imports only (stdlib: hashlib, json, os, pathlib, uuid, datetime/timezone; third-party: httpx, jwt, yaml; strands: strands.hooks.BeforeToolCallEvent, strands.hooks.HookRegistry; project-local: domain.identity.canonicalise, domain.scopes.TOOL_SCOPE_MAP) and a `PolicyHook` class stub at checkpoint.py
- [X] T004 Implement `PolicyHook.__init__(self, agent_name: str, user: str, base_url: str, run_id: str | None = None, limit_override: int | None = None, config_dir: pathlib.Path | None = None, bearer_token: str = "")`: store all params as `_agent_name`, `_user`, `_base_url`, `_run_id`, `_bearer_token`; load YAML from `(config_dir or pathlib.Path(__file__).parent / "config" / "agents") / f"{agent_name}.yaml"` as `self._config`; create `self._http = httpx.Client()` at checkpoint.py
- [X] T005 Implement `PolicyHook.register_hooks(self, registry)` that calls `registry.add_callback(BeforeToolCallEvent, self._before_tool_call)` at checkpoint.py
- [X] T006 Implement `PolicyHook._log(self, tool: str, arguments: dict, decision: str, rule: str | None, reason: str, sim_time: float | None = None)` that opens `os.environ["HOOK_LOG"]` in append mode (`"a"`, encoding UTF-8) and writes one JSON line containing all 11 required fields: `call_id` (str(uuid.uuid4())), `run_id` (self._run_id), `timestamp` (datetime.now(timezone.utc).isoformat()), `sim_time`, `user` (self._user), `agent` (self._agent_name), `tool`, `arguments`, `decision`, `rule`, `reason` at checkpoint.py

**Checkpoint**: Foundation ready — user story implementation can now begin.

---

## Phase 3: User Story 1 + User Story 2 — Allow Path + P7 Default Deny (Priority: P1) 🎯 MVP

**Goal**: Every evaluation must have a working allow path (US1) and all three P7 triggers must deny before reaching any business rule (US2).

**Independent Test (US1)**: Construct `PolicyHook` for `expense-assistant` with a valid bearer token; call `_before_tool_call` with `tool_use={"name":"submit_expense","input":{"claimant":"alice","amount":100.0,"description":"x"}}`; `event.cancel_tool` is falsy and the log records `"decision":"allow"`, `"rule":null`.

**Independent Test (US2)**: Call `_before_tool_call` with a tool not in `allowed_tools`; `event.cancel_tool` is non-empty and the log records `"decision":"deny"`, `"rule":"P7"`.

### Implementation for User Story 1 + 2

- [X] T007 [US1] [US2] Implement `PolicyHook._before_tool_call(self, event)` skeleton: extract `tool = event.tool_use["name"]` and `args = event.tool_use["input"]`; initialise `sim_time = None`; wrap the entire body in `try/except Exception as exc` that calls `self._log(tool, args, "error", None, str(exc))` then re-raises; add `self._log(tool, args, "allow", None, "allow", sim_time)` as the final line before the method returns (the allow path) at checkpoint.py
- [X] T008 [US1] [US2] Add blank-user guard (evaluation step 1) inside `_before_tool_call` at the top of the try block: `if canonicalise(self._user) == "":` → call `self._log(tool, args, "deny", "P7", "blank user")`, set `event.cancel_tool = "blank user"`, and `return` at checkpoint.py
- [X] T009 [US1] [US2] Add allowed_tools and acts_for checks (evaluation steps 2–3) inside `_before_tool_call`: step 2 — if `tool not in self._config["allowed_tools"]` → deny P7 with reason `f"tool '{tool}' not in allowed_tools"`; step 3 — if `self._config["acts_for"] != "any"` → `GET {self._base_url}/directory/users/{canonicalise(self._user)}`; if `response.status_code == 404` → deny P7 "user not found"; elif `response.status_code != 200` → `response.raise_for_status()`; else if `response.json()["role"] != self._config["acts_for"]` → deny P7 "role mismatch" at checkpoint.py

**Checkpoint**: US1 and US2 are independently testable. Run `uv run pytest tests/test_contract.py -v` — all 6 tests should pass.

---

## Phase 4: User Story 3 — Bearer Token Scope Enforced (Priority: P1)

**Goal**: A call with a missing, malformed, or insufficiently-scoped JWT is denied with rule `SCOPE`.

**Independent Test**: Call with `bearer_token=""` for any allowed tool → log records `"decision":"deny"`, `"rule":"SCOPE"`; call with a valid JWT missing the required scope → same outcome.

### Implementation for User Story 3

- [X] T010 [US3] Add SCOPE check (evaluation step 5, between P6 and P1–P5) inside `_before_tool_call`: attempt `jwt.decode(self._bearer_token, options={"verify_signature": False, "verify_exp": False})`; on `jwt.DecodeError` or `Exception` → deny SCOPE "bearer token missing or invalid"; extract scopes as `set(payload.get("scope", "").split())`; look up `required = TOOL_SCOPE_MAP.get(tool)` — if `required` is set and `required not in scopes` → deny SCOPE "missing required scope" at checkpoint.py

**Checkpoint**: US3 independently testable. A call with no token for an allowed tool is denied with SCOPE before reaching P1–P5.

---

## Phase 5: User Story 4 — Business Rules P1–P5 Enforced (Priority: P2)

**Goal**: Each of the five business rules is enforced by fetching live data from the directory on every call.

**Independent Test**: Submit expense with wrong claimant → deny P1; oversized amount without approval_ref → deny P2; approve own expense → deny P3; book travel without active delegation → deny P4 with non-null sim_time; pay unlisted vendor → deny P5.

### Implementation for User Story 4

- [X] T011 [US4] Implement P1 rule inside `_before_tool_call` for `submit_expense`: if `canonicalise(args.get("claimant", "")) != canonicalise(self._user)` → deny P1 "claimant does not match acting user" at checkpoint.py
- [X] T012 [US4] Implement P2 rule inside `_before_tool_call` for `submit_expense` (after P1): if `self._config.get("expense_limit") is not None` and `args["amount"] > self._config["expense_limit"]` and not `args.get("approval_ref")` → deny P2 "amount exceeds limit without approval_ref" at checkpoint.py
- [X] T013 [US4] Implement P3 rule inside `_before_tool_call` for `approve_expense`: (a) `GET {base_url}/directory/expenses/{args["expense_id"]}`; if 404 → deny P3 "expense not found"; else `raise_for_status()` on non-200; (b) if `expense["claimant"] == canonicalise(self._user)` → deny P3 "self-approval not permitted"; (c) `GET {base_url}/directory/users/{expense["claimant"]}`; if 404 → deny P3 "claimant not found"; else `raise_for_status()` on non-200; (d) if `user_rec.get("manager") != self._user` → deny P3 "approver is not claimant's manager" at checkpoint.py
- [X] T014 [P] [US4] Implement P4 rule inside `_before_tool_call` for `book_travel`: if `canonicalise(args["traveller"]) != canonicalise(self._user)` → `GET {base_url}/directory/delegations`; on non-200 call `response.raise_for_status()`; capture `sim_time = response.json()["sim_time"]`; check whether any delegation satisfies `delegator == canonicalise(args["traveller"])` and `delegate == canonicalise(self._user)` and `active == True` and `"travel" in scope`; if none found → deny P4 f"no active travel delegation from '{args['traveller']}' to '{self._user}'" at checkpoint.py
- [X] T015 [P] [US4] Implement P5 rule inside `_before_tool_call` for `pay_vendor`: `GET {base_url}/directory/vendors`; on non-200 call `response.raise_for_status()`; if `canonicalise(args["vendor"])` not in the returned JSON list → deny P5 f"vendor '{args['vendor']}' not in approved vendor list" at checkpoint.py

**Checkpoint**: All five business rules independently trigger the correct deny code.

---

## Phase 6: User Story 5 — Tool Fingerprint Validated (Priority: P2)

**Goal**: A modified tool definition (name/description/inputSchema hash mismatch) is caught before the SCOPE check with rule `P6`.

**Independent Test**: Construct a fake `selected_tool` with a modified `description`; compute the hash — it differs from the stored fingerprint → log records `"decision":"deny"`, `"rule":"P6"`. With `selected_tool=None` the check is skipped entirely.

### Implementation for User Story 5

- [X] T016 [US5] Insert P6 fingerprint check (evaluation step 4, immediately after the acts_for check and before the SCOPE check) inside `_before_tool_call`: skip entirely if `event.selected_tool is None` or `tool not in self._config.get("fingerprints", {})`; otherwise access `spec = event.selected_tool.tool_spec`; compute `digest = hashlib.sha256(json.dumps({"name": spec["name"], "description": spec["description"], "inputSchema": spec["inputSchema"]}, sort_keys=True).encode("utf-8")).hexdigest()`; if `digest != self._config["fingerprints"][tool]` → deny P6 "fingerprint mismatch" at checkpoint.py

**Checkpoint**: P6 check is skipped for `selected_tool=None` and fires for any hash mismatch.

---

## Phase 7: User Story 6 — Every Call Produces an Audit Log Entry (Priority: P2)

**Goal**: Every evaluation path appends exactly one complete JSONL entry — allow, deny, and error paths all write before returning or raising.

**Independent Test**: Make 5 calls with mixed outcomes; read `HOOK_LOG`; assert exactly 5 lines, each valid JSON, each with all 11 required fields; all `call_id` values are distinct UUIDs.

### Implementation for User Story 6

- [X] T017 [US6] Audit all code paths in `_before_tool_call` and `_log` in checkpoint.py: (1) confirm every early-return deny branch calls `self._log(…)` before setting `event.cancel_tool` and returning; (2) confirm the try/except error path calls `self._log(…, decision="error")` before re-raising; (3) confirm `sim_time` is passed through from the delegations call and is `None` on all other paths (users, vendors, expenses calls do not set sim_time); (4) confirm `_log` writes all 11 fields from contracts/hook-log-schema.md and that `decision` is one of `"allow"`, `"deny"`, `"error"` at checkpoint.py

**Checkpoint**: Reading `HOOK_LOG` after any mix of tool calls shows one line per invocation, all fields present, `sim_time` non-null only on P4 paths.

---

## Phase 8: User Story 7 — Directory 5xx Errors Propagate (Priority: P2)

**Goal**: Any 5xx response from any directory endpoint raises `httpx.HTTPStatusError` out of `_before_tool_call` — not swallowed, not converted to a deny.

**Independent Test**: Point `base_url` at a server returning HTTP 500 for `/directory/users/{user}`; call an allowed tool with `acts_for != "any"`; the caller receives `httpx.HTTPStatusError` (or subclass). Same for delegations (P4), vendors (P5), and expenses (P3).

### Implementation for User Story 7

- [X] T018 [US7] Verify all four directory HTTP calls in checkpoint.py use this error-handling pattern: `if response.status_code == 404:` → handle as "not found" (apply deny rule); `else: response.raise_for_status()` — this propagates all non-404 4xx errors and all 5xx errors as `httpx.HTTPStatusError`; confirm no `try/except` inside `_before_tool_call` catches `httpx.HTTPStatusError` (only the outer `except Exception` catches it, logs `"error"`, then re-raises) at checkpoint.py

**Checkpoint**: HTTP 500/503 from any directory endpoint propagates to the test caller unchanged.

---

## Phase 9: Polish & Cross-Cutting Concerns

**Purpose**: Cleanup and validation across all user stories.

- [X] T019 [P] Add `PolicyHook.__del__(self)` for best-effort cleanup: `try: self._http.close()\nexcept Exception: pass` at checkpoint.py
- [X] T020 [P] Run `uv run pytest tests/test_contract.py -v` and fix any failures; all 6 contract tests (class exists, constructor signature, optional args, instantiation, hook registration, log fields) must pass at checkpoint.py

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — verify immediately
- **Foundational (Phase 2)**: Depends on Phase 1 completion — BLOCKS all user stories
- **US1+US2 (Phase 3)**: Depends on Phase 2 — no user story dependencies
- **US3 (Phase 4)**: Depends on Phase 2 — no user story dependencies (can be worked in parallel with Phase 3 once Phase 2 is done)
- **US4 (Phase 5)**: Depends on Phase 2; T014 and T015 can run in parallel with each other
- **US5 (Phase 6)**: Depends on Phase 2; the P6 check must be inserted at evaluation step 4 — verify ordering is correct relative to acts_for and SCOPE
- **US6 (Phase 7)**: Depends on all user story phases being complete; audit task confirms nothing was missed
- **US7 (Phase 8)**: Can be worked alongside Phases 3–6; confirms error-handling pattern used in all directory calls
- **Polish (Phase 9)**: Depends on all user story phases being complete

### User Story Dependencies

- **US1+US2 (P1)**: Can start after Foundational — no dependencies on other stories
- **US3 (P1)**: Can start after Foundational — no dependencies on other stories
- **US4 (P2)**: Can start after Foundational — no dependencies on other stories; T014/T015 are independent within the phase
- **US5 (P2)**: Can start after Foundational — P6 check insertion point is independent of P1–P5 logic
- **US6 (P2)**: Audit over all phases — best run last
- **US7 (P2)**: HTTP error-handling pattern applies to all directory calls added in US1–US4

### Within Each User Story Phase

- Foundational phase (T003–T006) must complete in task order (T003 → T004 → T005/T006 in parallel)
- Phase 3: T007 → T008 → T009 (each builds on the prior)
- Phase 5: T011 → T012 (both in `submit_expense` block, sequential); T013 (approve_expense, independent); T014/T015 in parallel (different tool blocks)
- Phase 6: T016 after T009 (inserts at a specific point in `_before_tool_call`)

### Parallel Opportunities

- T001/T002 (Phase 1) in parallel
- T005/T006 (Phase 2) in parallel
- Phases 3, 4, 5, 6, 7, 8 can proceed in parallel once Phase 2 is done (if staffed)
- T014/T015 (P4 and P5 rules) in parallel within Phase 5
- T019/T020 (Polish) in parallel

---

## Parallel Example: Phase 5 (User Story 4)

```bash
# After T011+T012 (submit_expense rules P1+P2), launch P4 and P5 together:
Task: "T014 — P4 rule: GET /directory/delegations in book_travel block at checkpoint.py"
Task: "T015 — P5 rule: GET /directory/vendors in pay_vendor block at checkpoint.py"
```

---

## Implementation Strategy

### MVP First (User Stories 1+2 Only)

1. Complete Phase 1: Setup (verify existing files)
2. Complete Phase 2: Foundational (class, constructor, register_hooks, _log)
3. Complete Phase 3: US1+US2 (P7 deny + allow path)
4. **STOP and VALIDATE**: `uv run pytest tests/test_contract.py -v` passes; call an allowed tool and confirm log records `"decision":"allow"`
5. Deploy/demo the checkpoint with basic enforcement working

### Incremental Delivery

1. Phase 1+2 → Foundation ready
2. Phase 3 (US1+US2) → Basic allow/deny works; contract tests pass (MVP)
3. Phase 4 (US3 SCOPE) → Token-based access control added
4. Phase 5 (US4 P1–P5) → Full business rule enforcement
5. Phase 6 (US5 P6) → Tamper detection for tool definitions
6. Phase 7 (US6 audit) → Log completeness verified
7. Phase 8 (US7 5xx) → Failure propagation confirmed
8. Phase 9 (Polish) → Cleanup + full test run

### Parallel Team Strategy

With multiple developers, once Phase 2 is done:

- Developer A: Phase 3 (US1+US2) + Phase 4 (US3)
- Developer B: Phase 5 (US4 P1–P5) with T014/T015 in parallel
- Developer C: Phase 6 (US5 P6) + Phase 8 (US7 5xx propagation)
- All converge for Phase 7 (US6 audit) + Phase 9 (Polish)

---

## Notes

- `checkpoint.py` is the **only** application source file produced (FR-012); all tasks target that single file
- [P] marks tasks that can run in parallel (different code sections or independent blocks)
- [Story] label maps task to its user story for traceability
- Evaluation order in `_before_tool_call` is fixed and non-negotiable: (1) blank-user, (2) allowed_tools, (3) acts_for, (4) P6 fingerprint, (5) SCOPE, (6) P1–P5 business rules
- `sim_time` is captured **only** from `GET /directory/delegations` (the only endpoint that returns it); all other paths pass `sim_time=None` to `_log`
- 404 responses → treat as "not found" → apply deny rule; non-404 non-200 → `response.raise_for_status()` (propagates 5xx)
- `jwt.decode` uses `options={"verify_signature": False, "verify_exp": False}` — signature verification is intentionally skipped per research Decision 5
- `expense_limit` absent from config → P2 does not apply; expenses of any amount pass P2
- `acts_for: "any"` → skip GET /directory/users entirely (no role check needed)
- Stop at any checkpoint to validate the story independently before moving on
