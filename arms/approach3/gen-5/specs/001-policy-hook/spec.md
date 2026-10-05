# Feature Specification: PolicyHook Class — Agent Checkpoint Enforcement

**Feature Branch**: `001-policy-hook`

**Created**: 2026-10-05

**Status**: Draft

**Input**: User description: "Read spec_input.md and implement the PolicyHook class in checkpoint.py. The class must read allowed_tools and fingerprints from the agent config file at run time (never hard-coded), check the bearer token scope, apply rules P1-P7, log each decision, and let directory 5xx errors propagate."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Permitted Tool Call Allowed (Priority: P1)

A Strands agent user with a valid bearer token, whose tool is listed in the agent's YAML configuration and whose fingerprint matches, successfully calls an allowed tool. Every enforcement check passes and the tool executes. A structured log entry is written.

**Why this priority**: The allow path is the baseline; every other scenario is a variant of it. Without a working allow path there is no meaningful policy enforcement.

**Independent Test**: Deploy the hook against a test agent YAML with one allowed tool, send a well-formed event with a valid token and matching fingerprint, and assert the tool call proceeds and one log line is written.

**Acceptance Scenarios**:

1. **Given** a user with a non-empty identity, a valid JWT with the required scope, a tool in `allowed_tools`, and a matching fingerprint, **When** `_before_tool_call` is invoked, **Then** the call is allowed, the log entry has `decision: "allow"` and `rule: null`.
2. **Given** a user booking travel for themselves (traveller == user), **When** `_before_tool_call` is invoked for `book_travel`, **Then** no delegation lookup is made and the call is allowed.

---

### User Story 2 - Policy Rules P1–P7 Deny Unwanted Actions (Priority: P1)

A Strands agent user attempts an action that violates one of the seven policy rules (e.g., submitting an expense for another person, exceeding the expense limit without approval, self-approving, booking travel without delegation, paying an unlisted vendor, mismatched fingerprint, or calling an unlisted tool). The checkpoint denies the call, logs the denial with the correct rule code, and does not execute the tool.

**Why this priority**: Policy enforcement is the core value delivered by this feature. Each rule must fire independently and be traceable in the log.

**Independent Test**: For each rule P1–P7, construct a minimal event that should be denied, invoke the hook, and assert `decision: "deny"`, `rule: "P<N>"` in the log.

**Acceptance Scenarios**:

1. **Given** `submit_expense` with `claimant` ≠ acting user, **When** the hook evaluates, **Then** decision is `"deny"` and rule is `"P1"`.
2. **Given** `submit_expense` with amount > `expense_limit` and no `approval_ref`, **When** the hook evaluates, **Then** decision is `"deny"` and rule is `"P2"`.
3. **Given** `approve_expense` where the acting user is the claimant, **When** the hook evaluates, **Then** decision is `"deny"` and rule is `"P3"`.
4. **Given** `book_travel` with `traveller` ≠ user and no active delegation, **When** the hook evaluates, **Then** decision is `"deny"` and rule is `"P4"`.
5. **Given** `pay_vendor` with a vendor not in the live vendor list, **When** the hook evaluates, **Then** decision is `"deny"` and rule is `"P5"`.
6. **Given** a tool whose current definition hash does not match the stored fingerprint, **When** the hook evaluates, **Then** decision is `"deny"` and rule is `"P6"`.
7. **Given** a tool name not in `allowed_tools`, **When** the hook evaluates, **Then** decision is `"deny"` and rule is `"P7"`.
8. **Given** an acting user that is an empty string after canonicalisation, **When** the hook evaluates, **Then** decision is `"deny"` and rule is `"P7"`.

---

### User Story 3 - Bearer Token Scope Enforcement (Priority: P2)

A Strands agent user attempts a tool call with a missing, malformed, or insufficiently-scoped JWT bearer token. The checkpoint denies the call with rule code `SCOPE` before any business-rule check runs.

**Why this priority**: Token scope is a security gate that must fire before business logic; a broken token should never reach P1–P5 checks.

**Independent Test**: Pass an empty token, a non-JWT string, and a valid JWT missing the required scope; assert `decision: "deny"`, `rule: "SCOPE"` for each.

**Acceptance Scenarios**:

