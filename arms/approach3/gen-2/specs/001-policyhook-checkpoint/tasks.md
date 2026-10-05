---

description: "Task list for PolicyHook Checkpoint Module implementation"
---

# Tasks: PolicyHook Checkpoint Module

**Input**: Design documents from `/specs/001-policyhook-checkpoint/`

**Prerequisites**: plan.md, spec.md, data-model.md, contracts/policyhook-interface.md, research.md, quickstart.md

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story. All implementation lands in a single file (`checkpoint.py`) at repo root; config YAML files are independent and marked `[P]`.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1–US5)

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Verify the project environment and create the `checkpoint.py` file with the required import block.

- [X] T001 Verify domain kit files are present and importable: confirm `domain/identity.py` (exports `canonicalise`), `domain/scopes.py` (exports `TOOL_SCOPE_MAP`) exist at repo root
- [X] T002 Create `checkpoint.py` at repo root with full import block: stdlib (`hashlib`, `json`, `os`, `pathlib`, `uuid`, `datetime`), `httpx`, `jwt`, `yaml`, `strands` (`HookProvider`, `HookRegistry`, `BeforeToolCallEvent`), `from domain.identity import canonicalise`, `from domain.scopes import TOOL_SCOPE_MAP`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Implement the PolicyHook class skeleton, constructor, YAML config loading, httpx client, hook registration, and decision log writer. These must be complete before any user story can be implemented.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T003 Implement `PolicyHook(HookProvider)` class skeleton with constructor signature `__init__(self, agent_name: str, user: str, base_url: str, run_id: str | None = None, limit_override: int | None = None, config_dir: pathlib.Path | None = None, bearer_token: str = "") -> None` in `checkpoint.py`
- [X] T004 Implement YAML config loading in `PolicyHook.__init__`: default `config_dir` to `pathlib.Path(__file__).parent / "config" / "agents"` when `None`; load `{config_dir}/{agent_name}.yaml` via `yaml.safe_load`; extract `allowed_tools` (list[str], required — raise `KeyError` if missing), `fingerprints` (dict[str, str], required — raise `KeyError` if missing), `acts_for` (str, required — raise `KeyError` if missing), `expense_limit` (int or None, optional); apply `limit_override` to replace `expense_limit` when `limit_override` is not `None` in `checkpoint.py`
- [X] T005 Implement `httpx.Client(base_url=base_url)` creation assigned to `self._client` at construction time in `checkpoint.py`
- [X] T006 Implement `register_hooks(self, registry: HookRegistry, **kwargs) -> None` calling `registry.add_callback(BeforeToolCallEvent, self._before_tool_call)` in `checkpoint.py`
- [X] T007 Implement `_write_log` helper in `checkpoint.py`: read `os.environ["HOOK_LOG"]` — raise `RuntimeError` with clear message if unset; open file in append mode and write `json.dumps(entry, default=str) + "\n"` where `entry` contains all 11 required fields: `call_id`, `run_id`, `timestamp` (ISO-8601 UTC via `datetime.utcnow().isoformat() + "Z"`), `sim_time`, `user`, `agent`, `tool`, `arguments`, `decision`, `rule`, `reason`

**Checkpoint**: Constructor loads config, client ready, hook registered, log writer functional — user story work can now begin.

---

## Phase 3: User Stories 1 & 2 — Policy Enforcement + Runtime Config Loading (Priority: P1) 🎯 MVP

**Goal**: `_before_tool_call` evaluates the first four policy checks (blank-user, allowed_tools, acts_for role, fingerprint) in the fixed order defined in data-model.md, writes one log entry per call, and cancels the tool call on any deny.

**Independent Test**: Construct `PolicyHook` with a minimal agent YAML (`acts_for: any`, one allowed tool), point it at a stub directory server, call `_before_tool_call` with a `FakeEvent`. Verify `event.cancel_tool` is falsy on allow and a non-empty string on deny, and that exactly one JSON log entry is written to `HOOK_LOG`.

### Implementation for User Stories 1 & 2

