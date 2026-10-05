import datetime
import hashlib
import json
import os
import pathlib
import uuid

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
    ) -> None:
        if config_dir is None:
            config_dir = pathlib.Path(__file__).parent / "config" / "agents"

        with open(config_dir / f"{agent_name}.yaml", encoding="utf-8") as f:
            cfg = yaml.safe_load(f)

        self.agent_name = agent_name
        self.user = canonicalise(user)
        self.base_url = base_url
        self.run_id = run_id
        self.bearer_token = bearer_token

        self._allowed_tools: list[str] = cfg["allowed_tools"]
        self._fingerprints: dict[str, str] = cfg.get("fingerprints", {})
        self._acts_for: str = cfg["acts_for"]
        self._expense_limit: int | None = (
            limit_override if limit_override is not None else cfg.get("expense_limit")
        )

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)

    def _fingerprint(self, tool) -> str:
        spec = tool.tool_spec
        return hashlib.sha256(
            json.dumps(
                {
                    "name": spec["name"],
                    "description": spec["description"],
                    "inputSchema": spec["inputSchema"],
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def _write_log(
        self,
        tool: str,
        arguments: dict,
        sim_time: float | None,
        decision: str,
        rule: str | None,
        reason: str,
    ) -> None:
        entry = (
            json.dumps(
                {
                    "call_id": str(uuid.uuid4()),
                    "run_id": self.run_id,
                    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "sim_time": sim_time,
                    "user": self.user,
                    "agent": self.agent_name,
                    "tool": tool,
                    "arguments": arguments,
                    "decision": decision,
                    "rule": rule,
                    "reason": reason,
                }
            )
            + "\n"
        )
        with open(os.environ["HOOK_LOG"], "a", encoding="utf-8") as f:
            f.write(entry)

    def _before_tool_call(self, event) -> None:
        tool_name: str = ""
        arguments: dict = {}
        sim_time: float | None = None

        try:
            tool_name = event.tool_use["name"]
            arguments = event.tool_use.get("input", {})
            selected_tool = event.selected_tool

            # Check 1: blank user guard (P7)
            if not self.user:
                self._write_log(tool_name, arguments, None, "deny", "P7", "blank user")
                event.cancel_tool = "blank user"
                return

            # Check 2: allowed-tools check (P7)
            if tool_name not in self._allowed_tools:
                self._write_log(
                    tool_name,
                    arguments,
                    None,
                    "deny",
                    "P7",
                    f"tool '{tool_name}' not in allowed_tools",
                )
                event.cancel_tool = f"tool '{tool_name}' not in allowed_tools"
                return

            # Check 3: role check (P7)
            if self._acts_for != "any":
                resp = httpx.get(f"{self.base_url}/directory/users/{self.user}")
                if resp.status_code == 404:
                    self._write_log(
                        tool_name, arguments, None, "deny", "P7", "user not in directory"
                    )
                    event.cancel_tool = "user not in directory"
                    return
                resp.raise_for_status()
                role = resp.json()["role"]
                if role != self._acts_for:
                    self._write_log(
                        tool_name,
                        arguments,
                        None,
                        "deny",
                        "P7",
                        f"role '{role}' does not match required '{self._acts_for}'",
                    )
                    event.cancel_tool = (
                        f"role '{role}' does not match required '{self._acts_for}'"
                    )
                    return

            # Check 4: fingerprint check (P6)
            if selected_tool is not None and tool_name in self._fingerprints:
                actual = self._fingerprint(selected_tool)
                if actual != self._fingerprints[tool_name]:
                    self._write_log(
                        tool_name,
                        arguments,
                        None,
                        "deny",
                        "P6",
                        f"fingerprint mismatch for '{tool_name}'",
                    )
                    event.cancel_tool = f"fingerprint mismatch for '{tool_name}'"
                    return

            # Check 5: SCOPE check
            if not self.bearer_token:
                self._write_log(
                    tool_name, arguments, None, "deny", "SCOPE", "missing bearer token"
                )
                event.cancel_tool = "missing bearer token"
                return
            try:
                claims = jwt.decode(self.bearer_token, options={"verify_signature": False})
            except Exception:
                self._write_log(
                    tool_name, arguments, None, "deny", "SCOPE", "invalid bearer token"
                )
                event.cancel_tool = "invalid bearer token"
                return
            required_scope = TOOL_SCOPE_MAP.get(tool_name)
            if required_scope and required_scope not in claims.get("scope", "").split():
                self._write_log(
                    tool_name,
                    arguments,
                    None,
                    "deny",
                    "SCOPE",
                    f"token missing scope '{required_scope}'",
                )
                event.cancel_tool = f"token missing scope '{required_scope}'"
                return

            # Check 6: business rules
            if tool_name == "submit_expense":
                # P1: claimant must match acting user
                claimant = canonicalise(arguments.get("claimant", ""))
                if claimant != self.user:
                    self._write_log(
                        tool_name,
                        arguments,
                        sim_time,
                        "deny",
                        "P1",
                        f"claimant '{claimant}' does not match acting user '{self.user}'",
                    )
                    event.cancel_tool = (
                        f"claimant '{claimant}' does not match acting user '{self.user}'"
                    )
                    return
                # P2: expense limit check
                if (
                    self._expense_limit is not None
                    and arguments.get("amount", 0) > self._expense_limit
                    and not arguments.get("approval_ref")
                ):
                    self._write_log(
                        tool_name,
                        arguments,
                        sim_time,
                        "deny",
                        "P2",
                        f"amount {arguments['amount']} exceeds limit {self._expense_limit} without approval_ref",
                    )
                    event.cancel_tool = f"amount {arguments['amount']} exceeds limit {self._expense_limit} without approval_ref"
                    return

            elif tool_name == "approve_expense":
                # P3: no self-approval; acting user must be claimant's manager
                resp = httpx.get(
                    f"{self.base_url}/directory/expenses/{arguments['expense_id']}"
                )
                if resp.status_code == 404:
                    self._write_log(
                        tool_name, arguments, sim_time, "deny", "P3", "expense not found"
                    )
                    event.cancel_tool = "expense not found"
                    return
                resp.raise_for_status()
                claimant = resp.json()["claimant"]
                if claimant == self.user:
                    self._write_log(
                        tool_name,
                        arguments,
                        sim_time,
                        "deny",
                        "P3",
                        "self-approval: acting user is the expense claimant",
                    )
                    event.cancel_tool = "self-approval: acting user is the expense claimant"
                    return
                resp2 = httpx.get(f"{self.base_url}/directory/users/{claimant}")
                if resp2.status_code == 404:
                    self._write_log(
                        tool_name,
                        arguments,
                        sim_time,
                        "deny",
                        "P3",
                        "claimant not in directory",
                    )
                    event.cancel_tool = "claimant not in directory"
                    return
                resp2.raise_for_status()
                if resp2.json()["manager"] != self.user:
                    self._write_log(
                        tool_name,
                        arguments,
                        sim_time,
                        "deny",
                        "P3",
                        f"acting user '{self.user}' is not manager of '{claimant}'",
                    )
                    event.cancel_tool = (
                        f"acting user '{self.user}' is not manager of '{claimant}'"
                    )
                    return

            elif tool_name == "book_travel":
                # P4: traveller must be acting user or have active delegation to acting user
                traveller = canonicalise(arguments.get("traveller", ""))
                if traveller != self.user:
                    resp = httpx.get(f"{self.base_url}/directory/delegations")
                    resp.raise_for_status()
                    data = resp.json()
                    sim_time = data.get("sim_time")
                    if not any(
                        d.get("from") == traveller
                        and d.get("to") == self.user
                        and d.get("active")
                        for d in data.get("delegations", [])
                    ):
                        self._write_log(
                            tool_name,
                            arguments,
                            sim_time,
                            "deny",
                            "P4",
                            f"no active delegation from '{traveller}' to '{self.user}'",
                        )
                        event.cancel_tool = (
                            f"no active delegation from '{traveller}' to '{self.user}'"
                        )
                        return

            elif tool_name == "pay_vendor":
                # P5: vendor must be in approved vendor list
                resp = httpx.get(f"{self.base_url}/directory/vendors")
                resp.raise_for_status()
                vendor = canonicalise(arguments.get("vendor", ""))
                if vendor not in resp.json().get("vendors", []):
                    self._write_log(
                        tool_name,
                        arguments,
                        sim_time,
                        "deny",
                        "P5",
                        f"vendor '{vendor}' not in approved vendor list",
                    )
                    event.cancel_tool = f"vendor '{vendor}' not in approved vendor list"
                    return

            # All checks passed — write allow log entry
            self._write_log(tool_name, arguments, sim_time, "allow", None, "all checks passed")

        except Exception as e:
            self._write_log(tool_name, arguments, sim_time, "error", None, str(e))
            raise
