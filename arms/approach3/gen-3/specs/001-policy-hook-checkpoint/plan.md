# Implementation Plan: PolicyHook Checkpoint Enforcement

**Branch**: `001-policy-hook-checkpoint` | **Date**: 2026-10-05 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/001-policy-hook-checkpoint/spec.md`

## Summary

Implement `PolicyHook` — a single Python class in `checkpoint.py` (project root) that registers a `_before_tool_call` callback with the Strands `BeforeToolCallEvent`, evaluates every tool call against a fixed sequence of seven rules (P1–P7 + SCOPE check), fetches live facts from the directory HTTP service on every call, and appends one structured JSON audit entry to `HOOK_LOG` for every evaluation.

## Technical Context

**Language/Version**: Python 3.12 (`.venv`); `requires-python = ">=3.11"`

**Primary Dependencies**:
- `strands-agents>=0.1` — `HookProvider` Protocol, `BeforeToolCallEvent` dataclass, `HookRegistry`
- `httpx>=0.27` — synchronous directory HTTP calls (sync client required; callback is non-async)
- `PyJWT>=2.8` — bearer token decoding for SCOPE check
- `PyYAML>=6.0.3` — agent configuration loading

**Storage**: Append-only JSONL audit file at the path held in the `HOOK_LOG` environment variable; agent YAML config files read once at construction time from `config/agents/{agent_name}.yaml` (or `config_dir` override)

**Testing**: `pytest>=8` with `pytest-asyncio` (`asyncio_mode = "auto"`); run with `uv run pytest`; generated policy tests may be placed in `tests/generated/` (not evaluated by the pipeline)

**Target Platform**: In-process Python agent hook; no server or MCP code produced

**Project Type**: Single-file module (`checkpoint.py` at project root)

**Performance Goals**: At most 2–3 synchronous HTTP requests per tool-call evaluation; no explicit latency SLO

**Constraints**:
- `checkpoint.py` is the **only** application source file produced (FR-012)
- Imports restricted to: stdlib, `httpx`, `PyJWT`, `PyYAML`, `strands`, and project-local `domain/identity.py` and `domain/scopes.py`
- `_before_tool_call` must be a synchronous callback (Strands `invoke_callbacks` raises `RuntimeError` on async callbacks)

**Scale/Scope**: One `PolicyHook` instance per agent-session; evaluates every tool call for that session

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Spec alignment | Status |
|-----------|---------------|--------|
| P1 — Own Expenses Only | `submit_expense`: `canonicalise(claimant) == user`; deny P1 otherwise | PASS |
| P2 — Single Expense Limit | `submit_expense`: amount > `expense_limit` and no `approval_ref` → deny P2; no limit when field absent | PASS |
| P3 — No Self-Approval | `approve_expense`: fetch expense claimant; user == claimant → deny P3; user ≠ claimant's manager → deny P3 | PASS |
| P4 — Live Travel Delegation | `book_travel`: if traveller ≠ user, fetch `/directory/delegations`; check `active` only, no timestamp math | PASS |
| P5 — Approved Vendor List | `pay_vendor`: fetch `/directory/vendors` (JSON array) every call; vendor not in list → deny P5 | PASS |
| P6 — Reviewed Tool Definition | SHA-256 over canonical JSON of `{name, description, inputSchema}` vs stored fingerprint; skip if `selected_tool is None` or no fingerprint stored | PASS |
| P7 — Default Deny | Three triggers: blank user after canonicalise, tool not in `allowed_tools`, role mismatch in `acts_for` check | PASS |

**Architecture constraints check**:

- Check order is fixed and non-negotiable (blank-user → allowed_tools → acts_for → P6 → SCOPE → P1–P5): ✓
- All live facts fetched per-call from directory; none cached between calls: ✓
- HTTP 5xx errors propagate (call `raise_for_status()` after filtering 404); 404 → "not found" → applicable deny rule: ✓
- Agent YAML config loaded once at construction, not at call time: ✓
- `canonicalise()` applied to `claimant`, `traveller`, `vendor` before any comparison: ✓
- No server, MCP, or non-permitted imports: ✓

**Gate: PASS — no violations**

**Post-Phase 1 re-check**: All design artifacts (data model, contracts, quickstart) respect the same constraints. Gate remains PASS.

## Project Structure

### Documentation (this feature)

```text
specs/001-policy-hook-checkpoint/
├── plan.md              # This file (/speckit-plan output)
├── research.md          # Phase 0 output (/speckit-plan)
├── data-model.md        # Phase 1 output (/speckit-plan)
├── quickstart.md        # Phase 1 output (/speckit-plan)
├── contracts/           # Phase 1 output (/speckit-plan)
│   ├── directory-api.md
│   └── hook-log-schema.md
└── tasks.md             # Phase 2 output (/speckit-tasks — not yet created)
```

### Source Code

```text
checkpoint.py            # Only application source file produced (project root)

config/
└── agents/
    ├── expense-assistant.yaml   # acts_for: any, limit: 500
    ├── travel-assistant.yaml    # acts_for: any, limit: 500
    └── payments-agent.yaml      # acts_for: finance, no limit

domain/
├── identity.py          # canonicalise() — imported by checkpoint.py
└── scopes.py            # TOOL_SCOPE_MAP — imported by checkpoint.py

tests/
├── test_contract.py     # Existing format/contract tests
└── generated/           # Policy tests written by generator (not pipeline-evaluated)
```

**Structure Decision**: Single-project layout; `checkpoint.py` at the project root (required by `test_contract.py`'s `from checkpoint import PolicyHook`). `domain/` provides the only project-local imports. No `src/` layout.
