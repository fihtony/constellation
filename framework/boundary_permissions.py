"""Shared permission helpers for boundary adapters.

Boundary adapters must validate the caller permission snapshot carried in
``message.metadata.permissions``. In-process tool calls may not build a full
message envelope, so these helpers also fall back to the current thread-local
ToolRegistry permission engine when available.
"""
from __future__ import annotations

import os
import re
from typing import Any


_DEFAULT_PROTECTED_BRANCH_PATTERNS = [
    r"^main$",
    r"^master$",
    r"^develop$",
    r"^release/.*$",
]


def permission_enforcement_mode() -> str:
    mode = os.environ.get("PERMISSION_ENFORCEMENT", "strict").strip().lower() or "strict"
    return mode if mode in {"strict", "warn", "off"} else "strict"


def current_permission_snapshot() -> dict[str, Any] | None:
    from framework.tools.registry import get_registry

    engine = getattr(get_registry(), "_permission_engine", None)
    permissions = getattr(engine, "permissions", None)
    if permissions is None:
        return None
    return {
        "allowedTools": list(getattr(permissions, "allowed_tools", []) or []),
        "deniedTools": list(getattr(permissions, "denied_tools", []) or []),
        "scm": getattr(permissions, "scm", "read"),
        "filesystem": getattr(permissions, "filesystem", "workspace-only"),
        "custom": dict(getattr(permissions, "custom", {}) or {}),
    }


def branch_scope(branch_name: str, permissions_snapshot: dict[str, Any] | None = None) -> str:
    branch = (branch_name or "").strip()
    if not branch:
        return "*"

    patterns = _DEFAULT_PROTECTED_BRANCH_PATTERNS
    if isinstance(permissions_snapshot, dict):
        scope_config = permissions_snapshot.get("scopeConfig") or {}
        scm_config = scope_config.get("scm") if isinstance(scope_config, dict) else {}
        candidate_patterns = (
            scm_config.get("protectedBranchPatterns")
            if isinstance(scm_config, dict)
            else None
        )
        if isinstance(candidate_patterns, list) and candidate_patterns:
            patterns = [str(item) for item in candidate_patterns]

    for pattern in patterns:
        try:
            if re.fullmatch(pattern, branch):
                return "branch:protected"
        except re.error:
            continue
    return "branch:development"


def enforce_boundary_permission(
    *,
    agent_id: str,
    capability: str,
    metadata: dict[str, Any] | None,
    required_tools: list[str],
    grant_agent: str,
    grant_action: str,
    scope: str = "*",
    require_scm_write: bool = False,
) -> dict[str, Any] | None:
    mode = permission_enforcement_mode()
    if mode == "off":
        return None

    meta = metadata or {}
    permissions_snapshot = meta.get("permissions")
    if not isinstance(permissions_snapshot, dict):
        permissions_snapshot = current_permission_snapshot()

    allowed = False
    if isinstance(permissions_snapshot, dict):
        if "allowed" in permissions_snapshot or "denied" in permissions_snapshot:
            allowed = _check_grant_permissions(
                permissions_snapshot,
                grant_agent=grant_agent,
                grant_action=grant_action,
                scope=scope,
            )
        else:
            allowed = _check_tool_permissions(
                permissions_snapshot,
                required_tools=required_tools,
                require_scm_write=require_scm_write,
            )

    if allowed:
        return None

    reason = _denial_reason(
        required_tools=required_tools,
        grant_agent=grant_agent,
        grant_action=grant_action,
        scope=scope,
        permissions_snapshot=permissions_snapshot,
        require_scm_write=require_scm_write,
    )
    from framework.audit_log import append_current_permission_denial

    append_current_permission_denial(
        operation=f"boundary:{capability}",
        reason=reason,
        metadata={
            "capability": capability,
            "grant_agent": grant_agent,
            "grant_action": grant_action,
            "scope": scope,
        },
    )
    if mode == "strict":
        return {
            "status": "permission_denied",
            "error": reason,
            "capability": capability,
        }

    print(
        f"[{agent_id}] WARN: permission check failed for {capability} "
        f"but enforcement={mode}: {reason}"
    )
    return None


def _check_tool_permissions(
    snapshot: dict[str, Any],
    *,
    required_tools: list[str],
    require_scm_write: bool,
) -> bool:
    allowed_tools = list(snapshot.get("allowed_tools") or snapshot.get("allowedTools") or [])
    denied_tools = set(snapshot.get("denied_tools") or snapshot.get("deniedTools") or [])
    scm_mode = str(snapshot.get("scm") or "read")

    if require_scm_write and scm_mode != "read-write":
        return False
    if not required_tools:
        return scm_mode != "none"
    for tool_name in required_tools:
        if tool_name in denied_tools:
            continue
        if allowed_tools and tool_name not in allowed_tools:
            continue
        return True
    return False


def _check_grant_permissions(
    snapshot: dict[str, Any],
    *,
    grant_agent: str,
    grant_action: str,
    scope: str,
) -> bool:
    denied_entries = snapshot.get("denied") or []
    allowed_entries = snapshot.get("allowed") or []

    if _match_grant_entries(denied_entries, grant_agent, grant_action, scope):
        return False
    if _match_grant_entries(allowed_entries, grant_agent, grant_action, scope):
        return True

    return str(snapshot.get("fallback") or "deny_and_escalate") == "allow_and_log"


def _match_grant_entries(
    entries: list[Any],
    grant_agent: str,
    grant_action: str,
    scope: str,
) -> bool:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("agent") or "") != grant_agent:
            continue
        operations = entry.get("operations") or []
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            action = str(operation.get("action") or "")
            action_scope = str(operation.get("scope") or "*")
            if _action_matches(action, grant_action) and _scope_matches(action_scope, scope):
                return True
    return False


def _action_matches(pattern: str, action: str) -> bool:
    return pattern in {"", "*"} or pattern == action


def _scope_matches(pattern: str, scope: str) -> bool:
    return pattern in {"", "*"} or pattern == scope


def _denial_reason(
    *,
    required_tools: list[str],
    grant_agent: str,
    grant_action: str,
    scope: str,
    permissions_snapshot: dict[str, Any] | None,
    require_scm_write: bool,
) -> str:
    if not isinstance(permissions_snapshot, dict):
        return "No permissions attached to request. Explicit permission grant required."

    if "allowed" in permissions_snapshot or "denied" in permissions_snapshot:
        return (
            f"Operation '{grant_action}' for agent '{grant_agent}' is not permitted "
            f"for scope '{scope}'."
        )

    if require_scm_write and str(permissions_snapshot.get("scm") or "read") != "read-write":
        return "SCM write operations are not permitted by the attached permission snapshot."

    if required_tools:
        return (
            "None of the required tools are permitted by the attached permission snapshot: "
            + ", ".join(required_tools)
        )

    return "The attached permission snapshot does not allow this boundary operation."