- [X] T008 [US1] Implement `_before_tool_call(self, event: BeforeToolCallEvent) -> None` skeleton in `checkpoint.py`: extract `tool_name = event.tool_use["name"]` and `args = event.tool_use["input"]`; initialise `sim_time = None` and `call_id = str(uuid.uuid4())`
- [X] T009 [US1] Implement step 1 — blank-user guard in `checkpoint.py`: `if not canonicalise(self.user)` → call `_write_log` with `decision="deny"`, `rule="P7"`, `reason="blank user"`, set `event.cancel_tool = "P7: blank user"`, return
- [X] T010 [US1] Implement step 2 — allowed_tools check in `checkpoint.py`: `if tool_name not in self.allowed_tools` → write log with `decision="deny"`, `rule="P7"`, `reason=f"tool {tool_name!r} not in allowed_tools"`, set `event.cancel_tool`, return
- [X] T011 [US1] [US2] Implement step 3 — acts_for role check in `checkpoint.py`: `if self.acts_for != "any"`, call `self._client.get(f"/directory/users/{self.user}").raise_for_status()` (5xx propagates; on 404 deny P7 "user not found"); on successful response compare `response.json()["role"]` to `self.acts_for`; deny P7 with reason "role mismatch" if they differ; write log and set `event.cancel_tool` on deny
- [X] T012 [US1] [US2] Implement step 4 — fingerprint check in `checkpoint.py`: `if event.selected_tool is not None and tool_name in self.fingerprints`, compute `hashlib.sha256(json.dumps({"description": event.selected_tool.tool_spec["description"], "inputSchema": event.selected_tool.tool_spec["inputSchema"], "name": event.selected_tool.tool_spec["name"]}, sort_keys=True).encode()).hexdigest()`; if digest != `self.fingerprints[tool_name]` → deny P6 with reason "fingerprint mismatch", write log, set `event.cancel_tool`, return; skip entirely when `event.selected_tool is None` or tool not in `self.fingerprints`
- [X] T013 [P] [US2] Create `config/agents/expense-assistant.yaml` with `acts_for: any`, `allowed_tools: [submit_expense, approve_expense, read_receipt]`, `fingerprints: {}`, `expense_limit: 500`
- [X] T014 [P] [US2] Create `config/agents/travel-assistant.yaml` with `acts_for: any`, `allowed_tools: [book_travel, read_itinerary]`, `fingerprints: {}`, `expense_limit: null` (or omit field)
- [X] T015 [P] [US2] Create `config/agents/payments-agent.yaml` with `acts_for: any`, `allowed_tools: [pay_vendor, list_vendors]`, `fingerprints: {}`, no `expense_limit`

**Checkpoint**: User Stories 1 & 2 independently functional — config is runtime-loaded, first four checks enforce deny-by-default, every evaluation writes a log entry.

---

## Phase 4: User Story 3 — Bearer Token Scope Validation (Priority: P2)

**Goal**: Step 5 of the check order (SCOPE) rejects missing, malformed, and under-scoped tokens before any business logic executes.

**Independent Test**: Instantiate `PolicyHook` with an empty `bearer_token`, an invalid JWT string, and a valid JWT missing the required scope. All three `_before_tool_call` invocations must produce `decision="deny"`, `rule="SCOPE"` in the log and a non-empty `event.cancel_tool`.

### Implementation for User Story 3

- [X] T016 [US3] Implement step 5 — SCOPE bearer-token check in `checkpoint.py`: `if not self.bearer_token` → deny SCOPE "missing token"; else attempt `jwt.decode(self.bearer_token, options={"verify_signature": False})`; on `jwt.DecodeError` → deny SCOPE "malformed token"; compare decoded `scope` claim (split on space) against `TOOL_SCOPE_MAP.get(tool_name, "")` from `domain.scopes`; deny SCOPE "insufficient scope" if required scope not present; write log and set `event.cancel_tool` on deny, return

**Checkpoint**: User Story 3 independently functional — all three SCOPE denial scenarios produce the correct log entry.

---

## Phase 5: User Story 4 — Business Rule Enforcement P1–P5 (Priority: P2)

**Goal**: Step 6 of the check order applies the five business rules for `submit_expense` (P1, P2), `approve_expense` (P3), `book_travel` (P4), and `pay_vendor` (P5) using live directory data; all other tools reach the allow path.

