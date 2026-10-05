# Feature Specification: PolicyHook Checkpoint Module

**Feature Branch**: `001-policyhook-checkpoint`

**Created**: 2026-10-05

**Status**: Draft

**Input**: User description: "Read spec_input.md and implement the PolicyHook class in checkpoint.py. The class must read allowed_tools and fingerprints from the agent config file at run time (never hard-coded), check the bearer token scope, apply rules P1-P7, log each decision, and let directory 5xx errors propagate."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Policy Enforcement on Tool Calls (Priority: P1)

An agent operator deploys a PolicyHook for a specific agent. When an agent attempts to call a tool, the checkpoint intercepts the call, evaluates it against all configured policy rules (P1–P7 and SCOPE), and either allows or denies it — with a structured decision log entry written every time.

**Why this priority**: This is the core purpose of the module. Without working policy enforcement, the entire feature has no value.

**Independent Test**: Configure a PolicyHook with a minimal agent config YAML, point it at a running directory service stub, and trigger a tool call. Verify the allow/deny outcome matches the expected rule, and that a JSON log entry is written.

**Acceptance Scenarios**:

1. **Given** a tool name in `allowed_tools`, a valid bearer token with the correct scope, and all P1–P5 conditions satisfied, **When** the hook evaluates the tool call, **Then** the decision is `"allow"` and one log entry with `rule: null` is written.
2. **Given** a tool name not in `allowed_tools`, **When** the hook evaluates the call, **Then** the decision is `"deny"` with `rule: "P7"` and one log entry is written.
3. **Given** an empty or whitespace-only user string, **When** the hook evaluates any tool call, **Then** the decision is `"deny"` with `rule: "P7"` and one log entry is written.

---

### User Story 2 - Runtime Config Loading from Agent YAML (Priority: P1)

A team managing multiple agents needs to update which tools an agent may call or change a fingerprint without deploying new code. They edit the agent's YAML config file; the running checkpoint reads those values on each call without requiring a restart or code change.

**Why this priority**: The non-negotiable requirement that no policy-relevant data is hard-coded means runtime config loading is foundational — it underpins every other rule.

**Independent Test**: Change `allowed_tools` in the agent YAML file, then trigger another tool call. Verify the updated list is honoured.

**Acceptance Scenarios**:

1. **Given** an agent config YAML with a specific `allowed_tools` list, **When** the PolicyHook is constructed, **Then** the allowed tools list is loaded from the file (not from hard-coded values).
2. **Given** a fingerprint stored in the config for a tool, **When** the hook evaluates that tool, **Then** it computes the current tool hash and compares it to the stored value — denying with P6 if they differ.
3. **Given** an `expense_limit` set in the config, **When** a `submit_expense` call exceeds that limit with no approval reference, **Then** the call is denied with P2.

---

### User Story 3 - Bearer Token Scope Validation (Priority: P2)

A security team requires that every tool call is gated by a valid bearer token that carries the appropriate OAuth2 scope for that tool. Missing, malformed, or under-scoped tokens must be rejected before business rules are evaluated.

**Why this priority**: SCOPE is evaluated before business rules (P1–P5). A missing or invalid token means no business logic should execute.

**Independent Test**: Supply a missing token, an invalid JWT, and a valid JWT without the required scope. Verify all three produce a `"deny"` with `rule: "SCOPE"`.

**Acceptance Scenarios**:

1. **Given** no bearer token (empty string), **When** the hook evaluates any tool call, **Then** the decision is `"deny"` with `rule: "SCOPE"`.
2. **Given** a bearer token that is not a valid JWT, **When** the hook evaluates a call, **Then** the decision is `"deny"` with `rule: "SCOPE"`.
3. **Given** a valid JWT that lacks the scope required for the requested tool, **When** the hook evaluates the call, **Then** the decision is `"deny"` with `rule: "SCOPE"`.

---

### User Story 4 - Business Rule Enforcement (P1–P5) (Priority: P2)

The compliance team needs the checkpoint to enforce five business rules covering expense ownership, approval thresholds, self-approval prevention, travel delegation, and approved-vendor checks. These rules must apply at call time using only live data from the directory service.

**Why this priority**: These rules represent the core compliance requirements. They execute only after structural checks (P6, P7, SCOPE) pass.

