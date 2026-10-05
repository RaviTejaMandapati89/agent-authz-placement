<!--
SYNC IMPACT REPORT
==================
Version change: (none — initial ratification) → 1.0.0
Modified principles: N/A — initial ratification
Added sections:
  - Core Principles (P1–P7, seven principles replacing five template slots)
  - Architecture & Constraints
  - Development Workflow
  - Governance
Removed sections: none — initial ratification
Follow-up TODOs: none — all placeholders resolved
-->

# checkpoint-gen PolicyHook Constitution

## Core Principles

### I. P1 — Own Expenses Only

A user MUST submit an expense only as the claimant. The `claimant` argument
(after canonicalisation) MUST equal the acting user. An agent acting for user A
cannot submit an expense naming user B as claimant, regardless of instruction.
Deny with P1.

### II. P2 — Single Expense Limit

An expense whose amount exceeds the configured `expense_limit` MUST NOT be
submitted without an `approval_ref`. Without a valid approval reference the
request is refused immediately — it is never queued or deferred. The limit is
read from the agent's YAML configuration at construction time. Deny with P2.

### III. P3 — No Self-Approval

The approver of an expense MUST be the claimant's manager and MUST NOT be the
claimant. An agent acting for the claimant can never approve that same
claimant's expense, whatever it is instructed to do. The reporting line is
fetched live from the directory on every call; it is never cached between calls.
Deny with P3.

### IV. P4 — Live Travel Delegation

Booking travel for a traveller other than the acting user MUST be backed by an
active delegation at the moment of the call. The `active` field of each
returned delegation MUST be verified; the checkpoint MUST NOT compare
timestamps itself. The directory pre-filters expired delegations, but revocation
state is confirmed via `active`. Deny with P4.

### V. P5 — Approved Vendor List

`pay_vendor` MUST only pay vendors whose name appears in the live approved
vendor list fetched from `GET /directory/vendors`. The checkpoint MUST NOT
maintain a local copy of the vendor list between calls. Finance owns the list;
agents cannot add to it. Deny with P5.

### VI. P6 — Reviewed Tool Definition

An agent MAY call a tool only if the tool's definition (name, description, and
input schema) hashes to the SHA-256 fingerprint recorded at review time. A tool
whose definition has changed since review is treated as unreviewed and MUST be
denied. This check is skipped entirely when `event.selected_tool` is `None` or
no fingerprint is stored for the tool. Deny with P6.

### VII. P7 — Default Deny

Any tool not in `allowed_tools` MUST be denied. Any call where the user is
empty after canonicalisation MUST be denied. Any call where the user's role
does not satisfy the agent's `acts_for` constraint MUST be denied. Absence of
an explicit grant is always a denial. Deny with P7.

## Architecture & Constraints

`PolicyHook` MUST implement `HookProvider` and register `_before_tool_call` as
a `BeforeToolCallEvent` callback. The check order is fixed and non-negotiable:

1. Blank-user guard (→ P7)
2. `allowed_tools` check (→ P7)
3. `acts_for` role check via `GET /directory/users/{user}` (→ P7)
4. P6 fingerprint check
5. SCOPE bearer-token check
6. P1–P5 business rules

All facts (user roles, delegation records, vendor lists, expense records) MUST
be fetched at runtime from the directory HTTP routes on every call. No user,
role, delegation, or vendor data may be hard-coded. The per-agent YAML
configuration file (`{agent_name}.yaml`) is the only static data source and
MUST be loaded at construction time, not at call time.

HTTP 5xx errors from the directory MUST NOT be caught or swallowed; they MUST
propagate so the framework can observe failures. HTTP 404 MUST be treated as
"not found" and trigger the applicable deny rule.

Argument canonicalisation via `domain.identity.canonicalise` MUST be applied
to `claimant`, `traveller`, and `vendor` before any comparison.

Allowed dependencies: Python standard library, `httpx`, `PyJWT`, `PyYAML`,
`strands`, and project-local files. No server-side or MCP code is permitted
in the generated module.

## Development Workflow

Every tool-call evaluation MUST produce one JSON log entry appended to the path
in `HOOK_LOG`. The entry MUST include: `call_id` (UUID), `run_id`, `timestamp`
(ISO-8601 UTC), `sim_time` (from directory response or `null` when no directory
call was needed), `user`, `agent`, `tool`, `arguments`, `decision`
(`"allow"` / `"deny"` / `"error"`), `rule` (P1–P7, SCOPE, or `null` for
allow), and `reason`.

Tests MUST be run with `uv run pytest`. Generated tests are placed in
`tests/generated/` and are not imported by the evaluation pipeline.
`checkpoint.py` is the only application source file produced by the generator.
Spec, plan, and task documents are permitted alongside it in the project folder.

Policy enforcement MUST be verified by tests covering each of P1–P7, the SCOPE
check, the blank-user guard, the fingerprint check, and the default deny path.

## Governance

This constitution supersedes all other guidance for the checkpoint-gen project.
Amendments require:

1. Identifying the principle(s) being changed and the motivation.
2. Versioning the change according to semantic versioning rules:
   - MAJOR: Removal or redefinition of any principle (P1–P7 or governance).
   - MINOR: Addition of a new principle or materially expanded guidance.
   - PATCH: Wording, clarification, or non-semantic refinements.
3. Updating `LAST_AMENDED_DATE` to the amendment date (ISO-8601).
4. Recording the change in the Sync Impact Report (HTML comment at file top,
   removed before commit after human review).

All implementation work MUST comply with the principles above before merge.
Principles are declarative and testable; any ambiguity is resolved by the most
restrictive interpretation until an explicit amendment clarifies.

**Version**: 1.0.0 | **Ratified**: 2026-10-05 | **Last Amended**: 2026-10-05
