# Feature Specification: PolicyHook Checkpoint Enforcement

**Feature Branch**: `001-policy-hook-checkpoint`

**Created**: 2026-10-05

**Status**: Draft

**Input**: User description: "Read spec_input.md and implement the PolicyHook class in checkpoint.py. The class must read allowed_tools and fingerprints from the agent config file at run time (never hard-coded), check the bearer token scope, apply rules P1-P7, log each decision, and let directory 5xx errors propagate."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Policy Enforcement Before Every Tool Call (Priority: P1)

An agent operator deploys a PolicyHook alongside an AI agent. Before every tool call the agent attempts, the hook evaluates the full set of policy rules in a fixed order. Calls that fail any rule are blocked before the tool executes. Calls that pass all rules proceed.

**Why this priority**: This is the core purpose of the module — without pre-call enforcement, no other story delivers value.

**Independent Test**: Configure a PolicyHook with a minimal agent config listing no allowed tools. Send a tool call. Verify the call is blocked with rule P7.

**Acceptance Scenarios**:

1. **Given** an agent config with `allowed_tools: []`, **When** any tool call is attempted, **Then** the call is denied with rule P7.
2. **Given** the acting user is an empty string or blank, **When** any tool call is attempted, **Then** the call is denied with rule P7 before any other rule is evaluated.
3. **Given** all policy rules pass for a tool call, **When** the tool call is evaluated, **Then** the call is allowed to proceed.
4. **Given** the agent config restricts the agent to a specific role and the acting user's role does not match, **When** any tool call is attempted, **Then** the call is denied with rule P7.
5. **Given** the rules are evaluated, **Then** the order is always: blank-user guard → allowed-tools check → role check → fingerprint check → scope check → business rules (P1–P5).

---

### User Story 2 - Complete Audit Trail for Every Decision (Priority: P2)

A security auditor needs to review every tool call decision the checkpoint made. The hook appends one log entry per tool call evaluation — before the tool runs — containing all information needed to reconstruct the decision without access to the original request.

**Why this priority**: Compliance and accountability require that every enforcement decision is recorded, regardless of whether it allows or blocks. An enforcement action without an audit record is non-compliant.

**Independent Test**: Trigger any tool call via a configured PolicyHook. Confirm the designated log file gains exactly one new line-delimited JSON entry containing all required fields.

**Acceptance Scenarios**:

1. **Given** a tool call is evaluated and allowed, **When** the evaluation completes, **Then** a log entry with `decision: "allow"` and `rule: null` is appended.
2. **Given** a tool call is evaluated and denied, **When** the evaluation completes, **Then** a log entry with `decision: "deny"` and the triggering rule identifier (e.g., `"P1"`, `"P7"`, `"SCOPE"`) is appended.
3. **Given** a tool call evaluation encounters an error, **When** the error occurs, **Then** a log entry with `decision: "error"` is appended before the error propagates.
4. **Given** any log entry is written, **Then** it contains all required fields: `call_id`, `run_id`, `timestamp`, `sim_time`, `user`, `agent`, `tool`, `arguments`, `decision`, `rule`, `reason`.
5. **Given** a decision required no directory lookup, **Then** the `sim_time` field is `null`.
6. **Given** a decision required a directory lookup, **Then** the `sim_time` field reflects the simulation clock time returned in that directory response.

---

### User Story 3 - Runtime Configuration Without Code Changes (Priority: P3)

An agent operator updates the list of authorized tools for an agent or records a new fingerprint for a reviewed tool. These changes take effect by updating the agent configuration file — no modification to the checkpoint module is needed.

**Why this priority**: Hard-coded policy facts are a security vulnerability and operational burden. The system must remain adaptable to policy changes through configuration alone.

**Independent Test**: Initialize PolicyHook with a config file listing one allowed tool. Initialize a second instance with a config file listing a different tool. Verify each instance enforces only its own config.

**Acceptance Scenarios**:

1. **Given** the agent config lists specific allowed tools, **When** PolicyHook is initialized, **Then** only those tools may proceed past the allowed-tools check.
2. **Given** the agent config contains a fingerprint for a tool, **When** that tool's definition has changed since the fingerprint was recorded, **Then** the call is denied with rule P6.
3. **Given** the agent config contains an `expense_limit`, **When** a `submit_expense` call exceeds that limit without an approval reference, **Then** the call is denied with rule P2.
4. **Given** the agent config contains no fingerprint for a tool, **When** that tool is called, **Then** the fingerprint check is skipped for that tool.
5. **Given** the tool's definition object is unavailable (`None`) at call time, **When** the fingerprint check is reached, **Then** the check is skipped entirely.

---

### User Story 4 - Business Rule Enforcement for Financial and Travel Tools (Priority: P4)

