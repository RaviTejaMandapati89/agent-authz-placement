<!--
SYNC IMPACT REPORT (remove before committing)
=============================================
Version change: [blank template] → 1.0.0
Modified principles: N/A (initial population from blank template)
Added sections:
  - Core Principles (P1–P7)
  - Technical Constraints
  - Development & Testing Standards
  - Governance
Removed sections: none (template was unpopulated)
Follow-up TODOs:
  - RATIFICATION_DATE set to today (2026-10-05); confirm if an earlier design date should be used.
-->

# Checkpoint Constitution

## Core Principles

### P1. Own Expenses Only

For `submit_expense`, the `claimant` argument MUST equal the acting user after
canonicalisation. A user MUST NOT submit an expense on behalf of another user.

**Rationale**: Prevents impersonation in expense submission and ensures clear
auditability of financial claims.

### P2. Single Expense Limit

For `submit_expense`, if the submitted amount exceeds the configured
`expense_limit` and no `approval_ref` is supplied, the call MUST be denied.

**Rationale**: Enforces spending controls; large expenses require a pre-obtained
approval reference to guarantee an out-of-band authorisation trail.

### P3. No Self-Approval

For `approve_expense`, a user MUST NOT approve an expense they submitted
(claimant check) and MUST be the claimant's direct manager per the live
directory. Any other approver relationship MUST be denied.

**Rationale**: Segregation-of-duties control; eliminates the self-approval
conflict-of-interest risk.

### P4. Live Travel Delegation

For `book_travel`, if the `traveller` argument differs from the acting user after
canonicalisation, the checkpoint MUST fetch live delegations and confirm at least
one delegation with `active: true` covers the traveller→user pair. The checkpoint
MUST rely on the directory's `active` field and MUST NOT evaluate timestamps
itself.

**Rationale**: Ensures travel bookings on behalf of another person are explicitly
authorised, using the directory as the single source of truth for delegation
state.

### P5. Approved Vendor List

For `pay_vendor`, the `vendor` argument (after canonicalisation) MUST appear in
the live vendor list returned by the directory. Payments to unlisted vendors MUST
be denied.

**Rationale**: Restricts payments to pre-vetted suppliers; prevents rogue or
typo-driven vendor submissions.

### P6. Reviewed Tool Definition

Before any tool call proceeds, the checkpoint MUST compute a SHA-256 hash over
the tool's `name`, `description`, and `inputSchema` and compare it against the
fingerprint stored in the agent's YAML configuration. A mismatch MUST be denied.
This check MUST be skipped when `event.selected_tool` is `None` or when no
fingerprint is stored for that tool.

**Rationale**: Guards against silent tool-definition drift; ensures every tool
the agent can call has been explicitly reviewed and approved at its current
definition.

### P7. Default Deny

Any tool not present in the agent's `allowed_tools` list MUST be denied. This
rule also applies to the blank-user guard (empty user string after
canonicalisation) and to role mismatches when `acts_for` is a named role.
Absence of an explicit allow is a deny.

**Rationale**: Fail-closed posture; unknown or unvetted tools are never
accidentally permitted.

## Technical Constraints

- All policy-relevant facts (users, roles, managers, vendors, delegations,
  expenses) MUST be fetched at run time from the directory HTTP routes. Hard-coded
  facts that would require a code change to update are PROHIBITED.
- Agent configuration (allowed tools, fingerprints, `acts_for`, expense limit)
  MUST be loaded from a per-agent YAML file (`{agent_name}.yaml`) at construction
  time. Values MUST NOT be inlined in source code.
- On any HTTP 5xx response from the directory, the checkpoint MUST let the error
  propagate; swallowing or suppressing infrastructure errors is PROHIBITED.
- On HTTP 404 from the directory, the checkpoint MUST treat the result as "not
  found" and apply the corresponding deny rule.
- Argument canonicalisation (strip + lowercase) via `domain.identity.canonicalise`
  MUST be applied to `claimant`, `traveller`, and `vendor` before any comparison.
- The checkpoint MUST log every tool-call evaluation to the path in the `HOOK_LOG`
  environment variable as a single-line JSON object with the fields defined in the
  specification (call_id, run_id, timestamp, sim_time, user, agent, tool,
  arguments, decision, rule, reason).
- Allowed imports are limited to: Python standard library, `httpx`, `PyJWT`,
  `PyYAML`, `strands`, and files provided in this kit.

## Development & Testing Standards

- The check order defined in the specification (blank-user → P7 allowed_tools →
  acts_for → P6 fingerprint → SCOPE bearer token → P1–P5) MUST be followed
  exactly; reordering requires a constitution amendment.
- `checkpoint.py` is the single generated source file. Tests MUST be written to
  `tests/generated/` and MUST NOT be imported by the pipeline.
- Tests MUST cover every deny path (P1–P7, SCOPE) and the allow path for each
  relevant tool. HTTP 5xx propagation and 404 handling MUST each have at least
  one test.
- Run tests with: `uv run pytest`

## Governance

This constitution supersedes all other development practices for this project.
Amendments MUST be made via the `/speckit-constitution` command with an explicit
rationale and MUST increment the version number according to semantic versioning:

- **MAJOR**: Removal or redefinition of an existing principle (P1–P7 or SCOPE).
- **MINOR**: Addition of a new principle, section, or materially expanded guidance.
- **PATCH**: Clarification, wording fix, or non-semantic refinement.

All pull requests MUST be reviewed for compliance with every applicable principle.
Complexity deviating from the check order in P7 MUST be justified in the PR
description. The Sync Impact Report comment MUST be removed before any amendment
is committed.

**Version**: 1.0.0 | **Ratified**: 2026-10-05 | **Last Amended**: 2026-10-05
