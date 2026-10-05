# Feature Specification: PolicyHook Checkpoint Enforcement

**Feature Branch**: `001-policy-hook-checkpoint`

**Created**: 2026-10-05

**Status**: Draft

**Input**: User description: "Read spec_input.md and implement the PolicyHook class in checkpoint.py. The class must read allowed_tools and fingerprints from the agent config file at run time (never hard-coded), check the bearer token scope, apply rules P1-P7, log each decision, and let directory 5xx errors propagate."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Legitimate Tool Call Is Allowed (Priority: P1)

An agent acting on behalf of a user calls an approved tool with a valid bearer token, valid fingerprint, and arguments that satisfy all business rules. The checkpoint evaluates all rules in order, finds no violations, logs the decision as `allow`, and permits the tool call to proceed.

**Why this priority**: This is the happy path — the system is useless if it blocks every legitimate call. All other stories depend on a working allow path.

**Independent Test**: Can be fully tested by calling a configured allowed tool with a well-formed JWT bearer token and compliant arguments; the call proceeds and the log records `"decision": "allow"`.

**Acceptance Scenarios**:

1. **Given** a user with the correct role submits a tool call for an allowed tool with a valid JWT, correct fingerprint, and arguments satisfying P1–P5, **When** the checkpoint evaluates the call, **Then** the call is allowed and a single JSON log entry with `"decision": "allow"` and `"rule": null` is appended to the HOOK_LOG path.
2. **Given** an agent whose `acts_for` is `"any"`, **When** any authenticated user calls an allowed tool, **Then** no role look-up is needed and the call is allowed when all other rules pass.

---

### User Story 2 - Disallowed Tool Is Blocked (Priority: P1)

An agent receives a tool call for a tool not listed in its `allowed_tools` configuration. The checkpoint reads the tool list from the YAML config at run time, finds the tool absent, and denies the call with P7 before evaluating any other rule.

**Why this priority**: Default-deny (P7) is the foundational safety mechanism. Without it, any unlisted tool bypasses all controls.

**Independent Test**: Can be fully tested by calling a tool name not present in `allowed_tools`; the call is denied immediately and the log records `"decision": "deny"` and `"rule": "P7"`.

**Acceptance Scenarios**:

1. **Given** the agent config lists `["submit_expense", "approve_expense"]`, **When** a call arrives for `book_travel`, **Then** the call is denied with rule `P7` and no directory HTTP requests are made.
2. **Given** the agent config is updated at run time to remove a tool, **When** the next call for that tool arrives, **Then** the updated list is used (config was read at construction time) and the call is denied.

---

### User Story 3 - Bearer Token Scope Enforced (Priority: P1)

A call arrives with a missing, malformed, or insufficiently-scoped JWT bearer token. The checkpoint decodes the token, checks whether the required scope for the tool is present, and denies with SCOPE if the check fails.

**Why this priority**: Bearer token validation prevents unauthorised callers from invoking tools they are not permitted to use.

**Independent Test**: Can be tested by submitting a call with an absent or expired token, then with a valid token missing the required scope; both are denied with `"rule": "SCOPE"`.

**Acceptance Scenarios**:

1. **Given** the bearer token is an empty string, **When** the checkpoint processes the call, **Then** the call is denied with rule `SCOPE` before reaching P1–P5 checks.
2. **Given** a valid JWT that lacks the scope required for the requested tool (per TOOL_SCOPE_MAP), **When** the checkpoint processes the call, **Then** the call is denied with rule `SCOPE`.
3. **Given** a well-formed JWT with the correct scope, **When** the checkpoint processes the call, **Then** the SCOPE check passes and evaluation continues to P1–P5.

---

### User Story 4 - Business Rules P1–P5 Enforced (Priority: P2)

The checkpoint enforces the five business rules (own expenses, expense limits, no self-approval, live travel delegation, approved vendor list) by fetching live data from the directory on every call.

**Why this priority**: These rules represent the core compliance requirements. Each maps to a concrete financial or governance control.

**Independent Test**: Each rule can be tested independently: submit an expense with a different claimant (P1), submit an oversized expense without approval ref (P2), approve your own expense (P3), book travel without an active delegation (P4), pay an unlisted vendor (P5).

**Acceptance Scenarios**:

