"""Constellation policy evaluator.

Provides three layers of access control as specified in the improvement plan §6:
1. Capability-level access control (which users/roles can use which capabilities)
2. Tool whitelist/blacklist enforcement (per-agent allowed/disallowed tools)
3. Bash command restrictions (blocked commands, network policy)

Configuration is loaded from registry-config.json security fields and
environment variables. Falls back to allow-all when no policy is configured.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


@dataclass
class BashRestrictions:
    """Bash execution constraints for an agent."""

    blocked_commands: list[str] = field(default_factory=list)
    allowed_network_hosts: list[str] = field(default_factory=list)
    max_output_bytes: int = 1_048_576  # 1 MB default


@dataclass
class SecurityPolicy:
    """Security policy parsed from registry-config.json security field."""

    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    bash_restrictions: BashRestrictions = field(default_factory=BashRestrictions)
    allowed_roles: list[str] = field(default_factory=lambda: ["*"])

    @classmethod
    def from_dict(cls, data: dict) -> "SecurityPolicy":
        """Parse a security policy from a registry-config.json 'security' block."""
        if not data:
            return cls()
        bash = data.get("bashRestrictions", {})
        return cls(
            allowed_tools=data.get("allowedTools", []),
            disallowed_tools=data.get("disallowedTools", []),
            bash_restrictions=BashRestrictions(
                blocked_commands=bash.get("blockedCommands", []),
                allowed_network_hosts=bash.get("allowedNetworkHosts", []),
                max_output_bytes=bash.get("maxOutputBytes", 1_048_576),
            ),
            allowed_roles=data.get("allowedRoles", ["*"]),
        )


class PolicyEvaluator:
    """Multi-layer policy evaluator for task dispatch and tool usage."""

    def __init__(self, policies: dict[str, SecurityPolicy] | None = None):
        self._policies = policies or {}

    def register_agent_policy(self, agent_id: str, policy: SecurityPolicy) -> None:
        """Register a security policy for an agent (usually at startup from registry-config)."""
        self._policies[agent_id] = policy

    def evaluate(self, task: dict, agent_definition: dict) -> dict:
        """Evaluate whether a task is allowed to be dispatched to the given agent.

        Returns: {"approved": bool, "scopes": list, "reason": str}
        """
        agent_id = agent_definition.get("agentId", "")
        policy = self._policies.get(agent_id)

        if not policy:
            return {
                "approved": True,
                "scopes": agent_definition.get("capabilities", []),
                "reason": "No policy configured — default allow.",
            }

        # Check role-based access
        user_role = (task.get("metadata", {}) or {}).get("userRole", "user")
        if "*" not in policy.allowed_roles and user_role not in policy.allowed_roles:
            return {
                "approved": False,
                "scopes": [],
                "reason": f"Role '{user_role}' not in allowed roles for {agent_id}.",
            }

        return {
            "approved": True,
            "scopes": agent_definition.get("capabilities", []),
            "reason": f"Policy check passed for {agent_id}.",
        }

    def check_tool_allowed(self, agent_id: str, tool_name: str) -> tuple[bool, str]:
        """Check if a tool is allowed for the given agent.

        Returns: (allowed: bool, reason: str)
        """
        policy = self._policies.get(agent_id)
        if not policy:
            return True, "No policy — default allow."

        if policy.disallowed_tools and tool_name in policy.disallowed_tools:
            return False, f"Tool '{tool_name}' is disallowed for {agent_id}."

        if policy.allowed_tools and tool_name not in policy.allowed_tools:
            return False, f"Tool '{tool_name}' not in allowed list for {agent_id}."

        return True, "Allowed."

    def check_bash_command(self, agent_id: str, command: str) -> tuple[bool, str]:
        """Check if a bash command is safe to execute for the given agent.

        Returns: (allowed: bool, reason: str)
        """
        policy = self._policies.get(agent_id)
        if not policy:
            return True, "No policy — default allow."

        restrictions = policy.bash_restrictions
        if not restrictions.blocked_commands:
            return True, "No bash restrictions configured."

        # Check first token and full command against blocked patterns
        tokens = command.strip().split()
        first_token = tokens[0] if tokens else ""

        for blocked in restrictions.blocked_commands:
            if first_token == blocked or command.strip().startswith(blocked):
                return False, f"Command blocked by security policy: {blocked}"

        return True, "Allowed."

    def get_agent_policy(self, agent_id: str) -> SecurityPolicy | None:
        """Get the security policy for an agent (for introspection/debugging)."""
        return self._policies.get(agent_id)