# Feature Specification: PolicyHook Checkpoint Enforcement

**Feature Branch**: `001-policy-hook-checkpoint`

**Created**: 2026-10-05

**Status**: Draft

**Input**: User description: "Read spec_input.md and implement the PolicyHook class in checkpoint.py. The class must read allowed_tools and fingerprints from the agent config file at run time (never hard-coded), check the bearer token scope, apply rules P1-P7, log each decision, and let directory 5xx errors propagate."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Tool Call Allowed Under All Rules (Priority: P1)

An agent acting for a legitimate user calls an allowed tool with a valid bearer token scope, correct fingerprint, and arguments that satisfy all business rules. The checkpoint evaluates each rule in order and, finding no violations, permits the call and writes a structured log entry recording the allow decision.

**Why this priority**: This is the golden path — every correctly-configured agent call must pass through. If this scenario fails, the checkpoint blocks all legitimate use.

**Independent Test**: Can be fully tested by constructing a PolicyHook with a valid config, presenting a valid bearer token, and calling `_before_tool_call` with a matching fingerprint and compliant arguments; the call must proceed with a log entry recording `"decision": "allow"`.

**Acceptance Scenarios**:

1. **Given** the agent config lists a tool as allowed and its fingerprint matches, **When** `_before_tool_call` is invoked with a valid scoped bearer token and rule-compliant arguments, **Then** the tool call is not cancelled and a log entry with `"decision": "allow"` and `"rule": null` is appended to `HOOK_LOG`.
2. **Given** the bearer token has the required scope for the tool, **When** the token is decoded, **Then** the scope claim covers the tool's required scope from `TOOL_SCOPE_MAP` and the call proceeds.

---

### User Story 2 - Denied Calls Are Blocked and Logged (Priority: P1)

When any policy rule is violated — wrong claimant, missing approval, self-approval, no active delegation, unapproved vendor, tampered tool fingerprint, missing allowed_tools entry, blank user, or insufficient token scope — the checkpoint cancels the tool call and writes a structured log entry naming the rule that fired.

**Why this priority**: Enforcement without audit is explicitly called out as non-compliant in the project constitution. Every deny must be observable.

**Independent Test**: Can be fully tested by constructing a PolicyHook with a minimal config and presenting an event that triggers each rule individually; each must produce exactly one log entry with `"decision": "deny"` and the correct `"rule"` field, and `cancel_tool` must be set.

**Acceptance Scenarios**:

1. **Given** a tool is not in `allowed_tools`, **When** the tool is called, **Then** the call is cancelled with rule `P7` and the log entry records `"decision": "deny"` and `"rule": "P7"`.
2. **Given** `submit_expense` is called with a claimant that differs from the acting user, **When** the checkpoint evaluates P1, **Then** the call is cancelled and the log entry records `"rule": "P1"`.
3. **Given** `submit_expense` amount exceeds the configured limit and no `approval_ref` is supplied, **When** P2 is evaluated, **Then** the call is cancelled and the log entry records `"rule": "P2"`.
4. **Given** `approve_expense` is called by the same user who is the expense's claimant, **When** P3 is evaluated via a live directory call, **Then** the call is cancelled and the log entry records `"rule": "P3"`.
5. **Given** `book_travel` is called for a third-party traveller with no active delegation, **When** P4 is evaluated, **Then** the call is cancelled and the log entry records `"rule": "P4"`.
6. **Given** `pay_vendor` is called with a vendor not on the directory's live approved list, **When** P5 is evaluated, **Then** the call is cancelled and the log entry records `"rule": "P5"`.
7. **Given** the tool's current definition hash does not match the stored fingerprint, **When** P6 is evaluated, **Then** the call is cancelled and the log entry records `"rule": "P6"`.
8. **Given** the bearer token is missing or not a valid JWT, **When** the SCOPE check is evaluated, **Then** the call is cancelled and the log entry records `"rule": "SCOPE"`.

---

### User Story 3 - Runtime Config Load (No Hard-Coded Facts) (Priority: P2)

An operator updates an agent's config file — changing `allowed_tools`, `fingerprints`, or `expense_limit` — and the updated values are in effect from the next construction of a PolicyHook instance without any code change.

**Why this priority**: The constitution explicitly prohibits hard-coded facts. This is the mechanism that enforces that constraint.

**Independent Test**: Can be fully tested by writing two different YAML config files to `config_dir`, constructing a PolicyHook for each, and verifying that the tools and limits read at instantiation time reflect each file's contents independently.

**Acceptance Scenarios**:

1. **Given** a YAML config with `allowed_tools: [submit_expense]` is written to `config_dir`, **When** PolicyHook is constructed, **Then** `submit_expense` is permitted and any other tool is denied without modifying any code.
2. **Given** `expense_limit: 200` is written to the config, **When** `submit_expense` is called with `amount: 201` and no `approval_ref`, **Then** it is denied with P2.

