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
from pathlib import Path
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


def resolve_execution_contract_permission_set(
    permission_profile: str,
    contract_data: dict[str, Any] | ExecutionContract,
) -> tuple[ExecutionContract, "PermissionSet"]:
    """Resolve a child execution contract against its local permission profile.

    The local permission profile defines the child's maximum capability envelope.
    The parent-supplied execution contract may only narrow that envelope; it may
    not broaden it.
    """
    from framework.permissions import PermissionEngine, PermissionSet

    contract = (
        contract_data
        if isinstance(contract_data, ExecutionContract)
        else ExecutionContract.from_dict(contract_data)
    )
    if not contract.verify_checksum() or contract.version != "1.0":
        raise ValueError("checksum or version verification failed")
    if not contract.allowed_tools:
        raise ValueError("allowedTools must be non-empty")
    if permission_profile and contract.profile_name and contract.profile_name != permission_profile:
        raise ValueError(
            f"contract profileName {contract.profile_name!r} does not match local profile {permission_profile!r}"
        )

    baseline: PermissionSet | None = None
    if permission_profile:
        root = Path(__file__).resolve().parent.parent
        perm_path = root / "config" / "permissions" / f"{permission_profile}.yaml"
        if not perm_path.is_file():
            raise ValueError(f"permission profile not found: {permission_profile}")
        baseline = PermissionEngine.from_yaml(str(perm_path)).permissions

    baseline_allowed = set((baseline.allowed_tools if baseline else []) or [])
    requested_allowed = list(contract.allowed_tools)
    if baseline_allowed:
        unexpected = sorted(set(requested_allowed) - baseline_allowed)
        if unexpected:
            raise ValueError(
                "contract allowedTools exceed local profile: " + ", ".join(unexpected)
            )
        effective_allowed = [tool for tool in requested_allowed if tool in baseline_allowed]
    else:
        effective_allowed = requested_allowed

    denied = set(contract.denied_tools)
    if baseline:
        denied.update(baseline.denied_tools)
    effective_denied = sorted(denied)

    if baseline:
        scm = baseline.scm
        filesystem = baseline.filesystem
        agent_launching = baseline.agent_launching
        allowed_agents = list(baseline.allowed_agents)
        custom = dict(baseline.custom)
    else:
        scm = "read-write" if any(tool.startswith("scm_") for tool in effective_allowed) else "read"
        filesystem = "workspace-only"
        agent_launching = False
        allowed_agents = []
        custom = {}

    if scm == "read-write" and not any(tool.startswith("scm_") for tool in effective_allowed):
        scm = "read"

    permission_set = PermissionSet(
        allowed_tools=effective_allowed,
        denied_tools=effective_denied,
        scm=scm,
        filesystem=filesystem,
        custom=custom,
        agent_launching=agent_launching,
        allowed_agents=allowed_agents,
    )
    return contract, permission_set