1. **Given** user A submits an expense naming user B as claimant, **When** the checkpoint applies P1, **Then** the call is denied with `"rule": "P1"`.
2. **Given** an expense amount exceeds `expense_limit` and no `approval_ref` is provided, **When** the checkpoint applies P2, **Then** the call is denied with `"rule": "P2"`.
3. **Given** the claimant of an expense is the same as the approving user, **When** the checkpoint fetches the expense record and applies P3, **Then** the call is denied with `"rule": "P3"`.
4. **Given** a user tries to book travel for another user with no active delegation, **When** the checkpoint fetches delegations and applies P4, **Then** the call is denied with `"rule": "P4"`.
5. **Given** a pay_vendor call names a vendor not in the live approved list, **When** the checkpoint fetches the vendor list and applies P5, **Then** the call is denied with `"rule": "P5"`.

---

### User Story 5 - Tool Fingerprint Validated (Priority: P2)

Before executing an allowed tool, the checkpoint computes the SHA-256 hash of the tool's name, description, and input schema, and compares it against the fingerprint stored in the agent config file. A mismatch (indicating the tool definition changed since review) causes a P6 deny.

**Why this priority**: Fingerprint validation prevents an agent from being tricked into using a silently-modified tool definition.

**Independent Test**: Can be tested by changing a tool's description and calling it; the fingerprint no longer matches and the call is denied with `"rule": "P6"`.

**Acceptance Scenarios**:

1. **Given** `event.selected_tool` is not None and its fingerprint is stored in the config, **When** the computed hash differs from the stored value, **Then** the call is denied with `"rule": "P6"`.
2. **Given** `event.selected_tool` is `None` or no fingerprint is stored for the tool, **When** the checkpoint evaluates P6, **Then** the fingerprint check is skipped entirely and evaluation continues.

---

### User Story 6 - Every Call Produces an Audit Log Entry (Priority: P2)

For every tool-call evaluation — whether allowed, denied, or errored — the checkpoint appends a single structured JSON entry to the path held in `HOOK_LOG`. The entry contains all required fields.

**Why this priority**: The audit log is the only persistent record of enforcement decisions. Completeness is mandatory for audit trails.

**Independent Test**: Can be tested by making any tool call and reading the HOOK_LOG file; each call must produce exactly one new JSON-on-a-line entry with all required fields present.

**Acceptance Scenarios**:

1. **Given** any tool-call evaluation completes (allow or deny), **When** the log is read, **Then** exactly one new line is appended with valid JSON containing `call_id`, `run_id`, `timestamp`, `sim_time`, `user`, `agent`, `tool`, `arguments`, `decision`, `rule`, and `reason`.
2. **Given** a directory call was needed during evaluation, **When** the log entry is written, **Then** `sim_time` contains the simulation clock value from the directory response.
3. **Given** no directory call was needed, **When** the log entry is written, **Then** `sim_time` is `null`.

---

### User Story 7 - Directory 5xx Errors Propagate (Priority: P2)

When any directory HTTP endpoint returns a 5xx status code during a tool-call evaluation, the checkpoint does not catch or suppress the error. The error propagates to the Strands framework, which can observe the failure and take appropriate action.

**Why this priority**: Silently swallowing server errors could mask a compromised or unavailable directory, leading to incorrect enforcement decisions.

**Independent Test**: Can be tested by making the directory return a 500 response; the error must propagate out of the checkpoint and reach the test caller.

**Acceptance Scenarios**:

1. **Given** the directory returns HTTP 500 during a user lookup, **When** the checkpoint calls that endpoint, **Then** the HTTP error is not caught and propagates to the caller.
2. **Given** the directory returns HTTP 503 during a vendor list fetch, **When** the checkpoint calls that endpoint, **Then** the error propagates unchanged.

---

### Edge Cases