---

### User Story 4 - Directory 5xx Errors Propagate (Priority: P2)

When any directory HTTP call returns a 5xx status code, the checkpoint does not swallow or convert the error; it propagates to the caller so the framework can observe the failure.

**Why this priority**: Silent error swallowing would cause policy checks to pass vacuously on a failed directory call, which would be a security flaw. The constitution mandates propagation.

**Independent Test**: Can be fully tested by pointing the checkpoint at a mock HTTP server that returns 500 for a given route, calling a tool that would trigger that directory lookup, and confirming that an exception propagates rather than a deny-or-allow decision being logged.

**Acceptance Scenarios**:

1. **Given** the directory returns HTTP 500 for `GET /directory/users/{user}`, **When** `acts_for` is a role name and the checkpoint attempts the user lookup, **Then** the HTTP error propagates and no allow decision is recorded.
2. **Given** the directory returns HTTP 500 for `GET /directory/expenses/{id}`, **When** P3 is evaluated for `approve_expense`, **Then** the error propagates rather than silently denying.

---

### User Story 5 - Acts-For Role Check (Priority: P3)

An agent configured to act only for a specific role denies tool calls from users whose directory role does not match, and permits calls from users whose role does match, fetching the user record live at call time.

**Why this priority**: The role-based access control is the mechanism that restricts specialist agents (e.g., `payments-agent` restricted to the `finance` role) to the correct user population.

**Independent Test**: Can be fully tested by configuring the agent with `acts_for: finance` and calling with both a finance-role user and a non-finance user, verifying that only the finance user proceeds.

**Acceptance Scenarios**:

1. **Given** `acts_for: finance` in the config and the directory reports the user's role as `finance`, **When** the checkpoint evaluates the acts_for check, **Then** the call proceeds past this check.
2. **Given** `acts_for: finance` and the directory reports the user's role as `employee`, **When** the acts_for check is evaluated, **Then** the call is denied with P7 and logged.

---

### Edge Cases

- What happens when `event.selected_tool` is `None`? The P6 fingerprint check must be skipped entirely.
- What happens when a tool has no fingerprint entry in the config? The P6 check must be skipped for that tool.
- What happens when the user string is empty or whitespace-only after canonicalisation? The blank-user guard must fire with P7 before any other check.
- What happens when the directory returns 404 for a user lookup? The lookup is treated as "not found" and the deny rule for the relevant check applies.
- What happens when `HOOK_LOG` env var is not set? The implementation must still succeed without crashing (log to a no-op or skip gracefully).
- What happens when `acts_for` is `"any"`? The acts_for role check must be skipped entirely without a directory call.
- What happens when `approve_expense` is called and the approver is not the claimant's manager? P3 must deny the call.
- What happens when the bearer token has valid structure but lacks the required scope? SCOPE denial must fire after fingerprint check but before P1–P5.
- What happens when `book_travel` is called by the traveller themselves (no delegation needed)? P4 must be skipped.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST expose a `PolicyHook` class that implements `HookProvider` and registers `_before_tool_call` as a callback for `BeforeToolCallEvent`.
- **FR-002**: The constructor MUST accept `agent_name`, `user`, `base_url`, and optional `run_id`, `limit_override`, `config_dir`, and `bearer_token` parameters as specified.
- **FR-003**: The constructor MUST load the agent's YAML configuration file from `config_dir/{agent_name}.yaml` at construction time; if `config_dir` is not provided, it MUST default to a path relative to the module file.
- **FR-004**: The system MUST read `allowed_tools`, `fingerprints`, `acts_for`, and `expense_limit` from the config file at run time; none of these values may be hard-coded.
- **FR-005**: The check order MUST be: blank-user guard → P7 allowed_tools → acts_for role → P6 fingerprint → SCOPE bearer token → P1–P5 business rules, evaluated in that fixed sequence for every tool call.
- **FR-006**: The system MUST deny calls from users with an empty or whitespace-only username (after canonicalisation) with rule P7.
- **FR-007**: The system MUST deny calls for tools not present in the agent's `allowed_tools` list with rule P7.
- **FR-008**: When `acts_for` is a role name (not `"any"`), the system MUST fetch `GET /directory/users/{user}` and deny with P7 if the user's role does not match.
- **FR-009**: The system MUST skip the P6 fingerprint check if `event.selected_tool` is `None` or if no fingerprint is stored for the tool in the config.
- **FR-010**: The system MUST compute a SHA-256 hash over the tool's `name`, `description`, and `inputSchema` to verify fingerprints, and deny with P6 if the hash does not match.
- **FR-011**: The system MUST deny with rule SCOPE if the bearer token is missing, cannot be decoded as a valid JWT, or does not carry the scope required for the tool per `TOOL_SCOPE_MAP`.
- **FR-012**: For `submit_expense`, the system MUST deny with P1 if the canonicalised `claimant` argument does not equal the acting user.
- **FR-013**: For `submit_expense`, the system MUST deny with P2 if the `amount` exceeds the configured limit and no `approval_ref` is provided.
- **FR-014**: For `approve_expense`, the system MUST fetch `GET /directory/expenses/{expense_id}` and `GET /directory/users/{claimant}`, then deny with P3 if the acting user is the claimant or is not the claimant's manager.
- **FR-015**: For `book_travel`, if the canonicalised `traveller` argument differs from the acting user, the system MUST fetch `GET /directory/delegations` and deny with P4 if no delegation is both marked `active` and covers travel from the traveller to the user. The system MUST NOT compare timestamps itself.
- **FR-016**: For `pay_vendor`, the system MUST fetch `GET /directory/vendors` and deny with P5 if the canonicalised `vendor` argument is not in the returned list.
- **FR-017**: The system MUST let HTTP 5xx responses propagate without catching or swallowing them.
- **FR-018**: The system MUST treat HTTP 404 responses as "not found" and apply the deny rule for the relevant check.
- **FR-019**: Every tool-call evaluation MUST append exactly one JSON log entry to the path held in `HOOK_LOG`, containing: `call_id` (UUID), `run_id`, `timestamp` (ISO-8601 UTC), `sim_time`, `user`, `agent`, `tool`, `arguments`, `decision`, `rule`, and `reason`.
- **FR-020**: The `decision` field MUST be `"allow"`, `"deny"`, or `"error"`; the `rule` field MUST be the rule id (`P1`–`P7`, `SCOPE`) or `null` for allowed calls.
- **FR-021**: All comparisons of `claimant`, `traveller`, and `vendor` arguments MUST be made against values canonicalised via `domain.identity.canonicalise` (strip whitespace, lowercase).
- **FR-022**: The module MUST only import from the Python standard library, `httpx`, `PyJWT`, `PyYAML`, `strands`, and files provided in the kit.

