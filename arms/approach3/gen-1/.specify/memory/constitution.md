<!-- SYNC IMPACT REPORT
Version change: [PLACEHOLDER] → 1.0.0
Added sections:
  - Core Principles: P1–P7 (7 principles, all new; template had 5 placeholder slots)
  - Implementation Constraints (new)
  - Audit & Decision Log (new)
  - Governance (new, governance rules filled in)
Removed sections: [SECTION_2_NAME] and [SECTION_3_NAME] placeholders replaced with
  named sections above
Modified principles: none (initial ratification, no prior principles existed)
TODOs: none — all placeholders resolved
-->

# Checkpoint Constitution

## Core Principles

### P1. Own Expenses Only

A user MUST submit expenses only as the claimant. An agent acting for one user
MUST NOT submit an expense naming a different user as claimant. The `claimant`
argument MUST be canonicalised (whitespace-stripped, lowercased) before
comparison.

**Rationale**: Prevents agents from filing fraudulent expenses on behalf of
users they are not acting for.

### P2. Single Expense Limit

An expense above the configured limit MUST be accompanied by an `approval_ref`
from the claimant's manager. If no `approval_ref` is present, the call MUST be
denied immediately — not queued, not deferred.

**Rationale**: High-value expenses require explicit managerial sign-off; silent
queuing would obscure the policy breach.

### P3. No Self-Approval

A user MUST NOT approve their own expense. The approver MUST be the claimant's
manager (verified via the directory at call time) and MUST NOT be the claimant.
This check MUST hold regardless of any agent instruction to the contrary.

**Rationale**: Segregation of duties; a claimant who can approve their own
expense is an unacceptable conflict of interest.

### P4. Live Travel Delegation

An agent booking travel for a third-party traveller MUST verify at call time
that an active delegation exists covering that traveller-to-user relationship.
The checkpoint MUST check the `active` field of each delegation returned by the
directory; it MUST NOT compare timestamps itself. Delegations not marked active
MUST be treated as invalid even if their expiry date has not passed.

**Rationale**: Delegations may be revoked mid-session; point-in-time checks at
session start are insufficient.

### P5. Approved Vendor List

Vendor payments MUST only be made to vendors present in the directory's
approved vendor list, fetched live at call time. Agents MUST NOT add to or
modify the vendor list. The `vendor` argument MUST be canonicalised before
comparison.

**Rationale**: Finance controls the vendor list; agent-side additions would
bypass procurement policy.

### P6. Reviewed Tool Definition

An agent MUST NOT call a tool whose definition (name, description, input
schema) does not match the SHA-256 fingerprint recorded at review time. If
`event.selected_tool` is `None` or no fingerprint is stored for the tool, this
check MUST be skipped. A changed tool definition MUST be treated as a different,
unreviewed tool.

**Rationale**: Prompt injection or server-side changes could alter tool
behaviour after review; fingerprint verification detects tampering.

### P7. Default Deny

Any tool not present in the agent's `allowed_tools` list MUST be denied. Any
action with no explicit grant from P1–P6 is refused. This includes blank-user
calls and role mismatches. Tools added after review are denied until
`allowed_tools` and fingerprints are updated.

**Rationale**: Explicit allow-listing is safer than implicit permission; unknown
tools MUST fail closed.

## Implementation Constraints

- The checkpoint MUST be implemented as a single file: `checkpoint.py`.
- The `PolicyHook` class MUST implement `HookProvider` from the Strands
  framework and register `_before_tool_call` as a `BeforeToolCallEvent`
  callback.
- No server-side, MCP, or network-listener code is permitted in `checkpoint.py`.
- All live facts (users, delegations, vendors, expenses) MUST be fetched via
  HTTP from the directory base URL at call time; none may be hard-coded.
- Allowed imports: Python standard library, `httpx`, `PyJWT`, `PyYAML`,
  `strands`. No other third-party dependencies.
- Agent configuration is loaded from a YAML file named `{agent_name}.yaml`
  at construction time; fields are never hard-coded.
- On HTTP 5xx responses, errors MUST propagate; they MUST NOT be caught or
  swallowed so the framework can observe the failure.
- On HTTP 404, treat as "not found" and apply the relevant deny rule.
- Check order MUST follow the sequence defined in the specification: blank-user
  guard → P7 allowed_tools → acts_for role → P6 fingerprint → SCOPE bearer
  token → P1–P5 business rules.

## Audit & Decision Log

Every tool-call evaluation MUST produce one structured log entry appended to
the path held in the `HOOK_LOG` environment variable before the tool runs.
The log entry MUST be a single-line JSON object containing: `call_id`,
`run_id`, `timestamp` (ISO-8601 UTC), `sim_time`, `user`, `agent`, `tool`,
`arguments`, `decision` (`allow`/`deny`/`error`), `rule` (`P1`–`P7`, `SCOPE`,
or `null` for allow), and `reason`.

An implementation that enforces a rule but omits the corresponding log entry
is scored as enforcing without audit and is non-compliant with this
constitution.

## Governance

This constitution supersedes all other practices and informal agreements for
this project. Amendments MUST be documented, version-bumped, and reflected in
this file before implementation begins.

- All code reviews MUST verify that the implementation conforms to each
  applicable principle (P1–P7) and the implementation constraints above.
- Complexity introduced for purposes outside P1–P7 enforcement MUST be
  justified against the project's single-file, no-MCP constraint.
- Version bumps follow semantic versioning: MAJOR for principle removals or
  redefinitions; MINOR for new principles or material expansions; PATCH for
  clarifications and wording fixes.
- The approved vendor list, delegation records, and user directory are
  authoritative sources owned outside this codebase; the checkpoint MUST
  treat them as read-only.

**Version**: 1.0.0 | **Ratified**: 2026-10-05 | **Last Amended**: 2026-10-05