The checkpoint enforces five business rules (P1–P5) that prevent fraud and unauthorized financial or travel actions. Each rule applies to a specific tool under a specific condition, using live directory data to verify the facts required for the decision.

**Why this priority**: These rules encode the financial and travel compliance policies that protect the organization. Bypassing them could enable fraudulent expense submissions, self-approvals, unauthorized travel bookings, or payments to unapproved vendors.

**Independent Test**: For each of P1–P5, construct a minimal tool call that violates only that rule. Verify the call is denied with the correct rule identifier and the relevant directory endpoint (if any) was consulted.

**Acceptance Scenarios**:

1. **Given** a `submit_expense` call where the `claimant` (normalized) differs from the acting user, **Then** the call is denied with P1.
2. **Given** a `submit_expense` call where the amount exceeds the configured limit and no `approval_ref` is provided, **Then** the call is denied with P2.
3. **Given** an `approve_expense` call where the acting user is the expense claimant, **Then** the call is denied with P3.
4. **Given** an `approve_expense` call where the acting user is not the claimant's manager in the directory, **Then** the call is denied with P3.
5. **Given** a `book_travel` call where the `traveller` (normalized) differs from the acting user and no active delegation from the traveller to the acting user exists in the directory, **Then** the call is denied with P4.
6. **Given** a `book_travel` call where a delegation exists but its `active` field is `false`, **Then** the delegation is not considered active and the call is denied with P4.
7. **Given** a `pay_vendor` call where the `vendor` (normalized) is not in the directory's current approved vendor list, **Then** the call is denied with P5.
8. **Given** any of the above calls pass all business rules, **Then** the call is allowed (assuming prior checks also passed).

---

### User Story 5 - Transparent Directory Service Error Handling (Priority: P5)

When the directory service returns a server error (5xx), the checkpoint does not silently swallow the error or substitute a fallback allow/deny decision. Instead, the error propagates to the calling framework so the operator is alerted to the service outage.

**Why this priority**: Silent error handling masks directory outages. A swallowed 5xx could either allow unauthorized calls (a security violation) or block legitimate ones (an availability issue) — both with no visibility to the operator.

**Independent Test**: Configure the directory base URL to return 500 responses. Trigger a tool call that requires a directory lookup. Confirm the HTTP error propagates rather than producing a clean allow or deny decision.

**Acceptance Scenarios**:

1. **Given** the directory service returns a 5xx response during a user lookup, **When** the checkpoint processes the call, **Then** the HTTP error propagates to the calling framework without being caught by the checkpoint.
2. **Given** the directory service returns a 5xx response during any other lookup (expenses, delegations, vendors), **When** the checkpoint processes the call, **Then** the HTTP error propagates without being caught.
3. **Given** the directory service returns a 404 for a requested resource (user, expense, etc.), **When** the checkpoint processes the call, **Then** the call is denied with the applicable rule for that check (P3, P4, P5, or P7 as appropriate).

---

### Edge Cases

