"""Approach 5 policy plugin — use-case Cedar plus central decision service.

Facts come from live state via POST /central/decide, not from token person claims.
This module has no knowledge of the module behind that route.
"""
import os
import pathlib
import time

import cedarpy
import httpx

# A per-run override directory may replace the committed policies; the
# committed files are never edited.
_POLICIES_DIR = pathlib.Path(
    os.environ.get("SHARED_POLICIES_DIR")
    or pathlib.Path(__file__).parent.parent / "shared" / "policies"
)

AGENT_USE_CASE: dict[str, str] = {
    "expense-assistant": "expenses",
    "travel-assistant":  "travel",
    "payments-agent":    "payments",
}

_policy_cache: dict[str, cedarpy.PolicySet] = {}

# Set by tests (ASGITransport) or left None for real subprocess deployments.
_central_transport: httpx.AsyncBaseTransport | None = None
# The central source is this server; its port comes from the environment.
_CENTRAL_BASE_URL = f"http://127.0.0.1:{os.environ.get('SERVER_PORT', '8765')}"


def _load_policy(use_case: str) -> cedarpy.PolicySet:
    if use_case not in _policy_cache:
        text = (_POLICIES_DIR / f"{use_case}.cedar").read_text(encoding="utf-8")
        _policy_cache[use_case] = cedarpy.PolicySet.from_str(text)
    return _policy_cache[use_case]


def _rule_id_from_result(result: cedarpy.AuthzResult) -> str | None:
    annotations = result.diagnostics.id_annotations_by_reason
    if annotations:
        return next(iter(annotations.values()))
    return None


def _build_uc_context(tool: str, arguments: dict) -> dict:
    ctx: dict = {"action_name": tool}
    if tool == "submit_expense":
        if "claimant" in arguments:
            ctx["claimant"] = str(arguments["claimant"])
        if "amount" in arguments:
            ctx["amount_pence"] = int(round(float(arguments["amount"]) * 100))
        ctx["has_approval_ref"] = bool(arguments.get("approval_ref"))
    elif tool == "pay_vendor":
        if "vendor" in arguments:
            ctx["vendor"] = str(arguments["vendor"])
    return ctx


async def evaluate(claims: dict, agent_chain: list, tool: str, arguments: dict) -> dict:
    if tool == "ask_payments_agent":
        return {"decision": "allow", "rule": None, "reason": "delegation",
                "central_called": False, "central_duration_ms": 0}

    agent = agent_chain[0] if agent_chain else None
    use_case = AGENT_USE_CASE.get(agent)
    if use_case is None:
        return {
            "decision": "deny",
            "rule": "GRANT",
            "reason": f"unknown agent {agent!r}",
            "central_called": False,
            "central_duration_ms": 0,
        }

    user = claims.get("sub", "")

    async with httpx.AsyncClient(
        transport=_central_transport,
        base_url=_CENTRAL_BASE_URL,
    ) as client:
        # Step 1: get live user role (not from token claims)
        role = ""
        central_calls = 1  # the directory read below; the decide call is counted after
        try:
            r = await client.get(f"/directory/users/{user}")
            if r.status_code == 200:
                role = r.json().get("role", "")
        except Exception:
            pass

        # Step 2: call /central/decide (timed)
        t0 = time.monotonic()
        try:
            r2 = await client.post("/central/decide", json={
                "user": user,
                "tool": tool,
                "arguments": arguments,
            })
            if r2.status_code == 200:
                central = r2.json()
            else:
                central = {
                    "decision": "deny",
                    "rule": "CENTRAL_UNAVAILABLE",
                    "reason": f"central service returned HTTP {r2.status_code}",
                }
        except Exception as exc:
            central = {
                "decision": "deny",
                "rule": "CENTRAL_UNAVAILABLE",
                "reason": f"central service unreachable: {exc}",
            }
        central_duration_ms = (time.monotonic() - t0) * 1000

    # Step 3: evaluate use-case Cedar policy with live role
    policy_set = _load_policy(use_case)
    ctx = _build_uc_context(tool, arguments)
    entities = [{
        "uid": {"type": "User", "id": user},
        "attrs": {"username": user, "role": role},
        "parents": [],
    }]
    request = {
        "principal": {"type": "User", "id": user},
        "action":    {"type": "Action", "id": tool},
        "resource":  {"type": "Resource", "id": "r"},
        "context":   ctx,
    }
    uc_result = cedarpy.is_authorized(request=request, policies=policy_set, entities=entities)
    uc_rule = _rule_id_from_result(uc_result) if not uc_result.allowed else None

    # Step 4: combine — central wins; log central rule when both refuse
    central_denied = central.get("decision") == "deny"
    central_rule = central.get("rule")

    if central_denied:
        return {
            "decision": "deny",
            "rule": central_rule,
            "reason": central.get("reason", f"denied by rule {central_rule}"),
            "central_called": True,
            "central_calls": central_calls + 1,
            "central_duration_ms": central_duration_ms,
        }

    if uc_rule is not None:
        return {
            "decision": "deny",
            "rule": uc_rule,
            "reason": f"denied by rule {uc_rule}",
            "central_called": True,
            "central_calls": central_calls + 1,
            "central_duration_ms": central_duration_ms,
        }

    return {
        "decision": "allow",
        "rule": None,
        "reason": "",
        "central_called": True,
        "central_calls": central_calls + 1,
        "central_duration_ms": central_duration_ms,
    }