**Independent Test**: For each of P1–P5, craft a tool call that violates exactly that rule, confirm denial, then craft one that satisfies it, confirm allow.

**Acceptance Scenarios**:

1. **Given** a `submit_expense` call where the `claimant` (after canonicalisation) differs from the acting user, **When** evaluated, **Then** deny with P1.
2. **Given** a `submit_expense` call exceeding the expense limit with no `approval_ref`, **When** evaluated, **Then** deny with P2.
3. **Given** an `approve_expense` call where the approver is the claimant, **When** evaluated, **Then** deny with P3.
4. **Given** a `book_travel` call for a different traveller with no active delegation, **When** evaluated, **Then** deny with P4.
5. **Given** a `pay_vendor` call for a vendor not in the directory's approved list, **When** evaluated, **Then** deny with P5.

---

### User Story 5 - Directory 5xx Propagation (Priority: P3)

An infrastructure team needs to know immediately when the directory service is unavailable. The checkpoint must not silently swallow server errors; it must let them propagate so the agent framework's error-handling and alerting infrastructure can observe the failure.

**Why this priority**: Silent failures would mask infrastructure problems. This is a non-negotiable operational requirement, but lower in feature priority because it only activates when the directory service is unhealthy.

**Independent Test**: Configure the directory stub to return 500 for a required route; trigger a tool call that exercises that route. Verify an HTTP error is raised (not caught) and no `"allow"` log entry is written.

**Acceptance Scenarios**:

1. **Given** the directory service returns a 5xx response on any route, **When** the hook evaluates a tool call that requires that route, **Then** the HTTP error propagates uncaught to the caller.
2. **Given** the directory service returns 404, **When** the hook fetches a resource, **Then** it treats the resource as not found and applies the relevant deny rule (no propagation for 404).

---

### Edge Cases

- What happens when the agent config YAML file is missing or malformed?
- How does the system handle a tool call when `event.selected_tool` is `None` during fingerprint check (must skip the fingerprint step entirely)?
- What happens when `acts_for` is `"any"` (must skip the role lookup entirely)?
- How does the hook behave when the `HOOK_LOG` environment variable is not set?
- What if the directory returns an unexpected JSON shape for a route (e.g., missing `role` field in a user record)?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST implement a `PolicyHook` class with the constructor signature specified in `spec_input.md` section 2.
- **FR-002**: The system MUST load `allowed_tools`, `fingerprints`, `acts_for`, and `expense_limit` from a YAML file named `{agent_name}.yaml` at construction time; these values MUST NOT be hard-coded.
- **FR-003**: The system MUST register `_before_tool_call` as a callback for `BeforeToolCallEvent` via `register_hooks`.
- **FR-004**: The system MUST evaluate policy checks in the exact order: blank-user guard → allowed_tools (P7) → acts_for role check (P7) → fingerprint check (P6) → SCOPE bearer-token check → business rules P1–P5.
- **FR-005**: The system MUST deny any tool call where the user string is empty after canonicalisation, with rule `P7`.
- **FR-006**: The system MUST deny any tool not present in the agent's `allowed_tools` list, with rule `P7`.
- **FR-007**: When `acts_for` is a role name (not `"any"`), the system MUST fetch the user record from `GET /directory/users/{user}` and deny with `P7` if the role does not match.
- **FR-008**: The system MUST compute a SHA-256 hash over the tool's `name`, `description`, and `inputSchema`, compare it against the stored fingerprint, and deny with `P6` if they differ; this step MUST be skipped when `event.selected_tool` is `None` or no fingerprint is stored for the tool.
- **FR-009**: The system MUST deny with `SCOPE` when the bearer token is absent (empty string) or cannot be decoded as a valid JWT.
- **FR-010**: The system MUST deny with `SCOPE` when the decoded JWT lacks the scope required for the requested tool, per `TOOL_SCOPE_MAP` in `domain/scopes.py`.
- **FR-011**: The system MUST apply rule P1: for `submit_expense`, the canonicalised `claimant` argument must equal the acting user; deny with P1 otherwise.
- **FR-012**: The system MUST apply rule P2: for `submit_expense`, if the amount exceeds the configured `expense_limit` and no `approval_ref` is provided, deny with P2.
- **FR-013**: The system MUST apply rule P3: for `approve_expense`, fetch the expense from `GET /directory/expenses/{expense_id}`; deny with P3 if the user is the claimant or is not the claimant's manager per `GET /directory/users/{claimant}`.
- **FR-014**: The system MUST apply rule P4: for `book_travel`, if the canonicalised `traveller` differs from the acting user, fetch delegations from `GET /directory/delegations`; check the `active` field of each returned delegation but MUST NOT perform timestamp arithmetic; deny with P4 if no active delegation covers travel from the traveller to the user.
- **FR-015**: The system MUST apply rule P5: for `pay_vendor`, fetch `GET /directory/vendors` and deny with P5 if the canonicalised `vendor` argument is not in the returned array.
- **FR-016**: The system MUST canonicalise `claimant`, `traveller`, and `vendor` arguments via `domain.identity.canonicalise` before any comparison.
- **FR-017**: The system MUST let any HTTP 5xx error from the directory propagate uncaught; 404 responses MUST be treated as "not found" and trigger the relevant deny rule.
- **FR-018**: Every tool-call evaluation MUST produce one structured JSON log entry appended to the file path held in the `HOOK_LOG` environment variable, with fields: `call_id`, `run_id`, `timestamp`, `sim_time`, `user`, `agent`, `tool`, `arguments`, `decision`, `rule`, `reason`.
- **FR-019**: The `sim_time` field MUST be populated from the directory response when a directory lookup is performed; it MUST be `null` when no directory call was made for the decision.
- **FR-020**: The system MUST only import from the Python standard library, `httpx`, `PyJWT`, `PyYAML`, `strands`, and files provided in the kit.
- **FR-021**: The generated output MUST be a single file: `checkpoint.py`.

