# Implementation Plan: PolicyHook Checkpoint Module

**Branch**: `001-policyhook-checkpoint` | **Date**: 2026-10-05 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-policyhook-checkpoint/spec.md`

## Summary

Implement a single `PolicyHook` class in `checkpoint.py` that integrates with the Strands agent
framework to enforce seven policy rules (P1–P7) and an OAuth2 scope check (SCOPE) on every tool
call. All policy-relevant data is loaded at construction time from a per-agent YAML config file;
live directory facts are fetched via synchronous HTTP on each evaluation. The module is a single
file with no hardcoded policy data.

## Technical Context

**Language/Version**: Python 3.12

**Primary Dependencies**:
- `httpx` — synchronous HTTP client for directory lookups (sync `httpx.Client` stored on instance)
- `PyJWT` — JWT decode without signature verification to extract the `scope` claim
- `PyYAML` — load per-agent YAML config at construction time
- `strands` — `HookProvider` protocol, `BeforeToolCallEvent`, `HookRegistry`

**Storage**:
- Agent config: YAML file at `{config_dir}/{agent_name}.yaml` — read once at construction
- Decision log: JSONL file at path from `HOOK_LOG` env var — one line appended per evaluation

**Testing**: pytest via `uv run pytest`

**Target Platform**: In-process with Strands agent on macOS/Linux server

**Project Type**: Library module (generates exactly one file: `checkpoint.py`)

**Performance Goals**: No throughput target; each tool-call evaluation makes ≤4 synchronous HTTP
requests (users, delegations, vendors, expenses) to the directory service as required by the rule
being evaluated.

**Constraints**:
- Single output file `checkpoint.py` at repo root
- Imports limited to stdlib + httpx + PyJWT + PyYAML + strands + kit files in `domain/`
- Zero hardcoded policy data

**Scale/Scope**: One PolicyHook instance per agent; evaluated synchronously for every tool call.

## Constitution Check

| Principle | Check | Status |
|---|---|---|
| I. Runtime-Only Facts | `allowed_tools`, `fingerprints`, `acts_for`, `expense_limit` loaded from YAML at construction; user/delegation/vendor/expense facts fetched from directory on each call — nothing hardcoded in `checkpoint.py` | PASS |
| II. Deny by Default | Blank user → deny P7; tool not in `allowed_tools` → deny P7; role mismatch → deny P7; bad/missing scope → deny SCOPE; no explicit allow path reached → deny | PASS |
| III. Fixed Check Order | blank-user (P7) → allowed\_tools (P7) → acts\_for role (P7) → fingerprint (P6) → SCOPE → P1–P5 — no reordering or skipping | PASS |
| IV. No Silent Failures | `httpx.HTTPStatusError` for 5xx propagates uncaught; every code path writes a log entry before returning | PASS |
| V. Minimal Dependency Surface | Only stdlib + httpx + PyJWT + PyYAML + strands + `domain/` kit files — enforced by import list | PASS |
| VI. Single-File Module Boundary | Output is exactly `checkpoint.py`; no server code, no MCP protocol code | PASS |
| VII. Argument Canonicalisation | `domain.identity.canonicalise` applied to `claimant`, `traveller`, and `vendor` before all comparisons | PASS |

**Gate result**: All principles satisfied. No violations requiring justification.

## Project Structure

### Documentation (this feature)

```
specs/001-policyhook-checkpoint/
├── plan.md              # This file (/speckit-plan output)
├── research.md          # Phase 0 output (/speckit-plan)
├── data-model.md        # Phase 1 output (/speckit-plan)
├── quickstart.md        # Phase 1 output (/speckit-plan)
├── contracts/           # Phase 1 output (/speckit-plan)
│   └── policyhook-interface.md
└── tasks.md             # Phase 2 output (/speckit-tasks — NOT created by /speckit-plan)
```

### Source Code (repository root)

```
checkpoint.py            # Single generated output file

config/agents/
├── expense-assistant.yaml
├── travel-assistant.yaml
└── payments-agent.yaml

domain/
├── identity.py          # canonicalise() — imported by checkpoint
├── scopes.py            # TOOL_SCOPE_MAP — imported by checkpoint
├── tokens.py            # JWT issuance (test fixtures only, not used by checkpoint)
├── fixtures.py          # Static test data (not used at runtime)
├── server.py            # Directory HTTP server (test fixture)
├── state.py             # Mutable fixture state (test fixture)
└── simclock.py          # Simulation clock (test fixture)

tests/
├── test_contract.py     # Existing contract/format tests
└── generated/           # Generator-written tests (not in production pipeline)
```

**Structure Decision**: Single-project layout (Option 1). All production code in one flat file
at repo root; `domain/` kit supplies importable utilities; `tests/` holds contract tests and
generator-written generated tests.

## Complexity Tracking

No constitution violations — this section is not applicable.