**Independent Test**: For each rule P1–P5, craft a violating `FakeEvent` and verify `decision="deny"` with the correct rule; craft a passing `FakeEvent` and verify `decision="allow"` with `rule=null`. Confirm `canonicalise` is applied to `claimant`, `traveller`, and `vendor` before comparison (SC-006).

### Implementation for User Story 4

- [X] T017 [US4] Implement business rule dispatch skeleton in `checkpoint.py`: after SCOPE check, `if tool_name == "submit_expense"` → call P1/P2 handler; `elif tool_name == "approve_expense"` → call P3 handler; `elif tool_name == "book_travel"` → call P4 handler; `elif tool_name == "pay_vendor"` → call P5 handler; `else` → write log with `decision="allow"`, `rule=None`, return
- [X] T018 [US4] Implement P1 check for `submit_expense` in `checkpoint.py`: `if canonicalise(args.get("claimant", "")) != canonicalise(self.user)` → deny P1 "claimant does not match acting user", write log, set `event.cancel_tool`, return
- [X] T019 [US4] Implement P2 check for `submit_expense` in `checkpoint.py`: `if self.expense_limit is not None and args.get("amount", 0) > self.expense_limit and not args.get("approval_ref")` → deny P2 "amount exceeds limit without approval reference", write log, set `event.cancel_tool`, return; if both P1 and P2 pass → write allow log
- [X] T020 [US4] Implement P3 check for `approve_expense` in `checkpoint.py`: `GET /directory/expenses/{args["expense_id"]}` via `self._client`; on 404 → deny P3 "expense not found"; extract `claimant = canonicalise(response.json()["claimant"])`; if `claimant == canonicalise(self.user)` → deny P3 "approver is the claimant"; fetch `GET /directory/users/{claimant}`; if `response.json()["manager"] != self.user` → deny P3 "approver is not claimant's manager"; otherwise write allow log
- [X] T021 [US4] Implement P4 check for `book_travel` in `checkpoint.py`: `if canonicalise(args.get("traveller", "")) != canonicalise(self.user)`, fetch `GET /directory/delegations`; extract `sim_time` from `response.json().get("sim_time")`; check each delegation in `response.json()["delegations"]` for `active == True`, `delegator == canonicalise(traveller)`, `delegate == canonicalise(self.user)`, and `"travel" in delegation["scope"]`; deny P4 "no active travel delegation" if none found; write allow log if delegation found; update `sim_time` variable from response
- [X] T022 [US4] Implement P5 check for `pay_vendor` in `checkpoint.py`: fetch `GET /directory/vendors`; response is a JSON array; `if canonicalise(args.get("vendor", "")) not in [canonicalise(v) for v in response.json()]` → deny P5 "vendor not in approved list"; otherwise write allow log

**Checkpoint**: User Story 4 independently functional — each of P1–P5 produces the correct allow/deny decision; all argument comparisons use canonicalised values.

---

## Phase 6: User Story 5 — Directory 5xx Propagation (Priority: P3)

**Goal**: HTTP 5xx errors from the directory service propagate uncaught to the caller; 404 responses are handled inline as "not found" denials. No `"allow"` log entry is written when a 5xx occurs.

**Independent Test**: Configure a mock directory to return 500 on any route; trigger a tool call that exercises that route; verify `httpx.HTTPStatusError` propagates and no `"allow"` entry appears in `HOOK_LOG`.

### Implementation for User Story 5

- [X] T023 [US5] Audit all `self._client.get(...)` calls in `checkpoint.py`: each must call `.raise_for_status()` immediately after the response is received so 5xx errors surface as `httpx.HTTPStatusError`; implement 404 detection via `response.status_code == 404` check before calling `raise_for_status()` or catching `httpx.HTTPStatusError` with a status check — **do not** use a bare `except httpx.HTTPStatusError` that would swallow 5xx; the pattern for each HTTP call is: `response = self._client.get(...); if response.status_code == 404: <deny>; else: response.raise_for_status(); <process response>`

**Checkpoint**: User Story 5 complete — 5xx errors are never silently swallowed; 404 triggers the correct deny path without raising.

---

## Phase N: Polish & Cross-Cutting Concerns

**Purpose**: Validation passes to confirm the completed `checkpoint.py` satisfies all success criteria.

