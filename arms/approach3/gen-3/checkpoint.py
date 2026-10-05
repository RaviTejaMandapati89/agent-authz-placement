import hashlib
import json
import os
import pathlib
import uuid
from datetime import datetime, timezone

import httpx
import jwt
import yaml
from strands.hooks import BeforeToolCallEvent

from domain.identity import canonicalise
from domain.scopes import TOOL_SCOPE_MAP


class PolicyHook:
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
        self._agent_name = agent_name
        self._user = user
        self._base_url = base_url
        self._run_id = run_id
        self._bearer_token = bearer_token
        config_path = (
            config_dir or pathlib.Path(__file__).parent / "config" / "agents"
        ) / f"{agent_name}.yaml"
        with open(config_path, encoding="utf-8") as f:
            self._config = yaml.safe_load(f)
        self._http = httpx.Client()

    def register_hooks(self, registry) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before_tool_call)

    def _log(
        self,
        tool: str,
        arguments: dict,
        decision: str,
        rule: str | None,
        reason: str,
        sim_time: float | None = None,
    ) -> None:
        entry = {
            "call_id": str(uuid.uuid4()),
            "run_id": self._run_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "sim_time": sim_time,
            "user": self._user,
            "agent": self._agent_name,
            "tool": tool,
            "arguments": arguments,
            "decision": decision,
            "rule": rule,
            "reason": reason,
        }
        with open(os.environ["HOOK_LOG"], "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")

    def _before_tool_call(self, event) -> None:
        tool = event.tool_use["name"]
        args = event.tool_use["input"]
        sim_time = None

        try:
            # Step 1: blank-user guard (P7)
            if canonicalise(self._user) == "":
                self._log(tool, args, "deny", "P7", "blank user")
                event.cancel_tool = "blank user"
                return

            # Step 2: allowed_tools check (P7)
            if tool not in self._config["allowed_tools"]:
                reason = f"tool '{tool}' not in allowed_tools"
                self._log(tool, args, "deny", "P7", reason)
                event.cancel_tool = reason
                return

            # Step 3: acts_for role check (P7)
            if self._config["acts_for"] != "any":
                response = self._http.get(
                    f"{self._base_url}/directory/users/{canonicalise(self._user)}"
                )
                if response.status_code == 404:
                    self._log(tool, args, "deny", "P7", "user not found")
                    event.cancel_tool = "user not found"
                    return
                else:
                    response.raise_for_status()
                if response.json()["role"] != self._config["acts_for"]:
                    self._log(tool, args, "deny", "P7", "role mismatch")
                    event.cancel_tool = "role mismatch"
                    return

            # Step 4: P6 fingerprint check
            if (
                event.selected_tool is not None
                and tool in self._config.get("fingerprints", {})
            ):
                spec = event.selected_tool.tool_spec
                digest = hashlib.sha256(
                    json.dumps(
                        {
                            "name": spec["name"],
                            "description": spec["description"],
                            "inputSchema": spec["inputSchema"],
                        },
                        sort_keys=True,
                    ).encode("utf-8")
                ).hexdigest()
                if digest != self._config["fingerprints"][tool]:
                    self._log(tool, args, "deny", "P6", "fingerprint mismatch")
                    event.cancel_tool = "fingerprint mismatch"
                    return

            # Step 5: SCOPE check
            try:
                payload = jwt.decode(
                    self._bearer_token,
                    options={"verify_signature": False, "verify_exp": False},
                )
            except Exception:
                self._log(tool, args, "deny", "SCOPE", "bearer token missing or invalid")
                event.cancel_tool = "bearer token missing or invalid"
                return

            scopes = set(payload.get("scope", "").split())
            required_scope = TOOL_SCOPE_MAP.get(tool)
            if required_scope is not None and required_scope not in scopes:
                self._log(tool, args, "deny", "SCOPE", "missing required scope")
                event.cancel_tool = "missing required scope"
                return

            # Step 6: Business rules P1-P5
            if tool == "submit_expense":
                # P1: claimant must match acting user
                if canonicalise(args.get("claimant", "")) != canonicalise(self._user):
                    self._log(tool, args, "deny", "P1", "claimant does not match acting user")
                    event.cancel_tool = "claimant does not match acting user"
                    return

                # P2: amount exceeds limit without approval_ref
                if (
                    self._config.get("expense_limit") is not None
                    and args["amount"] > self._config["expense_limit"]
                    and not args.get("approval_ref")
                ):
                    self._log(tool, args, "deny", "P2", "amount exceeds limit without approval_ref")
                    event.cancel_tool = "amount exceeds limit without approval_ref"
                    return

            elif tool == "approve_expense":
                # P3: no self-approval; approver must be claimant's manager
                response = self._http.get(
                    f"{self._base_url}/directory/expenses/{args['expense_id']}"
                )
                if response.status_code == 404:
                    self._log(tool, args, "deny", "P3", "expense not found")
                    event.cancel_tool = "expense not found"
                    return
                else:
                    response.raise_for_status()

                expense = response.json()
                if expense["claimant"] == canonicalise(self._user):
                    self._log(tool, args, "deny", "P3", "self-approval not permitted")
                    event.cancel_tool = "self-approval not permitted"
                    return

                response = self._http.get(
                    f"{self._base_url}/directory/users/{expense['claimant']}"
                )
                if response.status_code == 404:
                    self._log(tool, args, "deny", "P3", "claimant not found")
                    event.cancel_tool = "claimant not found"
                    return
                else:
                    response.raise_for_status()

                user_rec = response.json()
                if user_rec.get("manager") != self._user:
                    self._log(tool, args, "deny", "P3", "approver is not claimant's manager")
                    event.cancel_tool = "approver is not claimant's manager"
                    return

            elif tool == "book_travel":
                # P4: live travel delegation check
                if canonicalise(args["traveller"]) != canonicalise(self._user):
                    response = self._http.get(f"{self._base_url}/directory/delegations")
                    if response.status_code != 200:
                        response.raise_for_status()
                    data = response.json()
                    sim_time = data["sim_time"]
                    has_delegation = any(
                        d["delegator"] == canonicalise(args["traveller"])
                        and d["delegate"] == canonicalise(self._user)
                        and d["active"] is True
                        and "travel" in d["scope"]
                        for d in data["delegations"]
                    )
                    if not has_delegation:
                        reason = (
                            f"no active travel delegation from '{args['traveller']}'"
                            f" to '{self._user}'"
                        )
                        self._log(tool, args, "deny", "P4", reason, sim_time)
                        event.cancel_tool = reason
                        return

            elif tool == "pay_vendor":
                # P5: approved vendor list
                response = self._http.get(f"{self._base_url}/directory/vendors")
                if response.status_code != 200:
                    response.raise_for_status()
                vendors = response.json()
                if canonicalise(args["vendor"]) not in vendors:
                    reason = f"vendor '{args['vendor']}' not in approved vendor list"
                    self._log(tool, args, "deny", "P5", reason)
                    event.cancel_tool = reason
                    return

            # All checks passed
            self._log(tool, args, "allow", None, "allow", sim_time)

        except Exception as exc:
            self._log(tool, args, "error", None, str(exc))
            raise

    def __del__(self):
        try:
            self._http.close()
        except Exception:
            pass