1. **Given** a missing (empty string) bearer token, **When** the hook evaluates, **Then** decision is `"deny"` and rule is `"SCOPE"`.
2. **Given** a bearer token that is not a valid JWT, **When** the hook evaluates, **Then** decision is `"deny"` and rule is `"SCOPE"`.
3. **Given** a valid JWT that lacks the scope required for the requested tool, **When** the hook evaluates, **Then** decision is `"deny"` and rule is `"SCOPE"`.

---

### User Story 4 - Directory 5xx Errors Propagate (Priority: P2)

When the directory HTTP service returns a 5xx response during any lookup (user record, delegations, vendor list, expense), the checkpoint does not swallow the error. The framework receives the exception, enabling alerting and retry at a higher level.

**Why this priority**: Silencing infrastructure failures creates false-allow scenarios that violate the fail-closed requirement.

**Independent Test**: Mock the directory to return 500 for each route; assert the hook raises (does not return a deny log entry silently).

**Acceptance Scenarios**:

1. **Given** the directory returns HTTP 500 on `GET /directory/users/{user}`, **When** the hook calls the route, **Then** the exception propagates to the caller and is not caught by the hook.
2. **Given** the directory returns HTTP 503 on `GET /directory/vendors`, **When** the hook calls the route, **Then** the exception propagates.

---

### User Story 5 - Decision Log Written for Every Evaluation (Priority: P2)

Every tool call evaluation — allow or deny — produces exactly one structured JSON log entry appended to the file indicated by the `HOOK_LOG` environment variable.

**Why this priority**: Auditability is a governance requirement; without a complete log, compliance review is impossible.

**Independent Test**: Execute the hook ten times with varied outcomes; assert the log file contains exactly ten newline-delimited JSON objects, each with all required fields.

**Acceptance Scenarios**:

1. **Given** any tool call (allow or deny), **When** the hook completes, **Then** one JSON line is appended to `HOOK_LOG` containing `call_id`, `run_id`, `timestamp`, `sim_time`, `user`, `agent`, `tool`, `arguments`, `decision`, `rule`, and `reason`.
2. **Given** a directory lookup was made, **When** the log entry is written, **Then** `sim_time` contains the clock value returned by the directory.
3. **Given** no directory lookup was needed (e.g., blank-user deny), **When** the log entry is written, **Then** `sim_time` is `null`.

---

### User Story 6 - Agent Config Loaded from YAML at Construction (Priority: P3)

The `PolicyHook` constructor reads `allowed_tools`, `fingerprints`, `acts_for`, and `expense_limit` from the agent's YAML file (`{agent_name}.yaml`) at construction time. No values are hard-coded in source. Changing the YAML file and restarting the agent changes policy without any code modification.

**Why this priority**: Runtime configurability is the primary mechanism by which operators control agent permissions. Hard-coded values are explicitly prohibited.

**Independent Test**: Create two YAML files with different `allowed_tools` lists; instantiate `PolicyHook` with each and assert the correct tools are allowed/denied for each instance.

**Acceptance Scenarios**:

1. **Given** a YAML file where `allowed_tools` contains only `["submit_expense"]`, **When** the hook is instantiated and evaluates `book_travel`, **Then** the call is denied with P7.
2. **Given** a YAML file updated to add `book_travel` to `allowed_tools` (and hook reinstantiated), **When** the hook evaluates `book_travel`, **Then** the call proceeds past the P7 check.

---

### Edge Cases

- What happens when the `HOOK_LOG` environment variable is not set?
  - The hook must still function; if logging fails, the failure should be observable but must not silently swallow the log.
- What happens when the agent YAML file does not exist or cannot be parsed?
  - The constructor must raise a clear error at startup rather than silently failing at call time.
- What happens when `event.selected_tool` is `None` during the fingerprint check?
  - The P6 fingerprint check must be skipped entirely; no denial is issued for a missing selected tool.
- What happens when a delegation exists but `active` is `false`?
  - The delegation must not count; the hook must deny with P4.
- What happens on HTTP 404 from the directory (e.g., unknown user, unknown expense)?
  - Treat as "not found" and apply the corresponding deny rule (P3, P4, or P7 as appropriate).
- What happens when `acts_for` is `"any"` in the agent YAML?
  - The role check is skipped; any user is permitted past that check.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST expose a `PolicyHook` class that accepts `agent_name`, `user`, `base_url`, `run_id`, `limit_override`, `config_dir`, and `bearer_token` at construction time.