### Key Entities

- **PolicyHook**: The single class exported by `checkpoint.py`; registers pre-call enforcement logic with the Strands agent framework and carries per-instance configuration.
- **Agent Config**: A YAML file per agent containing `allowed_tools`, `fingerprints`, `acts_for`, and optionally `expense_limit`; the authoritative source for what each agent is permitted to do.
- **Decision Log Entry**: A single-line JSON object written to `HOOK_LOG` before every tool call resolves; the primary audit artifact.
- **Directory**: The HTTP service at `base_url` providing live facts (user records, delegations, vendor list, expenses) that the checkpoint fetches at call time.
- **Bearer Token**: A JWT presented by the caller carrying an OAuth scope claim; the checkpoint verifies its validity and scope coverage before allowing any P1–P5 check.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Every tool call — permitted or denied — produces exactly one structured log entry before the tool runs; zero calls produce zero or multiple entries.
- **SC-002**: All seven policy rules (P1–P7) plus the SCOPE check correctly classify 100% of the provided contract test cases as allow or deny with the expected rule identifier.
- **SC-003**: The blank-user guard fires before any directory call is made, so no directory request is issued for an empty-user call.
- **SC-004**: An agent config change (without code change) takes effect in the next PolicyHook instance within the same process, verified by constructing two instances with different configs and observing different enforcement outcomes.
- **SC-005**: HTTP 5xx errors from any directory route are observable at the caller; zero 5xx responses are silently absorbed as allow or deny decisions.
- **SC-006**: The `checkpoint.py` module passes all provided contract tests (`tests/test_contract.py`) and any generator-written tests in `tests/generated/` without modification to the test files.

## Assumptions

- The Strands framework's `HookProvider` base class and `BeforeToolCallEvent` are available from the installed `strands` package at `.venv`; no alternative hook interface is used.
- `config_dir` defaults to a `config/agents/` directory relative to the module file when not explicitly provided.
- The `HOOK_LOG` environment variable points to a writable file path; if unset, the module handles the absence gracefully without crashing (no-op log).
- All directory routes are synchronous HTTP endpoints accessible over plain HTTP; no authentication is required from the checkpoint to call the directory.
- The `TOOL_SCOPE_MAP` in `domain/scopes.py` is the single authoritative mapping from tool name to required OAuth scope; the checkpoint imports it directly.
- JWT verification uses the `PyJWT` library; the token's signature, expiry, and scope claim are verified but key management and issuance are out of scope for `checkpoint.py`.
- The `sim_time` field in the log entry is populated from the `sim_time` field of any directory response that was fetched during the evaluation; it is `null` when no directory call was made.
- `limit_override`, if provided, replaces the `expense_limit` from the config for that PolicyHook instance.
- Tool fingerprint computation covers `name`, `description`, and `inputSchema` fields of `event.selected_tool` serialised deterministically before hashing.
