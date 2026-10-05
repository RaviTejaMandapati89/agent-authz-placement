# Implementation Plan: PolicyHook Checkpoint Enforcement

**Branch**: `001-policy-hook-checkpoint` | **Date**: 2026-10-05 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/001-policy-hook-checkpoint/spec.md`

## Summary

Implement `checkpoint.py` — a single Python module exporting `PolicyHook(HookProvider)` that
registers a `BeforeToolCallEvent` callback with the Strands agent framework. The hook evaluates
seven policy rules (P1–P7 plus a SCOPE bearer-token check) in a fixed, non-configurable order
before every tool call. All policy facts are read from an agent YAML config file and from a live
HTTP directory; nothing is hard-coded. Every evaluation produces exactly one JSONL audit entry.

## Technical Context

**Language/Version**: Python 3.12 (`.venv`); `requires-python = ">=3.11"`

**Primary Dependencies**:
- `strands-agents` — `HookProvider`, `HookRegistry`, `BeforeToolCallEvent`, `AgentTool`, `ToolSpec`
- `httpx` — synchronous HTTP client for all directory lookups
- `PyJWT` — bearer token decode and scope extraction
- `PyYAML` — agent config loading
- `cryptography` — JWT key operations (transitive dep of PyJWT)

**Storage**: No persistent state. Append-only JSONL audit log written to the path in `$HOOK_LOG`.

**Testing**: `pytest 8` with `pytest-asyncio` (`asyncio_mode = "auto"`)

**Target Platform**: Pure Python library module — no server, no MCP, no async I/O in the hook

**Project Type**: Single-file library — `checkpoint.py` at project root

**Performance Goals**: Non-directory checks are sub-millisecond. Directory lookups are
synchronous `httpx` calls; latency is bounded by the directory service RTT.

**Constraints**:
- No caching of any directory fact across calls (FR-016)
- HTTP 5xx responses must propagate uncaught (FR-014)
- All policy facts sourced from YAML config or live directory (FR-002)
- `checkpoint.py` is the only generated source file; generator tests go in `tests/generated/`

**Scale/Scope**: One `PolicyHook` instance per agent session; up to 5 directory HTTP requests per
tool evaluation in the worst case (user role, expense record, expense claimant's manager,
delegations, vendor list).

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Own Expenses Only (P1) | PASS | FR-008 — claimant after canonicalise == acting user |
| II. Single Expense Limit (P2) | PASS | FR-009 — config-driven limit; no approval_ref triggers deny |
| III. No Self-Approval (P3) | PASS | FR-010 — live directory fetch for manager; no cached reporting lines |
| IV. Live Travel Delegation (P4) | PASS | FR-011 — active field check only; no timestamp compare; no caching |
| V. Approved Vendor List (P5) | PASS | FR-012 — live vendor list fetched every call |
| VI. Reviewed Tool Definition (P6) | PASS | FR-006 — SHA-256 from config; skipped when no fingerprint or selected_tool is None |
| VII. Default Deny (P7) | PASS | FR-003/004/005 — blank-user, allowed-tools, role-mismatch all deny with P7 |
| Audit & Observability | PASS | FR-015 — one entry per evaluation with all 11 required fields |
| 5xx Propagation | PASS | FR-014 — no catch/swallow of directory 5xx errors |
| No Hard-coded Facts | PASS | FR-002 — all facts from config file or live directory at call time |

No constitution violations. Complexity tracking not required.

**Post-Phase-1 re-check**: All design artifacts confirmed consistent with constitution.
`data-model.md` entities and `contracts/` schemas reflect live-fetch-only policy. No caching
introduced in any design artifact.

## Project Structure

### Documentation (this feature)

```text
specs/001-policy-hook-checkpoint/
├── plan.md              # This file (/speckit-plan output)
├── research.md          # Phase 0 output (/speckit-plan)
├── data-model.md        # Phase 1 output (/speckit-plan)
├── quickstart.md        # Phase 1 output (/speckit-plan)
├── contracts/           # Phase 1 output (/speckit-plan)
│   ├── PolicyHook.md
│   ├── agent-config-schema.md
│   └── log-entry-schema.md
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created here)
```

### Source Code (repository root)

```text
checkpoint.py            # Only generated source file

config/
└── agents/
    ├── expense-assistant.yaml
    ├── travel-assistant.yaml
    └── payments-agent.yaml

domain/
├── identity.py          # canonicalise() — strip + lowercase
├── scopes.py            # TOOL_SCOPE_MAP: tool name → required scope string
└── ...                  # server, tokens, state, simclock (harness, not imported by checkpoint)

tests/
├── test_contract.py     # Existing format/contract tests
└── generated/           # Generator may write additional tests here
```

**Structure Decision**: Single-project flat layout. `checkpoint.py` at the project root,
alongside the existing `domain/` and `config/` directories.
