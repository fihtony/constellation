"""Task-level permission grant system.

Each task carries a PermissionGrant derived from the default permission file
for its task type (development, office, etc.).  Boundary agents (Jira, SCM,
UI Design) check the grant before executing mutating operations.

Permission files live in ``common/permissions/{taskType}.json``.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from common.message_utils import build_text_artifact


_PERMISSIONS_DIR = os.path.join(os.path.dirname(__file__), "permissions")
_DEFAULT_PROTECTED_BRANCH_PATTERNS = [
    r"^main$",
    r"^master$",
    r"^develop$",
    r"^release/.*$",
]


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class OperationRule:
    action: str
    scope: str = "*"
    description: str = ""


@dataclass
class AgentPermissions:
    agent: str
    operations: list[OperationRule] = field(default_factory=list)
    escalation: str = ""


@dataclass
class PermissionGrant:
    """Immutable permission snapshot attached to a task."""

    task_type: str
    version: str = "1.0"
    allowed: list[AgentPermissions] = field(default_factory=list)
    denied: list[AgentPermissions] = field(default_factory=list)
    scope_config: dict[str, Any] = field(default_factory=dict)
    fallback: str = "deny_and_escalate"

    # --- Query helpers ---------------------------------------------------

    def is_allowed(self, agent: str, action: str, scope: str = "*") -> bool:
        """Return True if the (agent, action, scope) tuple is explicitly allowed."""
        # Denied takes priority over allowed
        if self._match_denied(agent, action, scope):
            return False
        return self._match_allowed(agent, action, scope)

    def check(self, agent: str, action: str, scope: str = "*") -> tuple[bool, str]:
        """Check an operation and return (allowed, reason)."""
        if self._match_denied(agent, action, scope):
            return False, f"Operation '{action}' is denied for agent '{agent}' by task permissions."
        if self._match_allowed(agent, action, scope):
            return True, "allowed"
        # Fallback
        if self.fallback == "allow_and_log":
            return True, f"Operation '{action}' not in explicit list — allowed by fallback (logged)."
        return False, (
            f"Operation '{action}' for agent '{agent}' is not in the allowed list. "
            f"Escalation required ({self.fallback})."
        )

    def escalation_for(self, agent: str, action: str, scope: str = "*") -> str:
        denied_entry = self._find_denied_entry(agent, action, scope)
        if denied_entry:
            return denied_entry.escalation or "require_user_approval"
        if self._match_allowed(agent, action, scope):
            return ""
        if self.fallback == "deny_and_escalate":
            return "require_user_approval"
        return ""

    def to_dict(self) -> dict:
        """Serialize for inclusion in A2A message metadata."""
        return {
            "taskType": self.task_type,
            "version": self.version,
            "scopeConfig": self.scope_config,
            "allowed": [
                {
                    "agent": ap.agent,
                    "operations": [
                        {"action": op.action, "scope": op.scope}
                        for op in ap.operations
                    ],
                }
                for ap in self.allowed
            ],
            "denied": [
                {
                    "agent": ap.agent,
                    "operations": [
                        {"action": op.action, "scope": op.scope}
                        for op in ap.operations
                    ],
                    "escalation": ap.escalation,
                }
                for ap in self.denied
            ],
            "fallback": self.fallback,
        }

    # --- Internal --------------------------------------------------------

    def _match_allowed(self, agent: str, action: str, scope: str) -> bool:
        for ap in self.allowed:
            if ap.agent != agent:
                continue
            for op in ap.operations:
                if _action_matches(op.action, action) and _scope_matches(
                    op.scope,
                    scope,
                    scope_config=self.scope_config,
                ):
                    return True
        return False

    def _match_denied(self, agent: str, action: str, scope: str = "*") -> bool:
        return self._find_denied_entry(agent, action, scope) is not None

    def _find_denied_entry(self, agent: str, action: str, scope: str = "*") -> AgentPermissions | None:
        for ap in self.denied:
            if ap.agent != agent:
                continue
            for op in ap.operations:
                if _action_matches(op.action, action) and _scope_matches(
                    op.scope,
                    scope,
                    scope_config=self.scope_config,
                ):
                    return ap
        return None


@dataclass
class PermissionDeniedDetails:
    permission_agent: str
    target_agent: str
    action: str
    target: str
    reason: str
    escalation: str = "require_user_approval"
    scope: str = "*"
    request_agent: str = ""
    task_id: str = ""
    orchestrator_task_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "error": "permission_denied",
            "permissionAgent": self.permission_agent,
            "targetAgent": self.target_agent,
            "action": self.action,
            "target": self.target,
            "scope": self.scope,
            "reason": self.reason,
            "escalation": self.escalation or "require_user_approval",
            "requestAgent": self.request_agent,
            "taskId": self.task_id,
            "orchestratorTaskId": self.orchestrator_task_id,
        }


class PermissionDeniedError(PermissionError):
    def __init__(self, details: PermissionDeniedDetails):
        self.details = details
        super().__init__(details.reason)


class PermissionEscalationRequired(RuntimeError):
    def __init__(self, details: PermissionDeniedDetails):
        self.details = details
        super().__init__(details.reason)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_permission_grant(task_type: str) -> PermissionGrant:
    """Load the default PermissionGrant for a task type.

    Falls back to a deny-all grant if the permission file is missing.
    """
    path = os.path.join(_PERMISSIONS_DIR, f"{task_type}.json")
    if not os.path.isfile(path):
        # Deny-all fallback
        return PermissionGrant(task_type=task_type, fallback="deny_and_escalate")

    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)

    return _parse_grant(raw)


def parse_permission_grant(data: dict | None) -> PermissionGrant | None:
    """Parse a PermissionGrant from A2A metadata (e.g. message.metadata.permissions)."""
    if not data:
        return None
    return _parse_grant(data)


def grant_permission(
    permissions_data: dict | None,
    *,
    agent: str,
    action: str,
    scope: str = "*",
    description: str = "",
) -> dict | None:
    """Return a permission snapshot with the requested operation explicitly granted.

    The matching denied rule is removed first because denied rules override allowed
    rules in this policy engine.
    """
    if not isinstance(permissions_data, dict):
        return permissions_data

    updated = json.loads(json.dumps(permissions_data, ensure_ascii=False))
    denied_entries = []
    for entry in updated.get("denied", []):
        if entry.get("agent") != agent:
            denied_entries.append(entry)
            continue
        operations = []
        for op in entry.get("operations", []):
            op_action = str(op.get("action") or "")
            op_scope = str(op.get("scope") or "*")
            if _action_matches(op_action, action) and _scope_matches(op_scope, scope):
                continue
            operations.append(op)
        if operations:
            next_entry = dict(entry)
            next_entry["operations"] = operations
            denied_entries.append(next_entry)
    updated["denied"] = denied_entries

    allowed_entries = list(updated.get("allowed", []))
    for entry in allowed_entries:
        if entry.get("agent") != agent:
            continue
        operations = entry.setdefault("operations", [])
        if any(
            str(op.get("action") or "") == action and str(op.get("scope") or "*") == scope
            for op in operations
        ):
            return updated
        operations.append({"action": action, "scope": scope, "description": description})
        return updated

    allowed_entries.append(
        {
            "agent": agent,
            "operations": [{"action": action, "scope": scope, "description": description}],
        }
    )
    updated["allowed"] = allowed_entries
    return updated


def build_permission_denied_details(
    *,
    permission_agent: str,
    target_agent: str,
    action: str,
    target: str,
    reason: str,
    escalation: str = "require_user_approval",
    scope: str = "*",
    request_agent: str = "",
    task_id: str = "",
    orchestrator_task_id: str = "",
) -> PermissionDeniedDetails:
    return PermissionDeniedDetails(
        permission_agent=permission_agent,
        target_agent=target_agent,
        action=action,
        target=target,
        reason=reason,
        escalation=escalation or "require_user_approval",
        scope=scope,
        request_agent=request_agent,
        task_id=task_id,
        orchestrator_task_id=orchestrator_task_id,
    )


def build_permission_denied_artifact(details: PermissionDeniedDetails, *, agent_id: str = "") -> dict[str, Any]:
    metadata = details.to_dict()
    metadata["permissionDenied"] = True
    if agent_id:
        metadata["agentId"] = agent_id
    return build_text_artifact(
        "permission-denied",
        json.dumps(details.to_dict(), ensure_ascii=False),
        artifact_type="application/json",
        metadata=metadata,
    )


def extract_permission_denial(task: dict | None) -> PermissionDeniedDetails | None:
    if not isinstance(task, dict):
        return None
    for artifact in task.get("artifacts") or []:
        metadata = artifact.get("metadata") if isinstance(artifact, dict) else {}
        if isinstance(metadata, dict) and metadata.get("permissionDenied"):
            payload = {
                "permissionAgent": metadata.get("permissionAgent"),
                "targetAgent": metadata.get("targetAgent"),
                "action": metadata.get("action"),
                "target": metadata.get("target"),
                "scope": metadata.get("scope") or "*",
                "reason": metadata.get("reason") or "Permission denied.",
                "escalation": metadata.get("escalation") or "require_user_approval",
                "requestAgent": metadata.get("requestAgent") or "",
                "taskId": metadata.get("taskId") or "",
                "orchestratorTaskId": metadata.get("orchestratorTaskId") or "",
            }
            if payload["permissionAgent"] and payload["targetAgent"] and payload["action"]:
                return PermissionDeniedDetails(
                    permission_agent=str(payload["permissionAgent"]),
                    target_agent=str(payload["targetAgent"]),
                    action=str(payload["action"]),
                    target=str(payload["target"] or ""),
                    scope=str(payload["scope"] or "*"),
                    reason=str(payload["reason"]),
                    escalation=str(payload["escalation"] or "require_user_approval"),
                    request_agent=str(payload["requestAgent"] or ""),
                    task_id=str(payload["taskId"] or ""),
                    orchestrator_task_id=str(payload["orchestratorTaskId"] or ""),
                )
    return None


def _parse_grant(raw: dict) -> PermissionGrant:
    allowed: list[AgentPermissions] = []
    for entry in raw.get("allowed", []):
        agent = entry.get("agent")
        if not agent:
            continue
        ops = [
            OperationRule(
                action=op.get("action", ""),
                scope=op.get("scope", "*"),
                description=op.get("description", ""),
            )
            for op in entry.get("operations", [])
        ]
        allowed.append(AgentPermissions(agent=agent, operations=ops))

    denied: list[AgentPermissions] = []
    for entry in raw.get("denied", []):
        agent = entry.get("agent")
        if not agent:
            continue
        ops = [
            OperationRule(
                action=op.get("action", ""),
                scope=op.get("scope", "*"),
                description=op.get("description", ""),
            )
            for op in entry.get("operations", [])
        ]
        denied.append(
            AgentPermissions(
                agent=agent,
                operations=ops,
                escalation=entry.get("escalation", ""),
            )
        )

    return PermissionGrant(
        task_type=raw.get("taskType", "unknown"),
        version=raw.get("version", "1.0"),
        allowed=allowed,
        denied=denied,
        scope_config=raw.get("scopeConfig") if isinstance(raw.get("scopeConfig"), dict) else {},
        fallback=raw.get("fallback", "deny_and_escalate"),
    )


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _action_matches(pattern: str, action: str) -> bool:
    """Check if an action pattern matches an action string.

    Supports:
      - Exact match: "read" matches "read"
      - Wildcard: "*" matches anything
      - Prefix: "issue.update.*" matches "issue.update.summary"
    """
    if pattern == "*" or pattern == action:
        return True
    if pattern.endswith(".*"):
        prefix = pattern[:-2]
        return action.startswith(prefix + ".") or action == prefix
    return False


def _load_protected_branch_patterns(scope_config: dict[str, Any] | None = None) -> list[str]:
    if not isinstance(scope_config, dict):
        return list(_DEFAULT_PROTECTED_BRANCH_PATTERNS)
    scm_config = scope_config.get("scm")
    if not isinstance(scm_config, dict):
        return list(_DEFAULT_PROTECTED_BRANCH_PATTERNS)
    raw_patterns = scm_config.get("protectedBranchPatterns")
    if not isinstance(raw_patterns, list):
        return list(_DEFAULT_PROTECTED_BRANCH_PATTERNS)
    patterns = [str(item).strip() for item in raw_patterns if str(item).strip()]
    return patterns or list(_DEFAULT_PROTECTED_BRANCH_PATTERNS)


def _regex_matches(pattern: str, value: str) -> bool:
    try:
        return re.fullmatch(pattern, value) is not None
    except re.error:
        print(f"[task-permissions] Invalid regex pattern: {pattern!r}")
        # Fail safe: malformed protected-branch regex blocks the branch write.
        return True


def _branch_is_protected(requested_scope: str, scope_config: dict[str, Any] | None = None) -> bool:
    branch_name = str(requested_scope or "").strip()
    if not branch_name:
        return True
    for pattern in _load_protected_branch_patterns(scope_config):
        if _regex_matches(pattern, branch_name):
            return True
    return False


def _scope_matches(
    allowed_scope: str,
    requested_scope: str,
    scope_config: dict[str, Any] | None = None,
) -> bool:
    """Check if a scope pattern matches a requested scope.

    Supports:
      - "*" matches everything
      - "self" matches "self"
      - "branch:development" matches any branch not protected by config regexes
      - "branch:protected" matches branches protected by config regexes
      - "regex:<pattern>" matches by full regular expression
      - "task_root" matches "task_root"
    """
    if allowed_scope == "*":
        return True
    if allowed_scope == requested_scope:
        return True
    if allowed_scope.startswith("regex:"):
        pattern = allowed_scope[len("regex:"):].strip()
        return bool(pattern) and _regex_matches(pattern, str(requested_scope or "").strip())
    if allowed_scope in {"branch:development", "dev/*"}:
        return not _branch_is_protected(requested_scope, scope_config=scope_config)
    if allowed_scope == "branch:protected":
        return _branch_is_protected(requested_scope, scope_config=scope_config)
    return False


# ---------------------------------------------------------------------------
# Audit logging
# ---------------------------------------------------------------------------

def audit_permission_check(
    *,
    task_id: str,
    orchestrator_task_id: str,
    request_agent: str,
    target_agent: str,
    action: str,
    target: str,
    decision: str,
    reason: str,
    agent_id: str = "",
) -> dict[str, Any]:
    """Build and print a structured audit log entry for a permission check."""
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "event": "PERMISSION_CHECK",
        "taskId": task_id,
        "orchestratorTaskId": orchestrator_task_id,
        "requestAgent": request_agent,
        "targetAgent": target_agent,
        "action": action,
        "target": target,
        "decision": decision,
        "reason": reason,
    }
    prefix = f"[{agent_id}] " if agent_id else ""
    print(f"{prefix}[audit] {json.dumps(entry, ensure_ascii=False)}")
    return entry
