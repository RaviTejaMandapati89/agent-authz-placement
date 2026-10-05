# Implementation Plan: PolicyHook Checkpoint Enforcement

**Branch**: `001-policy-hook-checkpoint` | **Date**: 2026-10-05 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-policy-hook-checkpoint/spec.md`

## Summary

Implement `PolicyHook` in `checkpoint.py`: a `HookProvider` subclass that enforces seven policy rules (P1–P7) plus a bearer-token scope check before every Strands agent tool call. All facts (allowed tools, fingerprints, role restrictions, expense limits) are loaded from a per-agent YAML config at construction time. Live facts (user roles, delegations, vendor list, expense records) are fetched from an HTTP directory at call time. Every evaluation produces one structured JSON log entry.

## Technical Context

**Language/Version**: Python 3.12 (`.venv`); requires ≥ 3.11 (`pyproject.toml`)

**Primary Dependencies**: `strands-agents>=0.1`, `httpx>=0.27`, `PyJWT>=2.8`, `PyYAML>=6.0.3`, `cryptography>=42`

**Storage**: File-based log (append-only JSONL to `HOOK_LOG`); YAML config files in `config/agents/`

**Testing**: `pytest>=8` with `asyncio_mode=auto` (see `pyproject.toml`)

**Target Platform**: macOS / Linux (no platform-specific code needed)

**Project Type**: Single-file library module (`checkpoint.py` in project root)

**Performance Goals**: Not specified; tool calls are synchronous; directory HTTP calls are the bottleneck

**Constraints**: Single file `checkpoint.py`; imports restricted to stdlib + `httpx`, `PyJWT`, `PyYAML`, `strands`, and kit files

**Scale/Scope**: One module, one class; tested via `tests/test_contract.py` and `tests/generated/`

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Gate Status | Notes |
|-----------|-------------|-------|
| P1. Own Expenses Only | ✅ PASS | FR-012 requires `claimant == user` check with canonicalisation |
| P2. Single Expense Limit | ✅ PASS | FR-013 requires `amount > limit → deny unless approval_ref present` |
| P3. No Self-Approval | ✅ PASS | FR-014 requires live directory fetch; approver must be claimant's manager |
| P4. Live Travel Delegation | ✅ PASS | FR-015 requires live fetch + `active` field check; no timestamp comparison |
| P5. Approved Vendor List | ✅ PASS | FR-016 requires live vendor list fetch with canonicalised comparison |
| P6. Reviewed Tool Definition | ✅ PASS | FR-009/FR-010: skip if `selected_tool is None` or no fingerprint stored |
| P7. Default Deny | ✅ PASS | FR-006/FR-007: blank-user and allowed_tools checks fire first |
| Audit & Decision Log | ✅ PASS | FR-019/FR-020: one log entry per call, all required fields |
| Implementation Constraints | ✅ PASS | Single file, no MCP, no network listener, correct import set |
| 5xx propagation | ✅ PASS | FR-017: no try/except around httpx HTTP calls |
| No hard-coded facts | ✅ PASS | FR-004: all config from YAML, all live facts from directory |

**Post-design re-check**: All principles satisfied by the design in `data-model.md`. No violations.

**Complexity violations**: None — the design fits the single-file, no-MCP constraint.

## Project Structure

### Documentation (this feature)

```text
specs/001-policy-hook-checkpoint/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/
│   ├── policyhook-api.md           # Python API contract
│   └── decision-log-schema.json    # JSON Schema for log entries
└── tasks.md             # Phase 2 output (not yet created)
```

### Source Code (repository root)

```text
checkpoint.py            # The single generated file (PolicyHook class)

config/
└── agents/
    ├── expense-assistant.yaml
    ├── payments-agent.yaml
    └── travel-assistant.yaml

domain/
├── identity.py          # canonicalise() function
├── scopes.py            # TOOL_SCOPE_MAP
├── tokens.py            # JWT issuance (not imported by checkpoint.py)
├── simclock.py          # Simulation clock (not imported by checkpoint.py)
├── fixtures.py          # Test fixtures
├── server.py            # Directory HTTP server
└── state.py             # Shared mutable state for tests

tests/
├── __init__.py
├── test_contract.py     # Provided contract tests (must not be modified)
└── generated/           # Generator-written tests (may be created)
```

**Structure Decision**: Single project. `checkpoint.py` lives at the root, co-located with `domain/` and `config/`. No `src/` layout — `checkpoint` is imported directly.

## Complexity Tracking

No constitution violations — this table is intentionally empty.