- [X] T024 [P] Validate no hardcoded policy data: `grep -n "allowed_tools\|fingerprint\|acts_for\|expense_limit" checkpoint.py` must return zero literal policy values (SC-002)
- [X] T025 [P] Validate allowed imports only in `checkpoint.py`: parse import nodes with `ast` and confirm all references are within the approved set from `contracts/policyhook-interface.md §8` — stdlib, `httpx`, `jwt`, `yaml`, `strands`, `domain.identity`, `domain.scopes` (SC-005)
- [X] T026 Run `uv run pytest tests/test_contract.py -v` and confirm all 7 contract tests pass (SC-001)
- [X] T027 Run quickstart.md end-to-end scenarios: start `domain/server.py`, set `HOOK_LOG`, construct `PolicyHook("expense-assistant", ...)`, run the five scenarios from `quickstart.md`, verify each produces the documented outcome

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion — **BLOCKS all user stories**
- **US1 & US2 (Phase 3)**: Depends on Foundational completion; these two stories are tightly coupled (config loading underpins every check) — implement together as a single phase
- **US3 (Phase 4)**: Depends on Phase 3 checkpoint (blank-user, allowed_tools, acts_for, fingerprint checks must work before SCOPE is layered in)
- **US4 (Phase 5)**: Depends on Phase 4 (SCOPE must pass before business rules execute)
- **US5 (Phase 6)**: Depends on Phase 5 (HTTP calls must be in place to audit raise_for_status usage)
- **Polish (Phase N)**: Depends on all prior phases complete

### User Story Dependencies

- **US1 + US2 (P1)**: Start after Foundational — no external story dependencies; implement as one unit
- **US3 (P2)**: Depends on US1+US2 phase complete (SCOPE slot is step 5 in the fixed order)
- **US4 (P2)**: Depends on US3 phase complete (business rules are step 6; SCOPE is step 5)
- **US5 (P3)**: Depends on US4 phase complete (HTTP calls introduced in US4 are the primary 5xx surface)

### Within Each Phase

- YAML config files (T013–T015) are independent of `checkpoint.py` tasks and marked `[P]`
- All `checkpoint.py` tasks within a phase are sequential (single file, no intra-phase parallelism)

### Parallel Opportunities

- T013, T014, T015 (YAML config files) can run in parallel with each other and with any `checkpoint.py` task that does not yet read them
- T024, T025 (validation passes) can run in parallel in the Polish phase

---

## Parallel Example: Phase 3 (US1 & US2)

```bash
# Run these in parallel while implementing checkpoint.py steps T008–T012:
Task: "Create config/agents/expense-assistant.yaml" (T013)
Task: "Create config/agents/travel-assistant.yaml"  (T014)
Task: "Create config/agents/payments-agent.yaml"    (T015)

# Sequential in checkpoint.py (single file, strict check order):
T008 → T009 → T010 → T011 → T012
```

---

## Implementation Strategy

### MVP First (US1 + US2 only — Phase 1–3)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: US1 + US2 (policy enforcement + runtime config)
4. **STOP and VALIDATE**: Trigger allow and deny scenarios with `FakeEvent`; confirm log entries
5. Deploy/demo minimal working hook

### Incremental Delivery

1. Setup + Foundational → class skeleton ready
2. Add US1 + US2 → blank-user, allowed_tools, acts_for, fingerprint checks → **MVP checkpoint**
3. Add US3 → SCOPE gate working → bearer-token security layer complete
4. Add US4 → full business logic → compliance-ready
5. Add US5 → 5xx propagation confirmed → production-grade error visibility
6. Polish → validation passes confirm all success criteria (SC-001 through SC-006)

---

## Notes

- `[P]` tasks touch different files (YAML configs vs `checkpoint.py`) — safe to parallelise
- `[US?]` labels map tasks to user stories from spec.md for traceability
- All check-order steps (T009–T012, T016, T018–T022) correspond directly to the state machine in data-model.md
- `canonicalise` must be applied to `claimant`, `traveller`, and `vendor` in T018, T021, T022 — constitution Principle VII
- `event.cancel_tool = reason_string` is the denial mechanism per research.md Decision 1
- `sim_time` is populated only from the delegations route response (research.md Decision 7)
- 5xx errors must propagate uncaught per constitution Principle IV; the pattern in T023 achieves this
