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
    ) -> None:
        self.agent_name = agent_name
        self.user = user
        self.run_id = run_id
        self.bearer_token = bearer_token

        if config_dir is None:
            config_dir = pathlib.Path(__file__).parent / "config" / "agents"

        config_path = config_dir / f"{agent_name}.yaml"
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)

        self.allowed_tools: list[str] = config["allowed_tools"]
        self.fingerprints: dict[str, str] = config["fingerprints"]
        self.acts_for: str = config["acts_for"]
        self.expense_limit: int | None = config.get("expense_limit")

        if limit_override is not None:
            self.expense_limit = limit_override

        self._client = httpx.Client(base_url=base_url)

    def register_hooks(self, registry: HookRegistry, **kwargs) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)

    def _write_log(
        self,
        call_id: str,
        sim_time,
        tool: str,
        arguments: dict,
        decision: str,
        rule,
        reason: str,
    ) -> None:
        log_path = os.environ.get("HOOK_LOG")
        if not log_path:
            raise RuntimeError("HOOK_LOG environment variable is not set")
        entry = {
            "call_id": call_id,
            "run_id": self.run_id,
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "sim_time": sim_time,
            "user": self.user,
            "agent": self.agent_name,
            "tool": tool,
            "arguments": arguments,
            "decision": decision,
            "rule": rule,
            "reason": reason,
        }
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def _before_tool_call(self, event: BeforeToolCallEvent) -> None:
        tool_name = event.tool_use["name"]
        args = event.tool_use["input"]
        sim_time = None
        call_id = str(uuid.uuid4())

        # Step 1: blank-user guard
        if not canonicalise(self.user):
            self._write_log(call_id, sim_time, tool_name, args, "deny", "P7", "blank user")
            event.cancel_tool = "P7: blank user"
            return

        # Step 2: allowed_tools check
        if tool_name not in self.allowed_tools:
            self._write_log(call_id, sim_time, tool_name, args, "deny", "P7", f"tool {tool_name!r} not in allowed_tools")
            event.cancel_tool = f"P7: tool {tool_name!r} not in allowed_tools"
            return

        # Step 3: acts_for role check
        if self.acts_for != "any":
            response = self._client.get(f"/directory/users/{self.user}")
            if response.status_code == 404:
                self._write_log(call_id, sim_time, tool_name, args, "deny", "P7", "user not found")
                event.cancel_tool = "P7: user not found"
                return
            response.raise_for_status()
            if response.json()["role"] != self.acts_for:
                self._write_log(call_id, sim_time, tool_name, args, "deny", "P7", "role mismatch")
                event.cancel_tool = "P7: role mismatch"
                return

        # Step 4: fingerprint check
        if event.selected_tool is not None and tool_name in self.fingerprints:
            spec = event.selected_tool.tool_spec
            digest = hashlib.sha256(
                json.dumps(
                    {
                        "description": spec["description"],
                        "inputSchema": spec["inputSchema"],
                        "name": spec["name"],
                    },
                    sort_keys=True,
                ).encode()
            ).hexdigest()
            if digest != self.fingerprints[tool_name]:
                self._write_log(call_id, sim_time, tool_name, args, "deny", "P6", "fingerprint mismatch")
                event.cancel_tool = "P6: fingerprint mismatch"
                return

        # Step 5: SCOPE bearer-token check
        if not self.bearer_token:
            self._write_log(call_id, sim_time, tool_name, args, "deny", "SCOPE", "missing token")
            event.cancel_tool = "SCOPE: missing token"
            return
        try:
            decoded = jwt.decode(self.bearer_token, options={"verify_signature": False})
        except jwt.DecodeError:
            self._write_log(call_id, sim_time, tool_name, args, "deny", "SCOPE", "malformed token")
            event.cancel_tool = "SCOPE: malformed token"
            return
        required_scope = TOOL_SCOPE_MAP.get(tool_name, "")
        token_scopes = decoded.get("scope", "").split()
        if required_scope and required_scope not in token_scopes:
            self._write_log(call_id, sim_time, tool_name, args, "deny", "SCOPE", "insufficient scope")
            event.cancel_tool = "SCOPE: insufficient scope"
            return

        # Step 6: business rule dispatch
        if tool_name == "submit_expense":
            # P1: claimant must match acting user
            if canonicalise(args.get("claimant", "")) != canonicalise(self.user):
                self._write_log(call_id, sim_time, tool_name, args, "deny", "P1", "claimant does not match acting user")
                event.cancel_tool = "P1: claimant does not match acting user"
                return
            # P2: amount check
            if (
                self.expense_limit is not None
                and args.get("amount", 0) > self.expense_limit
                and not args.get("approval_ref")
            ):
                self._write_log(call_id, sim_time, tool_name, args, "deny", "P2", "amount exceeds limit without approval reference")
                event.cancel_tool = "P2: amount exceeds limit without approval reference"
                return
            self._write_log(call_id, sim_time, tool_name, args, "allow", None, "all checks passed")

        elif tool_name == "approve_expense":
            response = self._client.get(f"/directory/expenses/{args['expense_id']}")
            if response.status_code == 404:
                self._write_log(call_id, sim_time, tool_name, args, "deny", "P3", "expense not found")
                event.cancel_tool = "P3: expense not found"
                return
            response.raise_for_status()
            claimant = canonicalise(response.json()["claimant"])
            if claimant == canonicalise(self.user):
                self._write_log(call_id, sim_time, tool_name, args, "deny", "P3", "approver is the claimant")
                event.cancel_tool = "P3: approver is the claimant"
                return
            user_response = self._client.get(f"/directory/users/{claimant}")
            if user_response.status_code == 404:
                self._write_log(call_id, sim_time, tool_name, args, "deny", "P3", "claimant not found in directory")
                event.cancel_tool = "P3: claimant not found in directory"
                return
            user_response.raise_for_status()
            if user_response.json()["manager"] != self.user:
                self._write_log(call_id, sim_time, tool_name, args, "deny", "P3", "approver is not claimant's manager")
                event.cancel_tool = "P3: approver is not claimant's manager"
                return
            self._write_log(call_id, sim_time, tool_name, args, "allow", None, "all checks passed")

        elif tool_name == "book_travel":
            traveller = canonicalise(args.get("traveller", ""))
            if traveller != canonicalise(self.user):
                response = self._client.get("/directory/delegations")
                if response.status_code == 404:
                    self._write_log(call_id, sim_time, tool_name, args, "deny", "P4", "no active travel delegation")
                    event.cancel_tool = "P4: no active travel delegation"
                    return
                response.raise_for_status()
                data = response.json()
                sim_time = data.get("sim_time")
                delegations = data.get("delegations", [])
                found = any(
                    d.get("active") is True
                    and canonicalise(d.get("delegator", "")) == traveller
                    and canonicalise(d.get("delegate", "")) == canonicalise(self.user)
                    and "travel" in d.get("scope", [])
                    for d in delegations
                )
                if not found:
                    self._write_log(call_id, sim_time, tool_name, args, "deny", "P4", "no active travel delegation")
                    event.cancel_tool = "P4: no active travel delegation"
                    return
            self._write_log(call_id, sim_time, tool_name, args, "allow", None, "all checks passed")

        elif tool_name == "pay_vendor":
            response = self._client.get("/directory/vendors")
            if response.status_code == 404:
                self._write_log(call_id, sim_time, tool_name, args, "deny", "P5", "vendor not in approved list")
                event.cancel_tool = "P5: vendor not in approved list"
                return
            response.raise_for_status()
            approved_vendors = [canonicalise(v) for v in response.json()]
            if canonicalise(args.get("vendor", "")) not in approved_vendors:
                self._write_log(call_id, sim_time, tool_name, args, "deny", "P5", "vendor not in approved list")
                event.cancel_tool = "P5: vendor not in approved list"
                return
            self._write_log(call_id, sim_time, tool_name, args, "allow", None, "all checks passed")

        else:
            self._write_log(call_id, sim_time, tool_name, args, "allow", None, "all checks passed")