### Key Entities

- **PolicyHook**: The central class; holds agent name, user identity, directory base URL, run ID, optional limit override, config directory, and bearer token. Evaluates tool calls against policy rules and writes decision log entries.
- **Agent Config**: A YAML file per agent (`{agent_name}.yaml`) containing `allowed_tools`, `fingerprints`, `acts_for`, and optionally `expense_limit`. Loaded at construction time.
- **Decision Log Entry**: A single-line JSON object written per tool-call evaluation containing outcome, rule, and context fields.
- **Directory Service**: An HTTP service exposing user, delegation, vendor, and expense routes. Provides all live policy facts at call time.
- **Bearer Token**: A JWT passed at construction time; validated for well-formedness and tool-specific scope before business rules execute.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: All seven policy rules (P1–P7) and SCOPE are enforced in the correct order; a test suite covering each rule independently passes with 100% of cases producing the correct allow/deny decision.
- **SC-002**: No policy-relevant value (tool lists, fingerprints, roles, expense limits) appears as a literal in `checkpoint.py`; automated static analysis finds zero hard-coded policy data.
- **SC-003**: Every tool-call evaluation produces exactly one valid JSON log entry matching the defined schema; no evaluation exits without a log entry.
- **SC-004**: When the directory service returns a 5xx response, the HTTP error is observable by the caller; zero 5xx responses are silently swallowed in any test scenario.
- **SC-005**: The `checkpoint.py` file imports no third-party library outside the approved set; dependency audit passes with zero violations.
- **SC-006**: Argument canonicalisation is applied before every identity comparison; test cases with untrimmed or mixed-case inputs produce the same decisions as their normalised equivalents.

## Assumptions

- The `strands` framework and `BeforeToolCallEvent` / `HookProvider` API are available and stable in the `.venv` provided in the project folder.
- The `domain.identity.canonicalise`, `domain/scopes.py` (`TOOL_SCOPE_MAP`), and other kit files are present and importable at run time.
- The `HOOK_LOG` environment variable will be set to a writable file path in production; if unset, the checkpoint's behaviour is not specified (assumption: it may raise or no-op, to be resolved during implementation).
- The `config_dir` parameter defaults to a path relative to the module file when `None` is passed.
- The `limit_override` constructor parameter overrides the `expense_limit` from the config when provided.
- Bearer token JWT decoding uses the `PyJWT` library without signature verification (scope check only), unless the kit's `domain/scopes.py` specifies otherwise.
- The directory service is trusted; its responses are accepted as authoritative without further validation beyond checking `active` fields.
- Generator-produced tests are placed in `tests/generated/` and are not imported by the production pipeline.
