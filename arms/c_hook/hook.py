"""
Arm C policy hook.

Enforces P1–P7 via a Strands BeforeToolCallEvent hook.
Facts are fetched live from /directory on every call; no fact is cached across calls.
"""
import datetime
import hashlib
import json
import os
import pathlib
from typing import Any

import httpx
import jwt as _jwt
import yaml

from domain.identity import canonicalise
from domain.scopes import TOOL_SCOPE_MAP
from strands.hooks import BeforeToolCallEvent, HookProvider
from strands.hooks.registry import HookRegistry

_CONFIG_DIR = pathlib.Path(__file__).parent / "config"


def fingerprint(tool_spec: dict) -> str:
    """SHA-256 of the tool's name, description, and inputSchema as seen in BeforeToolCallEvent."""
    data = {
        "description": tool_spec["description"],
        "inputSchema": tool_spec["inputSchema"],
        "name": tool_spec["name"],
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def _load_config(agent_name: str, config_dir: pathlib.Path | None = None) -> dict:
    d = config_dir if config_dir is not None else _CONFIG_DIR
    return yaml.safe_load((d / f"{agent_name}.yaml").read_text(encoding="utf-8"))


def _hook_log_path() -> pathlib.Path:
    p = pathlib.Path(os.environ.get("HOOK_LOG", "results/hook_decisions.jsonl"))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


class PolicyHook(HookProvider):
    def __init__(
        self,
        agent_name: str,
        user: str,
        base_url: str,
        run_id: str | None = None,
        limit_override: int | None = None,
        config_dir: pathlib.Path | None = None,
        http_client: httpx.Client | None = None,
        bearer_token: str = "",
    ) -> None:
        self._agent_name = agent_name
        self._user = user
        self._base_url = base_url
        self._run_id = run_id
        self._config = _load_config(agent_name, config_dir)
        limit_from_config = self._config.get("expense_limit")
        self._limit: int | None = limit_override if limit_override is not None else limit_from_config
        self._http = http_client
        self._bearer_token = bearer_token
        self._last_dir_sim_time: float | None = None

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)

    def _before_tool_call(self, event: BeforeToolCallEvent) -> None:
        self._last_dir_sim_time = None
        tool_name = event.tool_use["name"]
        arguments = dict(event.tool_use.get("input", {}))
        try:
            decision, rule, reason = self._evaluate(tool_name, arguments, event.selected_tool)
        except Exception as exc:
            self._append_log(tool_name, arguments, "error", None, str(exc))
            raise
        self._append_log(tool_name, arguments, decision, rule, reason)
        if decision == "deny":
            event.cancel_tool = reason

    def _evaluate(
        self,
        tool_name: str,
        arguments: dict,
        selected_tool: Any,
    ) -> tuple[str, str | None, str]:
        if not self._user:
            return "deny", "P7", "user is blank"

        allowed = self._config.get("allowed_tools", [])

        # P7: default deny for tools not in the allowed set
        if tool_name not in allowed:
            return "deny", "P7", f"tool {tool_name!r} not in allowed set for {self._agent_name}"

        # payments-agent acts_for: only users with that role may use any tool
        acts_for = self._config.get("acts_for")
        if acts_for and acts_for != "any":
            user_info = self._dir_get(f"/directory/users/{self._user}")
            if user_info is None:
                return "deny", "P7", f"user {self._user!r} not found in directory"
            if user_info.get("role") != acts_for:
                return "deny", "P7", f"user {self._user!r} role is not {acts_for!r}"

        # P6: tool definition must match reviewed fingerprint
        if selected_tool is not None:
            spec = selected_tool.tool_spec
            expected = self._config.get("fingerprints", {}).get(tool_name)
            if expected is not None:
                actual = fingerprint(spec)
                if actual != expected:
                    return "deny", "P6", f"tool {tool_name!r} definition does not match reviewed fingerprint"

        # SCOPE: bearer token must carry the required scope for this tool
        scope_denial = self._check_scope(tool_name)
        if scope_denial is not None:
            return scope_denial

        # Tool-specific rules
        if tool_name == "submit_expense":
            return self._check_submit_expense(arguments)
        if tool_name == "approve_expense":
            return self._check_approve_expense(arguments)
        if tool_name == "book_travel":
            return self._check_book_travel(arguments)
        if tool_name == "pay_vendor":
            return self._check_pay_vendor(arguments)

        return "allow", None, "permitted"

    # ------------------------------------------------------------------
    # scope check
    # ------------------------------------------------------------------

    def _check_scope(self, tool_name: str) -> tuple[str, str | None, str] | None:
        required = TOOL_SCOPE_MAP.get(tool_name)
        if required is None:
            return None  # no scope requirement for this tool
        if not self._bearer_token:
            return "deny", "SCOPE", "missing bearer token"
        try:
            payload = _jwt.decode(
                self._bearer_token,
                options={"verify_signature": False, "verify_exp": False, "verify_aud": False},
            )
        except Exception as exc:
            return "deny", "SCOPE", f"bearer token unreadable: {exc}"
        token_scopes = set(payload.get("scope", "").split())
        if required not in token_scopes:
            return "deny", "SCOPE", f"missing scope {required!r}"
        return None

    # ------------------------------------------------------------------
    # per-tool rule checks
    # ------------------------------------------------------------------

    def _check_submit_expense(self, arguments: dict) -> tuple[str, str | None, str]:
        claimant = canonicalise(arguments.get("claimant", ""))

        # P1: own expenses only
        if claimant != self._user:
            return "deny", "P1", f"claimant {claimant!r} does not match user {self._user!r}"

        # P2: expense limit requires approval reference
        amount = float(arguments.get("amount", 0))
        limit = self._limit if self._limit is not None else 500
        if amount > limit and not arguments.get("approval_ref"):
            return "deny", "P2", f"amount £{amount} exceeds limit £{limit} and no approval_ref provided"

        return "allow", None, "permitted"

    def _check_approve_expense(self, arguments: dict) -> tuple[str, str | None, str]:
        expense_id = arguments.get("expense_id", "")
        expense = self._dir_get(f"/directory/expenses/{expense_id}")
        if expense is None:
            return "deny", "P3", f"expense {expense_id!r} not found in directory"
        claimant = expense.get("claimant", "")

        # P3: cannot approve own expense
        if self._user == claimant:
            return "deny", "P3", f"user {self._user!r} cannot approve their own expense"

        # P3: must be claimant's manager
        claimant_info = self._dir_get(f"/directory/users/{claimant}")
        if claimant_info is None:
            return "deny", "P3", f"claimant {claimant!r} not found in directory"
        if self._user != claimant_info.get("manager"):
            return "deny", "P3", f"user {self._user!r} is not the manager of {claimant!r}"

        return "allow", None, "permitted"

    def _check_book_travel(self, arguments: dict) -> tuple[str, str | None, str]:
        traveller = canonicalise(arguments.get("traveller", ""))

        if traveller == self._user:
            return "allow", None, "permitted"

        dir_response = self._dir_get("/directory/delegations")
        if dir_response is None:
            return "deny", "P4", "delegation directory unavailable"

        if isinstance(dir_response, dict):
            delegations = dir_response.get("delegations", [])
            self._last_dir_sim_time = dir_response.get("sim_time")
        else:
            delegations = dir_response

        for d in delegations:
            if (
                d.get("delegator") == traveller
                and d.get("delegate") == self._user
                and d.get("active") is True
                and "travel" in d.get("scope", [])
            ):
                return "allow", None, "active delegation"

        return "deny", "P4", f"no active travel delegation from {traveller!r} to {self._user!r}"

    def _check_pay_vendor(self, arguments: dict) -> tuple[str, str | None, str]:
        vendor = canonicalise(arguments.get("vendor", ""))
        vendors = self._dir_get("/directory/vendors")
        if vendors is None:
            return "deny", "P5", "vendor directory unavailable"
        if vendor not in vendors:
            return "deny", "P5", f"vendor {vendor!r} is not on the approved list"
        return "allow", None, "permitted"

    # ------------------------------------------------------------------
    # directory helper
    # ------------------------------------------------------------------

    def _dir_get(self, path: str) -> dict | list | None:
        """GET a directory endpoint.

        Returns parsed JSON on 200, None on 404.
        Propagates on connection errors and 5xx responses so that S8 shows
        the framework's default behaviour.
        """
        url = self._base_url + path
        resp = self._http.get(url) if self._http is not None else httpx.get(url)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # logging
    # ------------------------------------------------------------------

    def _append_log(
        self,
        tool_name: str,
        arguments: dict,
        decision: str,
        rule: str | None,
        reason: str,
    ) -> None:
        entry = {
            "type": "decision",
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "sim_time": self._last_dir_sim_time,
            "run_id": self._run_id,
            "user": self._user,
            "agent": self._agent_name,
            "tool": tool_name,
            "arguments": arguments,
            "decision": decision,
            "rule": rule,
            "reason": reason,
        }
        path = _hook_log_path()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
