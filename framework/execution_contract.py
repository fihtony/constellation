"""Execution Contract — standardized parent-to-child permission/tool handoff.

This module defines the ExecutionContract structure that parent agents use
to pass permissions, allowed tools, workflow refs, and rules to per-task
child agents at dispatch time.

The contract is transmitted via A2A message.metadata.executionContract.
Child agents load it at startup and apply allowlist filtering.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ExecutionContract:
    """Immutable contract from parent to child agent."""

    profile_name: str
    allowed_tools: list[str] = field(default_factory=list)
    denied_tools: list[str] = field(default_factory=list)
    workflow_ref: str = ""
    rule_refs: list[str] = field(default_factory=list)
    definition_of_done: dict[str, Any] = field(default_factory=dict)
    workspace_root: str = ""
    version: str = "1.0"
    checksum: str = ""

    def compute_checksum(self) -> str:
        """Compute SHA-256 checksum of contract content for integrity verification."""
        content = json.dumps({
            "profile_name": self.profile_name,
            "allowed_tools": sorted(self.allowed_tools),
            "denied_tools": sorted(self.denied_tools),
            "workflow_ref": self.workflow_ref,
            "rule_refs": sorted(self.rule_refs),
            "definition_of_done": self.definition_of_done,
            "version": self.version,
        }, sort_keys=True, ensure_ascii=False)
        return f"sha256:{hashlib.sha256(content.encode()).hexdigest()}"

    def to_dict(self) -> dict[str, Any]:
        """Serialize contract for A2A metadata transport."""
        return {
            "profileName": self.profile_name,
            "allowedTools": self.allowed_tools,
            "deniedTools": self.denied_tools,
            "workflowRef": self.workflow_ref,
            "ruleRefs": self.rule_refs,
            "definitionOfDone": self.definition_of_done,
            "workspaceRoot": self.workspace_root,
            "version": self.version,
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExecutionContract:
        """Deserialize contract from A2A metadata."""
        return cls(
            profile_name=data.get("profileName", ""),
            allowed_tools=data.get("allowedTools", []),
            denied_tools=data.get("deniedTools", []),
            workflow_ref=data.get("workflowRef", ""),
            rule_refs=data.get("ruleRefs", []),
            definition_of_done=data.get("definitionOfDone", {}),
            workspace_root=data.get("workspaceRoot", ""),
            version=data.get("version", "1.0"),
            checksum=data.get("checksum", ""),
        )

    def verify_checksum(self) -> bool:
        """Verify contract integrity. Returns True if valid."""
        if not self.checksum:
            return False
        return self.checksum == self.compute_checksum()


def build_execution_contract(
    profile: dict[str, Any],
    workflow_ref: str,
    rule_refs: list[str],
    workspace_root: str,
    definition_of_done: dict[str, Any] | None = None,
) -> ExecutionContract:
    """Build an ExecutionContract from a loaded permission profile.

    Args:
        profile: The child agent's permission YAML loaded as a dict.
        workflow_ref: Path to the workflow config YAML.
        rule_refs: List of paths to rule config YAMLs.
        workspace_root: Task workspace root path.
        definition_of_done: Optional DoD override (otherwise loaded from workflow).
    """
    contract = ExecutionContract(
        profile_name=profile.get("agent_id", ""),
        allowed_tools=profile.get("allowed_tools", []),
        denied_tools=profile.get("denied_tools", []),
        workflow_ref=workflow_ref,
        rule_refs=rule_refs,
        workspace_root=workspace_root,
        definition_of_done=definition_of_done or {},
        version="1.0",
    )
    contract.checksum = contract.compute_checksum()
    return contract


def load_child_profiles(profile_paths: dict[str, str]) -> dict[str, dict[str, Any]]:
    """Load multiple child permission profiles from YAML files.

    Args:
        profile_paths: Mapping of agent_id to YAML file path.

    Returns:
        Mapping of agent_id to loaded profile dict.
    """
    import yaml  # type: ignore[import-untyped]

    profiles: dict[str, dict[str, Any]] = {}
    for agent_id, path in profile_paths.items():
        try:
            with open(path, encoding="utf-8") as fh:
                profiles[agent_id] = yaml.safe_load(fh) or {}
        except FileNotFoundError:
            profiles[agent_id] = {"agent_id": agent_id, "allowed_tools": []}
    return profiles


def apply_execution_contract(
    contract: ExecutionContract,
    tool_registry: Any,
) -> None:
    """Apply execution contract to a tool registry, filtering available tools.

    This enforces the execution layer:
      effective_tools = built_in_tools ∩ contract.allowed_tools

    Args:
        contract: The execution contract from the parent agent.
        tool_registry: ToolRegistry instance to filter.

    Raises:
        PermissionDeniedError: If contract checksum verification fails.
    """
    from framework.errors import PermissionDeniedError
    from framework.permissions import PermissionEngine, PermissionSet

    # Verify contract integrity
    if not contract.verify_checksum():
        raise PermissionDeniedError(
            f"Execution contract checksum verification failed for profile '{contract.profile_name}'"
        )

    # Build PermissionSet from contract
    ps = PermissionSet(
        allowed_tools=contract.allowed_tools,
        denied_tools=contract.denied_tools,
        scm="read-write" if any("scm_" in t for t in contract.allowed_tools) else "read",
        filesystem="workspace-only",
        agent_launching=False,
        allowed_agents=[],
    )
    engine = PermissionEngine(ps)

    # Bind permission engine to the tool registry (thread-local)
    if hasattr(tool_registry, "set_permission_engine"):
        tool_registry.set_permission_engine(engine)
