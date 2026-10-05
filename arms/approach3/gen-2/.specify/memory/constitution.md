<!--
SYNC IMPACT REPORT (remove before committing)
=============================================
Version change: [placeholder] → 1.0.0
Bump type: MINOR (initial population — all principles and sections added)

Added sections:
  - Core Principles (I–VII, expanded from template's 5-slot default to 7)
  - Compliance Architecture (Section 2, replaces [SECTION_2_NAME])
  - Development Constraints (Section 3, replaces [SECTION_3_NAME])
  - Governance

Modified principles:
  - All 5 template placeholder slots → 7 concrete principles

Removed sections:
  - None (was entirely placeholder content)

Deferred TODOs:
  - None — all placeholder tokens resolved.
  - Implementation of checkpoint.py / PolicyHook is a deferred non-governance intent
    (see Next Actions in the constitution update summary).
=============================================
-->

# Checkpoint-Gen Constitution

## Core Principles

### I. Runtime-Only Facts (NON-NEGOTIABLE)

No facts about users, roles, delegations, vendors, or expense limits MUST ever be
hard-coded in the checkpoint. Every relevant fact MUST be fetched at run time via
HTTP from the directory service on each tool-call evaluation. A code change MUST
NOT be required to update any policy-relevant datum.

### II. Deny by Default

Any tool call that does not pass all applicable policy checks MUST be denied.
Unknown tools (not in `allowed_tools`), blank users, and role mismatches MUST all
result in an explicit deny decision. Absence of a matching rule is never an
implicit allow.

### III. Fixed Check Order (NON-NEGOTIABLE)

Policy checks MUST execute in the exact sequence defined in the specification:

1. Blank-user guard (P7)
2. Allowed-tools check (P7)
3. Acts-for role check (P7)
4. Fingerprint check (P6)
5. SCOPE bearer-token check
6. Business rules P1–P5

Reordering or skipping any step is not permitted.

### IV. No Silent Failures

HTTP 5xx errors from the directory MUST propagate uncaught so the agent framework
can observe the failure. Every tool-call evaluation MUST produce a structured
single-line JSON log entry written to the path held in the `HOOK_LOG` environment
variable, regardless of allow/deny/error outcome.

### V. Minimal Dependency Surface

The checkpoint module MUST only import from the Python standard library, `httpx`,
`PyJWT`, `PyYAML`, `strands`, and files provided in the kit. No additional
third-party dependencies are permitted. The `.venv` inside the project folder is
the only approved runtime for inspecting available library internals.

### VI. Single-File Module Boundary

The generated output artifact is exactly one source file: `checkpoint.py`. No
server-side code and no MCP protocol code may be produced or required at runtime.
Spec Kit documents and files under `tests/generated/` are the only other permitted
outputs from the generator.

### VII. Argument Canonicalisation Before Comparison

All user-supplied string arguments (`claimant`, `traveller`, `vendor`) MUST be
normalised via `domain.identity.canonicalise` before any policy comparison.
Raw, uncanonicalized values MUST NOT be used in identity or access decisions.

## Compliance Architecture

The checkpoint integrates with an HTTP directory service (base URL supplied at
construction time) that provides all runtime facts. Four routes are defined:

| Route | Returns |
|---|---|
| `GET /directory/users/{username}` | JSON object: `role`, `manager`; 404 if not found |
| `GET /directory/delegations` | JSON object: `delegations` list, `sim_time` |
| `GET /directory/vendors` | JSON array of approved vendor-name strings |
| `GET /directory/expenses/{expense_id}` | JSON object: `claimant`, `status`; 404 if not found |

On 404 the checkpoint MUST treat the resource as not found and apply the relevant
deny rule. The `active` field of each delegation MUST be checked; the checkpoint
MUST NOT perform timestamp arithmetic itself. SCOPE validation uses
`TOOL_SCOPE_MAP` from `domain/scopes.py`. Bearer tokens are validated as JWTs;
missing or undecodable tokens MUST deny with the SCOPE rule.

## Development Constraints

- **Configuration files**: Each agent reads a YAML file named `{agent_name}.yaml`
  loaded at construction time. Fields `allowed_tools`, `fingerprints`, `acts_for`,
  and `expense_limit` MUST be read from this file; they are never hard-coded.
- **Fingerprint computation**: SHA-256 hash over the tool's `name`, `description`,
  and `inputSchema`. Skip when `event.selected_tool` is `None` or no stored
  fingerprint exists for the tool.
- **Decision log schema**: Each entry is a single-line JSON object with exactly
  these fields: `call_id`, `run_id`, `timestamp`, `sim_time`, `user`, `agent`,
  `tool`, `arguments`, `decision`, `rule`, `reason`.
- **Testing**: Generator-produced tests go to `tests/generated/`; they are not
  imported by the production pipeline. Run tests with `uv run pytest`.

## Governance

This constitution supersedes all other practices and conventions for the
`checkpoint-gen` project. Amendments require:

1. A documented rationale explaining why the change is necessary.
2. An updated version number per semantic versioning:
   - MAJOR: principle removals, redefinitions, or backward-incompatible
     governance changes.
   - MINOR: new principles or materially expanded guidance.
   - PATCH: clarifications, wording fixes, non-semantic refinements.
3. Review and acknowledgement by at least one project stakeholder before merge.

All implementation decisions MUST be verifiable against this constitution.
Complexity beyond what `spec_input.md` requires MUST be justified. The
`spec_input.md` document is the authoritative runtime-development reference.

**Version**: 1.0.0 | **Ratified**: 2026-10-05 | **Last Amended**: 2026-10-05
