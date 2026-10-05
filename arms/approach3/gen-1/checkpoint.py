import hashlib
import json
import os
import pathlib
import uuid
from datetime import datetime

import httpx
import jwt
import yaml
from strands.hooks import BeforeToolCallEvent, HookProvider, HookRegistry

from domain.identity import canonicalise
from domain.scopes import TOOL_SCOPE_MAP


class PolicyHook(HookProvider):

    def __init__(
        self,
        agent_name: str,
        user: str,
        base_url: str,
        run_id: str | None = None,
        limit_override: int | None = None,
        config_dir: pathlib.Path | None = None,
        bearer_token: str = "",
    ):
        if config_dir is None:
            config_dir = pathlib.Path(__file__).parent / "config" / "agents"
        config_path = config_dir / f"{agent_name}.yaml"
        if not config_path.exists():
            raise FileNotFoundError(f"Agent config not found: {config_path}")
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        self._agent_name = agent_name
        self._user = user
        self._base_url = base_url
        self._run_id = run_id
        self._bearer_token = bearer_token
        self._allowed_tools = set(config["allowed_tools"])
        self._fingerprints = config.get("fingerprints", {})
        self._acts_for = config["acts_for"]
        self._expense_limit = limit_override if limit_override is not None else config.get("expense_limit", 500)

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)

    def _write_log(self, entry: dict) -> None:
        log_path = os.environ.get("HOOK_LOG")
        if not log_path:
            return
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _make_log_entry(
        self,
        *,
        tool: str,
        arguments: dict,
        decision: str,
        rule: str | None,
        reason: str,
        sim_time: float | None = None,
    ) -> dict:
        return {
            "call_id": str(uuid.uuid4()),
            "run_id": self._run_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "sim_time": sim_time,
            "user": self._user,
            "agent": self._agent_name,
            "tool": tool,
            "arguments": arguments,
            "decision": decision,
            "rule": rule,
            "reason": reason,
        }

    def _before_tool_call(self, event: BeforeToolCallEvent) -> None:
        tool_name = event.tool_use["name"]
        arguments = event.tool_use.get("input", {})
        sim_time = None

        # Blank-user guard
        u = canonicalise(self._user)
        if not u:
            self._write_log(self._make_log_entry(
                tool=tool_name, arguments=arguments,
                decision="deny", rule="P7", reason="blank user",
            ))
            event.cancel_tool = "blank user"
            return

        # P7: allowed_tools check
        if tool_name not in self._allowed_tools:
            reason = f"tool {tool_name!r} not in allowed_tools"
            self._write_log(self._make_log_entry(
                tool=tool_name, arguments=arguments,
                decision="deny", rule="P7", reason=reason,
            ))
            event.cancel_tool = reason
            return

        # acts_for role check (skip if "any")
        if self._acts_for != "any":
            response = httpx.get(f"{self._base_url}/directory/users/{canonicalise(self._user)}")
            if response.status_code >= 500:
                response.raise_for_status()
            if response.status_code == 404:
                reason = "user not found in directory"
                self._write_log(self._make_log_entry(
                    tool=tool_name, arguments=arguments,
                    decision="deny", rule="P7", reason=reason,
                ))
                event.cancel_tool = reason
                return
            user_record = response.json()
            if user_record.get("role") != self._acts_for:
                reason = f"user role {user_record.get('role')!r} does not match acts_for {self._acts_for!r}"
                self._write_log(self._make_log_entry(
                    tool=tool_name, arguments=arguments,
                    decision="deny", rule="P7", reason=reason,
                ))
                event.cancel_tool = reason
                return

        # P6: fingerprint check
        if event.selected_tool is not None and tool_name in self._fingerprints:
            spec = event.selected_tool.tool_spec
            computed = hashlib.sha256(
                json.dumps(
                    {
                        "description": spec["description"],
                        "inputSchema": spec["inputSchema"],
                        "name": spec["name"],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            if computed != self._fingerprints[tool_name]:
                reason = f"fingerprint mismatch for tool {tool_name!r}"
                self._write_log(self._make_log_entry(
                    tool=tool_name, arguments=arguments,
                    decision="deny", rule="P6", reason=reason,
                ))
                event.cancel_tool = reason
                return

        # SCOPE: bearer token check
        if not self._bearer_token:
            reason = "missing bearer token"
            self._write_log(self._make_log_entry(
                tool=tool_name, arguments=arguments,
                decision="deny", rule="SCOPE", reason=reason,
            ))
            event.cancel_tool = reason
            return
        try:
            payload = jwt.decode(
                self._bearer_token,
                options={"verify_signature": False, "verify_exp": False, "verify_aud": False},
            )
        except jwt.exceptions.DecodeError:
            reason = "invalid bearer token"
            self._write_log(self._make_log_entry(
                tool=tool_name, arguments=arguments,
                decision="deny", rule="SCOPE", reason=reason,
            ))
            event.cancel_tool = reason
            return
        token_scopes = set(payload.get("scope", "").split())
        required = TOOL_SCOPE_MAP.get(tool_name, "")
        if required and required not in token_scopes:
            reason = f"missing scope {required!r}"
            self._write_log(self._make_log_entry(
                tool=tool_name, arguments=arguments,
                decision="deny", rule="SCOPE", reason=reason,
            ))
            event.cancel_tool = reason
            return

        # P1–P5: tool-specific rules
        if tool_name == "submit_expense":
            # P1: claimant must match acting user
            if canonicalise(arguments.get("claimant", "")) != canonicalise(self._user):
                reason = "claimant does not match acting user"
                self._write_log(self._make_log_entry(
                    tool=tool_name, arguments=arguments,
                    decision="deny", rule="P1", reason=reason, sim_time=sim_time,
                ))
                event.cancel_tool = reason
                return
            # P2: amount limit
            if arguments.get("amount", 0) > self._expense_limit and not arguments.get("approval_ref"):
                reason = f"amount {arguments.get('amount')} exceeds limit {self._expense_limit} without approval_ref"
                self._write_log(self._make_log_entry(
                    tool=tool_name, arguments=arguments,
                    decision="deny", rule="P2", reason=reason, sim_time=sim_time,
                ))
                event.cancel_tool = reason
                return

        elif tool_name == "approve_expense":
            # P3: no self-approval, approver must be claimant's manager
            response = httpx.get(f"{self._base_url}/directory/expenses/{arguments['expense_id']}")
            if response.status_code >= 500:
                response.raise_for_status()
            if response.status_code == 404:
                reason = "expense not found"
                self._write_log(self._make_log_entry(
                    tool=tool_name, arguments=arguments,
                    decision="deny", rule="P3", reason=reason, sim_time=sim_time,
                ))
                event.cancel_tool = reason
                return
            expense = response.json()
            sim_time = expense.get("sim_time", sim_time)

            response = httpx.get(f"{self._base_url}/directory/users/{expense['claimant']}")
            if response.status_code >= 500:
                response.raise_for_status()
            if response.status_code == 404:
                reason = "claimant user not found"
                self._write_log(self._make_log_entry(
                    tool=tool_name, arguments=arguments,
                    decision="deny", rule="P3", reason=reason, sim_time=sim_time,
                ))
                event.cancel_tool = reason
                return
            claimant_record = response.json()

            if canonicalise(self._user) == canonicalise(expense["claimant"]) or \
               canonicalise(self._user) != canonicalise(claimant_record.get("manager", "")):
                reason = "approver is claimant or not claimant's manager"
                self._write_log(self._make_log_entry(
                    tool=tool_name, arguments=arguments,
                    decision="deny", rule="P3", reason=reason, sim_time=sim_time,
                ))
                event.cancel_tool = reason
                return

        elif tool_name == "book_travel":
            # P4: delegation check (skip if booking for self)
            if canonicalise(arguments.get("traveller", "")) != canonicalise(self._user):
                response = httpx.get(f"{self._base_url}/directory/delegations")
                if response.status_code >= 500:
                    response.raise_for_status()
                data = response.json()
                sim_time = data.get("sim_time", sim_time)
                delegations = data.get("delegations", [])
                has_delegation = any(
                    d["active"] is True
                    and canonicalise(d["delegator"]) == canonicalise(arguments["traveller"])
                    and canonicalise(d["delegate"]) == canonicalise(self._user)
                    for d in delegations
                )
                if not has_delegation:
                    reason = f"no active delegation from {arguments.get('traveller')!r} to {self._user!r}"
                    self._write_log(self._make_log_entry(
                        tool=tool_name, arguments=arguments,
                        decision="deny", rule="P4", reason=reason, sim_time=sim_time,
                    ))
                    event.cancel_tool = reason
                    return

        elif tool_name == "pay_vendor":
            # P5: vendor must be in approved list
            response = httpx.get(f"{self._base_url}/directory/vendors")
            if response.status_code >= 500:
                response.raise_for_status()
            vendors = response.json()
            if canonicalise(arguments.get("vendor", "")) not in {canonicalise(v) for v in vendors}:
                reason = f"vendor {arguments.get('vendor')!r} not in approved list"
                self._write_log(self._make_log_entry(
                    tool=tool_name, arguments=arguments,
                    decision="deny", rule="P5", reason=reason, sim_time=sim_time,
                ))
                event.cancel_tool = reason
                return

        # All checks passed — allow
        self._write_log(self._make_log_entry(
            tool=tool_name, arguments=arguments,
            decision="allow", rule=None, reason="all checks passed", sim_time=sim_time,
        ))