- **FR-002**: The system MUST load `allowed_tools`, `fingerprints`, `acts_for`, and `expense_limit` exclusively from the agent's YAML configuration file — values MUST NOT be hard-coded.
- **FR-003**: The system MUST register `_before_tool_call` as a callback for `BeforeToolCallEvent` via `register_hooks`.
- **FR-004**: The system MUST evaluate each tool call in the fixed order: blank-user → P7 allowed_tools → acts_for role → P6 fingerprint → SCOPE token → P1–P5.
- **FR-005**: The system MUST canonicalise `claimant`, `traveller`, and `vendor` arguments (strip + lowercase) before any comparison, using `domain.identity.canonicalise`.
- **FR-006**: The system MUST fetch all policy-relevant facts (user records, delegations, vendor list, expenses) from live HTTP directory routes on each tool call; no facts may be cached across calls.
- **FR-007**: The system MUST let HTTP 5xx responses from the directory propagate without catching or suppressing them.
- **FR-008**: The system MUST treat HTTP 404 from the directory as "not found" and apply the corresponding deny rule.
- **FR-009**: The system MUST check the bearer token for presence, valid JWT structure, and required scope (per `TOOL_SCOPE_MAP` in `domain/scopes.py`) and deny with `SCOPE` on any failure.
- **FR-010**: The system MUST apply rules P1–P7 as defined in the constitution and deny tool calls that violate any applicable rule.
- **FR-011**: The system MUST append one structured JSON log entry to the `HOOK_LOG` path for every tool call evaluation, containing all required fields.
- **FR-012**: The system MUST record `sim_time` from the directory response when a lookup was made, and `null` otherwise.
- **FR-013**: The system MUST only import from the Python standard library, `httpx`, `PyJWT`, `PyYAML`, `strands`, and files provided in this kit.

### Key Entities

- **PolicyHook**: The central enforcement component. Holds agent config state loaded at construction; evaluates each `BeforeToolCallEvent` against all applicable rules.
- **Agent Config (YAML)**: Per-agent file containing `allowed_tools`, `fingerprints`, `acts_for`, and `expense_limit`. The single authoritative source for agent-level policy configuration.
- **Directory**: External HTTP service providing live facts: user records, expense records, delegations, and vendor lists. Routes defined in spec_input.md section 7.
- **Decision Log Entry**: A single-line JSON object written per evaluation capturing the outcome, the rule that fired, simulation time, and full call context.
- **Bearer Token**: JWT passed at construction. Validated for presence, decodability, and scope before business rules run.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Every tool call evaluation — allow or deny — produces exactly one log entry with all eleven required fields populated.
- **SC-002**: All seven deny paths (P1–P7) and the SCOPE deny path each have at least one automated test that passes in the test suite.
- **SC-003**: HTTP 5xx propagation and HTTP 404 handling each have at least one automated test that passes.
- **SC-004**: The allow path for each tool relevant to P1–P5 rules has at least one automated test that passes.
- **SC-005**: Changing only the agent YAML (without modifying source code) changes which tools are allowed or denied in the next agent instantiation.
- **SC-006**: No policy-relevant value (tool name, role, vendor, user identity, expense limit) is present as a literal in `checkpoint.py`.
- **SC-007**: The check order matches the specification exactly; no rule is evaluated before its predecessor in the fixed sequence.

## Assumptions

- The Strands agent framework's `BeforeToolCallEvent` provides `tool_name`, `tool_use`, `selected_tool`, and the arguments dict as attributes accessible during the callback.
- `domain/scopes.py` contains a `TOOL_SCOPE_MAP` dict mapping tool names to required JWT scope strings; this file exists in the kit and is not generated.
- `domain/identity.py` contains a `canonicalise` function that strips surrounding whitespace and lowercases a string; this file exists in the kit and is not generated.
- The directory HTTP service is reachable at `base_url` during every tool call; no offline or degraded-mode operation is required.
- The agent YAML files are stored in a `config/` directory relative to the module file unless `config_dir` is overridden at construction.
- `limit_override`, if provided, supersedes the `expense_limit` from the YAML for P2 evaluation.
- JWT tokens are decoded without signature verification (algorithm `"none"` or algorithm list from `domain/scopes.py`); the directory is the authoritative trust boundary.
- The `HOOK_LOG` environment variable points to a writable file path; if unset, behaviour is undefined (not in scope for this feature).
- `acts_for: "any"` means the role check is skipped for all users of that agent.
- The `fingerprints` map key is the tool name; the value is the expected SHA-256 hex digest of `name + description + inputSchema` serialised in a defined canonical form.
