import os
import json
import uuid
import datetime
import hashlib
import pathlib

import yaml
import httpx
import jwt

from strands.hooks import HookProvider, HookRegistry, BeforeToolCallEvent

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
        yaml_path = config_dir / f"{agent_name}.yaml"
        if not yaml_path.exists():
            raise FileNotFoundError(f"Agent config not found: {yaml_path}")
        with open(yaml_path, encoding="utf-8") as f:
            config = yaml.safe_load(f)
        if config is None or not isinstance(config, dict):
            raise ValueError(f"Invalid YAML config at {yaml_path}: parsed to {type(config)}")

        self._agent_name = agent_name
        self._user = user
        self._base_url = base_url
        self._run_id = run_id
        self._bearer_token = bearer_token
        self._allowed_tools: list[str] = config.get("allowed_tools", [])
        self._fingerprints: dict[str, str] = config.get("fingerprints", {})
        self._acts_for: str = config.get("acts_for", "any")
        if limit_override is not None:
            self._expense_limit = limit_override
        else:
            self._expense_limit = config.get("expense_limit")

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)

    def _http_get(self, url: str):
        response = httpx.get(url)
        if response.is_server_error:
            response.raise_for_status()
        return response

    def _log_entry(
        self,
        call_id: str,
        tool_name: str,
        args: dict,
        decision: str,
        rule: str | None,
        reason: str,
        sim_time: float | None,
    ) -> None:
        entry = {
            "call_id": call_id,
            "run_id": self._run_id,
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "sim_time": sim_time,
            "user": self._user,
            "agent": self._agent_name,
            "tool": tool_name,
            "arguments": args,
            "decision": decision,
            "rule": rule,
            "reason": reason,
        }
        with open(os.environ["HOOK_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _before_tool_call(self, event) -> None:
        call_id = str(uuid.uuid4())
        tool_name = event.tool_use["name"]
        args = event.tool_use["input"]
        sim_time = None

        # 1. blank-user check → P7
        if canonicalise(self._user) == "":
            self._log_entry(call_id, tool_name, args, "deny", "P7", "blank user", sim_time)
            event.cancel_tool = "P7: blank user"
            return

        # 2. allowed_tools check → P7
        if tool_name not in self._allowed_tools:
            self._log_entry(call_id, tool_name, args, "deny", "P7", f"tool '{tool_name}' not in allowed_tools", sim_time)
            event.cancel_tool = f"P7: tool '{tool_name}' not in allowed_tools"
            return

        # 3. acts_for role check → P7
        if self._acts_for != "any":
            resp = self._http_get(f"{self._base_url}/directory/users/{self._user}")
            if resp.status_code == 404:
                self._log_entry(call_id, tool_name, args, "deny", "P7", "user not found in directory", sim_time)
                event.cancel_tool = "P7: user not found in directory"
                return
            if resp.json()["role"] != self._acts_for:
                self._log_entry(call_id, tool_name, args, "deny", "P7", f"user role does not match acts_for '{self._acts_for}'", sim_time)
                event.cancel_tool = f"P7: user role does not match acts_for"
                return

        # 4. P6 fingerprint check
        if event.selected_tool is not None and tool_name in self._fingerprints:
            spec = event.selected_tool.tool_spec
            digest = hashlib.sha256(
                (
                    spec["name"]
                    + spec["description"]
                    + json.dumps(spec["inputSchema"], sort_keys=True, separators=(",", ":"))
                ).encode()
            ).hexdigest()
            if digest != self._fingerprints[tool_name]:
                self._log_entry(call_id, tool_name, args, "deny", "P6", "tool fingerprint mismatch", sim_time)
                event.cancel_tool = "P6: tool fingerprint mismatch"
                return

        # 5. SCOPE check
        if self._bearer_token == "":
            self._log_entry(call_id, tool_name, args, "deny", "SCOPE", "missing bearer token", sim_time)
            event.cancel_tool = "SCOPE: missing bearer token"
            return
        try:
            claims = jwt.decode(
                self._bearer_token,
                options={"verify_signature": False},
                algorithms=["HS256", "RS256", "ES256"],
            )
        except (jwt.DecodeError, jwt.InvalidTokenError):
            self._log_entry(call_id, tool_name, args, "deny", "SCOPE", "invalid JWT", sim_time)
            event.cancel_tool = "SCOPE: invalid JWT"
            return
        required_scope = TOOL_SCOPE_MAP.get(tool_name)
        if required_scope and required_scope not in claims.get("scope", "").split():
            self._log_entry(call_id, tool_name, args, "deny", "SCOPE", f"token missing required scope '{required_scope}'", sim_time)
            event.cancel_tool = f"SCOPE: token missing required scope '{required_scope}'"
            return

        # 6. P1–P5 business rules
        if tool_name == "submit_expense":
            # P1: own expenses only
            if canonicalise(args.get("claimant", "")) != canonicalise(self._user):
                self._log_entry(call_id, tool_name, args, "deny", "P1", f"claimant does not match acting user", sim_time)
                event.cancel_tool = "P1: claimant does not match acting user"
                return
            # P2: expense limit
            if (
                self._expense_limit is not None
                and args.get("amount", 0) > self._expense_limit
                and not args.get("approval_ref")
            ):
                self._log_entry(call_id, tool_name, args, "deny", "P2", f"amount exceeds expense limit without approval_ref", sim_time)
                event.cancel_tool = "P2: amount exceeds expense limit without approval_ref"
                return

        elif tool_name == "approve_expense":
            # P3: no self-approval, must be claimant's manager
            resp = self._http_get(f"{self._base_url}/directory/expenses/{args['expense_id']}")
            if resp.status_code == 404:
                self._log_entry(call_id, tool_name, args, "deny", "P3", "expense not found", sim_time)
                event.cancel_tool = "P3: expense not found"
                return
            expense_data = resp.json()
            raw_claimant = expense_data["claimant"]
            if canonicalise(self._user) == canonicalise(raw_claimant):
                self._log_entry(call_id, tool_name, args, "deny", "P3", "self-approval not allowed", sim_time)
                event.cancel_tool = "P3: self-approval not allowed"
                return
            resp2 = self._http_get(f"{self._base_url}/directory/users/{raw_claimant}")
            if resp2.json().get("manager") != self._user:
                self._log_entry(call_id, tool_name, args, "deny", "P3", "not claimant's manager", sim_time)
                event.cancel_tool = "P3: not claimant's manager"
                return

        elif tool_name == "book_travel":
            # P4: delegation required when booking for another person
            traveller = args.get("traveller", "")
            if canonicalise(traveller) != canonicalise(self._user):
                resp = self._http_get(f"{self._base_url}/directory/delegations")
                body = resp.json()
                sim_time = body["sim_time"]
                delegations = body["delegations"]
                has_delegation = any(
                    entry["delegator"] == canonicalise(traveller)
                    and entry["delegate"] == canonicalise(self._user)
                    and entry["active"] is True
                    for entry in delegations
                )
                if not has_delegation:
                    self._log_entry(call_id, tool_name, args, "deny", "P4", f"no active delegation from '{traveller}' to acting user", sim_time)
                    event.cancel_tool = "P4: no active delegation"
                    return

        elif tool_name == "pay_vendor":
            # P5: vendor must be in approved list
            resp = self._http_get(f"{self._base_url}/directory/vendors")
            vendor_list = resp.json()
            if canonicalise(args.get("vendor", "")) not in [canonicalise(v) for v in vendor_list]:
                self._log_entry(call_id, tool_name, args, "deny", "P5", f"vendor not in approved vendor list", sim_time)
                event.cancel_tool = "P5: vendor not in approved vendor list"
                return

        # 7. allow
        self._log_entry(call_id, tool_name, args, "allow", None, "all checks passed", sim_time)
