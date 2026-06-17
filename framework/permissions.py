"""Permission engine — enforces tool and capability access control.

The engine evaluates a PermissionSet against a requested operation and returns
allow/deny.  Designed for fail-closed behaviour: missing or malformed rules
deny by default.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re
from typing import Any

from framework.errors import PermissionDeniedError


@dataclass
class PermissionSet:
    """Describes what an agent is allowed to do.

    Attributes:
        allowed_tools: explicit allowlist of tool names (empty = all allowed).
        denied_tools: explicit denylist of tool names.
        scm: "read" | "read-write" | "none".
        filesystem: "workspace-only" | "full" | "none".
        custom: free-form dict for domain-specific rules.
    """

    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    scm: str = "read"
    filesystem: str = "workspace-only"
    custom: dict[str, Any] = field(default_factory=dict)
    agent_launching: bool = False              # Whether this agent can launch other agents
    allowed_agents: list[str] = field(default_factory=list)  # List of agent_ids that can be launched


class PermissionEngine:
    """Evaluates operations against a PermissionSet."""

    def __init__(self, permissions: PermissionSet | None = None) -> None:
        self._permissions = permissions or PermissionSet()

    @property
    def permissions(self) -> PermissionSet:
        return self._permissions

    def check_tool(self, tool_name: str) -> bool:
        """Return True if the tool is allowed."""
        if tool_name in self._permissions.denied_tools:
            return False
        if self._permissions.allowed_tools and tool_name not in self._permissions.allowed_tools:
            return False
        return True

    def require_tool(self, tool_name: str) -> None:
        """Raise PermissionDeniedError if the tool is not allowed."""
        if not self.check_tool(tool_name):
            raise PermissionDeniedError(f"Tool '{tool_name}' is not permitted")

    def check_command(self, command: str | list[str] | tuple[str, ...]) -> bool:
        """Return True if a run_command payload is allowed by command patterns.

        Command policy is intentionally stored under ``custom`` so permission
        YAML files stay easy to audit without expanding the top-level schema:

        ``allowed_command_patterns``
            Regex patterns for allowed command strings. Empty means commands
            are unrestricted for backward-compatible profiles.

        ``denied_command_patterns``
            Regex patterns that always deny, even when an allow pattern matches.
        """
        command_text = self._normalise_command(command)
        if not command_text:
            return False

        custom = self._permissions.custom or {}
        denied_patterns = self._pattern_list(
            custom.get("denied_command_patterns") or custom.get("denied_commands")
        )
        if any(self._pattern_matches(pattern, command_text, invalid_matches=True) for pattern in denied_patterns):
            return False

        allowed_patterns = self._pattern_list(
            custom.get("allowed_command_patterns") or custom.get("allowed_commands")
        )
        if not allowed_patterns:
            return True
        return any(self._pattern_matches(pattern, command_text) for pattern in allowed_patterns)

    def require_command(self, command: str | list[str] | tuple[str, ...]) -> None:
        """Raise PermissionDeniedError if a run_command payload is not allowed."""
        if not self.check_command(command):
            raise PermissionDeniedError(f"Command {self._normalise_command(command)!r} is not permitted")

    @staticmethod
    def _normalise_command(command: str | list[str] | tuple[str, ...]) -> str:
        if isinstance(command, (list, tuple)):
            return " ".join(str(part).strip() for part in command if str(part).strip()).strip()
        return str(command or "").strip()

    @staticmethod
    def _pattern_list(value: Any) -> list[str]:
        if isinstance(value, str):
            return [value]
        if isinstance(value, list):
            return [str(item) for item in value if str(item).strip()]
        return []

    @staticmethod
    def _pattern_matches(pattern: str, command_text: str, *, invalid_matches: bool = False) -> bool:
        try:
            return re.search(pattern, command_text) is not None
        except re.error:
            return invalid_matches

    def check_scm_write(self) -> bool:
        """Return True if SCM write operations are allowed."""
        return self._permissions.scm == "read-write"

    def require_scm_write(self) -> None:
        if not self.check_scm_write():
            raise PermissionDeniedError("SCM write operations are not permitted")

    def check_agent_launching(self, target_agent_id: str) -> bool:
        """Return True if this agent can launch target_agent_id."""
        if not self._permissions.agent_launching:
            return False
        allowed = self._permissions.allowed_agents
        if not allowed:
            return True  # No restriction list = can launch any
        return target_agent_id in allowed

    def require_agent_launching(self, target_agent_id: str) -> None:
        """Raise PermissionDeniedError if agent launching not permitted."""
        if not self.check_agent_launching(target_agent_id):
            raise PermissionDeniedError(
                f"Agent launching '{target_agent_id}' is not permitted"
            )

    @classmethod
    def from_dict(cls, data: dict) -> PermissionEngine:
        """Build a PermissionEngine from a raw config dict."""
        ps = PermissionSet(
            allowed_tools=data.get("allowed_tools", []),
            denied_tools=data.get("denied_tools", []),
            scm=data.get("scm", "read"),
            filesystem=data.get("filesystem", "workspace-only"),
            custom=data.get("custom", {}),
            agent_launching=data.get("agent_launching", False),
            allowed_agents=data.get("allowed_agents", []),
        )
        return cls(ps)
    @classmethod
    def from_yaml(cls, path: str) -> "PermissionEngine":
        """Load a PermissionEngine from a YAML config file.

        The YAML schema mirrors ``PermissionSet``:

        .. code-block:: yaml

            allowed_tools: [read_file, write_file, ...]
            denied_tools: []
            scm: read-write
            filesystem: workspace-only
            agent_launching: true
            allowed_agents: [web_dev, code_review]
            custom:
              protected_branch_patterns: ["^main$", "^master$"]
        """
        import yaml  # type: ignore[import-untyped]

        with open(path, encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return cls.from_dict(data)
