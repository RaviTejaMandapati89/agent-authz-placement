# Implementation Plan: PolicyHook Class — Agent Checkpoint Enforcement

**Branch**: `001-policy-hook` | **Date**: 2026-10-05 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-policy-hook/spec.md`

## Summary

Implement a `PolicyHook` class in `checkpoint.py` that integrates with the Strands agent framework's `BeforeToolCallEvent` to enforce seven policy rules (P1–P7) and a bearer-token scope check (SCOPE) before every tool call. All agent-level policy values are loaded at construction from a per-agent YAML config file; all runtime facts are fetched on-demand from the directory HTTP service. Every evaluation — allow or deny — is logged to `HOOK_LOG` as structured JSON.

## Technical Context

**Language/Version**: Python 3.12 (`.venv` confirms 3.12)

**Primary Dependencies**: `strands-agents>=0.1`, `httpx>=0.27`, `PyJWT>=2.8`, `PyYAML>=6.0.3` (all present in `pyproject.toml`)

**Storage**: Per-agent YAML config files in `config/agents/`; append-only JSONL decision log at `$HOOK_LOG`

**Testing**: `pytest` via `uv run pytest`; generated tests go to `tests/generated/`

**Target Platform**: Python process (server-side agent hook)

**Project Type**: Library module — single generated file `checkpoint.py`

**Performance Goals**: N/A (per-call enforcement, not throughput-bound)

**Constraints**: No policy-relevant fact may be cached across calls; HTTP 5xx must propagate; import list restricted to stdlib + httpx + PyJWT + PyYAML + strands + provided kit files

**Scale/Scope**: Single `checkpoint.py` file; tests cover every deny path and allow path per spec

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | Requirement Mapping | Status |
|-----------|--------------------|----|
| P1. Own Expenses Only | FR-010, spec §5 | PASS — rule implemented in P1–P5 block |
| P2. Single Expense Limit | FR-010, spec §5 | PASS — `expense_limit` from YAML, `limit_override` at construction |
| P3. No Self-Approval | FR-010, spec §5 | PASS — directory lookup for claimant and manager |
| P4. Live Travel Delegation | FR-010, spec §5 | PASS — fetches `/directory/delegations` each call; checks `active` field only |
| P5. Approved Vendor List | FR-010, spec §5 | PASS — fetches `/directory/vendors` each call |
| P6. Reviewed Tool Definition | FR-010, spec §5 | PASS — skipped when `selected_tool` is None |
| P7. Default Deny | FR-004, FR-010 | PASS — blank-user and allowed_tools checks fire first |
| Tech: No hard-coded facts | FR-002, FR-006 | PASS — YAML at construction; directory at call time |
| Tech: 5xx propagate | FR-007 | PASS — httpx raises; hook does not catch |
| Tech: 404 as not-found | FR-008 | PASS — per-route handling |
| Tech: Canonicalisation | FR-005 | PASS — `domain.identity.canonicalise` on claimant/traveller/vendor |
| Tech: Log every call | FR-011, FR-012 | PASS — `HOOK_LOG` append in every code path |
| Tech: Import restrictions | FR-013 | PASS — only stdlib + httpx + PyJWT + PyYAML + strands + kit |
| Check order | FR-004, constitution §Dev | PASS — blank-user → P7 → acts_for → P6 → SCOPE → P1–P5 |

**Gate result: PASS — no violations.**

## Project Structure

### Documentation (this feature)

```text
specs/001-policy-hook/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
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
├── identity.py          # canonicalise() — provided, not generated
├── scopes.py            # TOOL_SCOPE_MAP — provided, not generated
└── ...                  # other kit files, not generated

tests/
├── test_contract.py     # Existing contract tests (format/signature)
└── generated/           # Generated policy-rule tests (created by impl phase)
```

**Structure Decision**: Single-project layout. `checkpoint.py` lives at repo root so `from checkpoint import PolicyHook` resolves without package qualification, matching the existing `test_contract.py` import pattern.
