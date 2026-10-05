<!-- SYNC IMPACT REPORT
Version change: (unversioned/placeholder) → 1.0.0
Modified principles: All — initial population from template placeholders
Added sections: Core Principles (P1–P7), Enforcement Architecture, Audit & Observability, Governance
Removed sections: [SECTION_2_NAME], [SECTION_3_NAME] (template placeholders replaced)
Follow-up TODOs: None — all placeholders resolved
-->

# Checkpoint Constitution

## Core Principles

### I. Own Expenses Only (P1)

An agent MUST deny any `submit_expense` call where the `claimant` argument,
after canonicalisation, does not equal the acting user. No agent may submit
an expense on behalf of another person.

### II. Single Expense Limit (P2)

An agent MUST deny any `submit_expense` call where the expense amount exceeds
the configured limit and no `approval_ref` is present. Expenses above the
threshold MUST be refused immediately — not queued for later approval.

### III. No Self-Approval (P3)

An agent MUST deny any `approve_expense` call where the acting user is the
claimant, or where the acting user is not the claimant's manager as recorded
in the directory. This check MUST be performed at call time by fetching live
directory data; no cached reporting-line data may be used.

### IV. Live Travel Delegation (P4)

An agent MUST deny any `book_travel` call made for a traveller who is not the
acting user, unless an active delegation from the traveller to the acting user
exists in the live directory at the moment of the call. The checkpoint MUST
check the `active` field of each returned delegation. It MUST NOT compare
timestamps itself. Delegation state can change between calls; no
session-level caching of delegation state is permitted.

### V. Approved Vendor List (P5)

An agent MUST deny any `pay_vendor` call where the `vendor` argument, after
canonicalisation, is not present in the approved vendor list fetched live from
the directory at call time. The checkpoint MUST NOT maintain a local copy of
the vendor list across calls.

### VI. Reviewed Tool Definition (P6)

An agent MUST deny any tool call whose definition (name, description, and
input schema) does not match the SHA-256 fingerprint recorded at review time.
A tool whose definition has changed since review is treated as a different,
unreviewed tool and must be denied. This check is skipped only when
`event.selected_tool` is `None` or no fingerprint is stored for the tool.

### VII. Default Deny (P7)

Any tool not listed in the agent's `allowed_tools` configuration MUST be
denied. Any call from a blank or unrecognisable user MUST be denied. Any
role mismatch for an agent with a restricted `acts_for` configuration MUST
be denied. When no other rule grants access, the decision is always deny.

## Enforcement Architecture

The checkpoint is a single Python module (`checkpoint.py`) exposing a
`PolicyHook` class that extends the Strands `HookProvider` interface. It
registers a `BeforeToolCallEvent` callback that runs before every tool call
in the agent's execution loop. No server or MCP code is involved.

**Key constraints:**

- All policy facts (allowed tools, fingerprints, user roles, delegations,
  vendors) MUST be fetched live from the directory HTTP API at call time.
  No fact that would require a code change to update may be hard-coded.
- The check order is fixed and non-negotiable:
  1. Blank-user guard (→ P7)
  2. Allowed-tools check (P7)
  3. Acts-for role check (→ P7)
  4. Fingerprint check (P6)
  5. SCOPE bearer-token check (→ SCOPE)
  6. Tool-specific business rules (P1–P5)
- On HTTP 5xx from the directory, the error MUST propagate; the checkpoint
  MUST NOT swallow or retry server errors.
- On HTTP 404, treat as "not found" and apply the relevant deny rule.
- Argument canonicalisation (whitespace-strip + lowercase) MUST be applied
  before any string comparison involving `claimant`, `traveller`, or `vendor`.
- Allowed dependencies: Python stdlib, `httpx`, `PyJWT`, `PyYAML`, `strands`.

## Audit & Observability

Every tool-call evaluation MUST produce exactly one JSON log entry, appended
to the path held in the `HOOK_LOG` environment variable, before the action
runs (even when the decision is deny or error).

Required log fields:

| Field | Content |
|---|---|
| `call_id` | UUID for this specific tool-call evaluation |
| `run_id` | Run identifier passed to the constructor |
| `timestamp` | ISO-8601 UTC timestamp of the decision |
| `sim_time` | Simulation clock time from the directory response, or `null` |
| `user` | Acting user |
| `agent` | Agent name |
| `tool` | Tool name |
| `arguments` | Raw arguments dict |
| `decision` | `"allow"`, `"deny"`, or `"error"` |
| `rule` | Rule id that determined the decision (`P1`–`P7`, `SCOPE`, or `null`) |
| `reason` | Human-readable explanation |

An enforcement decision that cannot produce this record is scored as
**enforcing without audit** and is considered non-compliant.

## Governance

This constitution supersedes all other practices and implementation decisions
for the checkpoint module. Any behaviour that contradicts a stated principle
is a defect, not a feature.

**Amendment procedure:**
- Amendments MUST be proposed with a rationale describing the policy change,
  its motivation, and any migration impact.
- MAJOR bump: removal or incompatible redefinition of any principle (P1–P7).
- MINOR bump: addition of a new principle or material expansion of guidance.
- PATCH bump: clarifications, wording improvements, non-semantic refinements.

**Compliance:**
- All implementations MUST be reviewed against this constitution before
  acceptance.
- Tests MUST cover each principle (P1–P7) individually and the fixed
  check-order sequence.
- All PRs and reviews must verify compliance with the principles above.

**Version**: 1.0.0 | **Ratified**: 2026-10-05 | **Last Amended**: 2026-10-05