- What happens when the acting user string is blank or contains only whitespace after normalization?
- How does the checkpoint behave when `event.selected_tool` is `None` during the fingerprint check?
- How does the checkpoint behave when the bearer token is a non-empty string but cannot be decoded as a valid credential?
- How does the checkpoint behave when the bearer token is valid but does not carry the scope required for the tool?
- How does the checkpoint handle tool names that are not present in `TOOL_SCOPE_MAP` (i.e., no scope requirement defined for the tool)?
- How does the checkpoint handle `claimant`, `traveller`, or `vendor` values with mixed case or surrounding whitespace?
- What happens when a delegation record exists in the directory response but its `active` field is `false`?
- What happens when the `expense_limit` is not set in the agent config?
- What happens when the directory returns an empty vendor list for `pay_vendor`?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The enforcement module MUST evaluate all policy rules in a fixed, non-configurable order before any tool call executes: (1) blank-user guard, (2) allowed-tools check, (3) role check, (4) fingerprint check, (5) scope check, (6) business rules P1–P5.
- **FR-002**: The enforcement module MUST read the authorized tool list, fingerprints, role restriction, and expense limit from the agent configuration file at initialization time; none of these values may be hard-coded.
- **FR-003**: The enforcement module MUST deny any tool call from a user whose identity is blank or reduces to an empty string after normalization, using rule P7.
- **FR-004**: The enforcement module MUST deny any call to a tool not present in the agent's configured authorized tool list, using rule P7.
- **FR-005**: When the agent configuration specifies a role restriction (not `"any"`), the enforcement module MUST fetch the acting user's current role from the directory and deny with P7 if the role does not match.
- **FR-006**: The enforcement module MUST compare each tool's current definition against its recorded fingerprint; a mismatch MUST deny the call with P6. This check is skipped when no fingerprint exists for the tool or when the tool's definition object is unavailable.
- **FR-007**: The enforcement module MUST verify the bearer token is present, decodable as a valid credential, and carries the required authorization scope for the tool being called; any failure MUST deny the call with SCOPE.
- **FR-008**: For `submit_expense`, the enforcement module MUST deny the call when the normalized `claimant` argument does not match the normalized acting user, using rule P1.
- **FR-009**: For `submit_expense`, the enforcement module MUST deny the call when the expense amount exceeds the configured limit and no `approval_ref` is provided, using rule P2.
- **FR-010**: For `approve_expense`, the enforcement module MUST deny the call when the acting user is the expense's claimant, or when the acting user is not the claimant's manager according to the directory, using rule P3.
- **FR-011**: For `book_travel`, when the normalized `traveller` differs from the acting user, the enforcement module MUST deny the call unless an active delegation from the traveller to the acting user is present in the directory's current delegation list, using rule P4.
- **FR-012**: For `pay_vendor`, the enforcement module MUST deny the call when the normalized `vendor` argument is not in the directory's current approved vendor list, using rule P5.
- **FR-013**: The enforcement module MUST normalize `claimant`, `traveller`, and `vendor` arguments by stripping surrounding whitespace and converting to lowercase before any comparison.
- **FR-014**: The enforcement module MUST let HTTP 5xx responses from the directory propagate without catching or swallowing them.
- **FR-015**: The enforcement module MUST append exactly one log entry per tool call evaluation to the path specified by the `HOOK_LOG` environment variable, written before the tool executes, containing: `call_id`, `run_id`, `timestamp`, `sim_time`, `user`, `agent`, `tool`, `arguments`, `decision`, `rule`, `reason`.
- **FR-016**: All policy facts about users, delegations, vendors, and expenses MUST be fetched live from the directory at call time; no fact may be cached or reused across calls.
- **FR-017**: The enforcement module MUST register its pre-call evaluation callback with the framework's pre-tool-call event system.

### Key Entities

- **PolicyHook**: The enforcement component that evaluates tool calls against configured rules before execution. Initialized per agent session with agent name, acting user, directory base URL, optional run identifier, optional expense limit override, optional config directory, and optional bearer token.
- **Agent Configuration**: A per-agent file specifying the authorized tools, tool fingerprints, role restriction, and optional expense limit. Loaded at initialization time from a directory keyed by agent name.
- **Tool Call Event**: A framework-provided object representing a pending tool invocation, carrying the tool name, raw input arguments, and optionally the tool's definition object.
- **Decision Log Entry**: An immutable, line-delimited JSON audit record written for every evaluation containing all fields required by the log schema.
- **Directory**: The live, authoritative HTTP service providing user records (role, manager), delegation lists, approved vendor lists, and expense records. All lookups occur at call time; results are not cached.
- **Bearer Token**: A credential provided at construction time carrying authorization scope claims for tool categories.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Every tool call evaluation — allow, deny, or error — produces exactly one log entry; no evaluation is silent and no evaluation produces more than one entry.
- **SC-002**: Zero policy facts (authorized tools, fingerprints, user roles, delegations, approved vendors, expense limits) are embedded in the module's source code; all are sourced from the agent configuration file or the live directory.
- **SC-003**: Each of the seven policy principles (P1–P7) and the SCOPE check can be independently exercised through purpose-built test scenarios without modifying the module.
- **SC-004**: A directory 5xx error during any rule check surfaces to the calling framework as an observable error, not as a silent allow or deny.
- **SC-005**: Enforcement decisions for all rules are consistent and deterministic: the same input state always produces the same decision and log entry.
- **SC-006**: Tool call evaluations always follow the same six-step rule order regardless of tool name, acting user, or call history.

## Assumptions

- The framework's pre-tool-call event provides the tool name, raw input arguments as a dictionary, and optionally the tool's full definition object (name, description, input schema).
- The `HOOK_LOG` environment variable is set before the first tool call is evaluated; undefined behavior when the variable is absent is out of scope.
- Network-level failures beyond HTTP 5xx responses (e.g., DNS failure, connection refused) follow the same propagation behavior as 5xx responses.
- A role restriction in the agent config applies uniformly to all tools the agent is authorized to call; there is no per-tool role override.
- The bearer token is a single token for the agent session; per-call token rotation is not in scope.
- The `active` field of a delegation record is the authoritative indicator of validity; the checkpoint does not evaluate or compare expiry timestamps.
- When `expense_limit` is absent from the agent config, the P2 limit check is treated as a no-op (no limit applies).
- The directory pre-filters expired delegations before returning the list; the checkpoint only needs to check the `active` field of returned records.