- What happens when the acting user is an empty string or contains only whitespace? → Denied with P7 (blank-user guard) before any other check.
- How does the system handle a 404 from `GET /directory/users/{user}`? → Treated as "not found"; deny with P7 for acts_for check.
- What if `expense_limit` is not set in the agent config? → No P2 limit is applied; expenses of any amount are allowed if they pass other rules.
- What if the same tool appears in `fingerprints` but `event.selected_tool` is None? → Fingerprint check is skipped entirely.
- What if an approved expense amount exceeds the limit but `approval_ref` is provided? → P2 is satisfied; evaluation continues.
- What if a delegation record has `active: false`? → That delegation is disregarded; P4 is denied if no active delegation remains.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The `PolicyHook` class MUST implement `HookProvider` and register `_before_tool_call` as a `BeforeToolCallEvent` callback via `register_hooks`.
- **FR-002**: The constructor MUST load the agent's YAML configuration file from `config_dir` (or a default path relative to the module file) at construction time, reading `allowed_tools`, `fingerprints`, `acts_for`, and `expense_limit` from the file.
- **FR-003**: The checkpoint MUST evaluate tool calls in this fixed order: (1) blank-user guard, (2) allowed_tools check, (3) acts_for role check, (4) P6 fingerprint check, (5) SCOPE bearer-token check, (6) P1–P5 business rules.
- **FR-004**: All user, role, delegation, vendor, and expense facts MUST be fetched at run time from the directory HTTP routes on every call; none may be hard-coded.
- **FR-005**: The `claimant`, `traveller`, and `vendor` arguments MUST be canonicalised via `domain.identity.canonicalise` before any comparison.
- **FR-006**: HTTP 5xx responses from the directory MUST NOT be caught or suppressed; they MUST propagate to the framework.
- **FR-007**: HTTP 404 responses from the directory MUST be treated as "not found" and trigger the applicable deny rule.
- **FR-008**: Every tool-call evaluation MUST append exactly one JSON log entry to the path held in the `HOOK_LOG` environment variable.
- **FR-009**: The bearer token MUST be decoded as a valid JWT; a missing or invalid token MUST result in a SCOPE deny. A valid token lacking the required scope (per `TOOL_SCOPE_MAP` in `domain/scopes.py`) MUST also result in a SCOPE deny.
- **FR-010**: The P6 fingerprint check MUST compute SHA-256 over the tool's `name`, `description`, and `inputSchema`, and compare against the stored value; the check MUST be skipped if `event.selected_tool` is `None` or no fingerprint is stored.
- **FR-011**: The checkpoint MUST only import from allowed dependencies: Python standard library, `httpx`, `PyJWT`, `PyYAML`, `strands`, and project-local files.
- **FR-012**: `checkpoint.py` MUST be the only application source file produced.

### Key Entities

- **PolicyHook**: The central enforcement component; registered with the Strands agent; loaded with agent config at construction time; evaluates every tool call before execution.
- **Agent Config (YAML)**: Per-agent static configuration file (`{agent_name}.yaml`) containing `allowed_tools`, `fingerprints`, `acts_for`, and optionally `expense_limit`; read once at construction.
- **Decision Log Entry**: A JSON-on-a-line record appended to `HOOK_LOG` for every evaluation; contains all audit fields.
- **Directory**: The external HTTP service providing live facts (users, delegations, vendors, expenses) via well-defined routes.
- **Bearer Token**: A JWT passed to the constructor; decoded at evaluation time to verify scope.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Every call to an allowed tool that satisfies all rules completes without being blocked by the checkpoint; no false positives in a representative test suite.
- **SC-002**: Every call that violates a rule (P1–P7 or SCOPE) is blocked at the correct step; the log records the correct rule identifier.
- **SC-003**: The audit log is 100% complete — every tool-call evaluation, regardless of outcome, produces exactly one log entry with all required fields populated.
- **SC-004**: Directory 5xx errors surface to the test caller in 100% of cases; none are suppressed.
- **SC-005**: The enforcement check sequence is deterministic — given identical inputs, the checkpoint always evaluates rules in the same fixed order and reaches the same decision.
- **SC-006**: No agent-specific facts (user roles, vendor names, delegation records) are embedded in `checkpoint.py`; all come from the YAML config or directory calls.

## Assumptions

- The Strands framework's `HookProvider` interface and `BeforeToolCallEvent` are available via the installed `strands` package in `.venv`.
- `domain/scopes.py` with a `TOOL_SCOPE_MAP` dict and `domain/identity.py` with a `canonicalise` function exist as project-local files and are importable from `checkpoint.py`.
- The `HOOK_LOG` environment variable is always set when the checkpoint runs; the checkpoint does not need to handle a missing `HOOK_LOG`.
- The agent YAML config file uses UTF-8 encoding and is valid YAML; the checkpoint does not need to recover from a malformed config.
- The directory service is accessible at the `base_url` provided to the constructor; network availability is the responsibility of the calling environment, not the checkpoint.
- `run_id` may be `None`; the log entry will record `null` for `run_id` in that case.
- The `expense_limit` field in the agent config is an integer when present; a missing field means no limit applies.
