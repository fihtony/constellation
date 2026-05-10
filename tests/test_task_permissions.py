#!/usr/bin/env python3
"""Tests for common/task_permissions.py — permission grant loading, matching, and checking."""

from __future__ import annotations

import json
import os
import sys
import tempfile

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from common.task_permissions import (
    PermissionGrant,
    _action_matches,
    _branch_is_protected,
    _scope_matches,
    audit_permission_check,
    build_permission_denied_artifact,
    build_permission_denied_details,
    extract_permission_denial,
    grant_permission,
    load_permission_grant,
    parse_permission_grant,
)


# ---------------------------------------------------------------------------
# Action matching
# ---------------------------------------------------------------------------

def test_action_matches_exact():
    assert _action_matches("read", "read")
    assert not _action_matches("read", "write")

def test_action_matches_wildcard():
    assert _action_matches("*", "anything")
    assert _action_matches("*", "issue.update.summary")

def test_action_matches_prefix():
    assert _action_matches("issue.update.*", "issue.update.summary")
    assert _action_matches("issue.update.*", "issue.update.description")
    assert _action_matches("issue.update.*", "issue.update")
    assert not _action_matches("issue.update.*", "issue.delete")
    assert not _action_matches("issue.update.*", "comment.add")


# ---------------------------------------------------------------------------
# Scope matching
# ---------------------------------------------------------------------------

def test_scope_matches_wildcard():
    assert _scope_matches("*", "anything")

def test_scope_matches_exact():
    assert _scope_matches("self", "self")
    assert not _scope_matches("self", "other")

def test_scope_matches_branch_development_default_protected_patterns():
    assert _scope_matches("branch:development", "dev/feature-123")
    assert _scope_matches("branch:development", "feature/my-branch")
    assert _scope_matches("branch:development", "fix/bug-456")
    assert _scope_matches("branch:development", "hotfix/urgent")
    assert _scope_matches("branch:development", "chore/cleanup")
    assert _scope_matches("branch:development", "agent/test-123")
    assert _scope_matches("branch:development", "PROJ-2903-task-1")
    assert not _scope_matches("branch:development", "main")
    assert not _scope_matches("branch:development", "master")
    assert not _scope_matches("branch:development", "develop")
    assert not _scope_matches("branch:development", "release/1.0")

def test_scope_matches_branch_protected_default_patterns():
    assert _scope_matches("branch:protected", "main")
    assert _scope_matches("branch:protected", "master")
    assert _scope_matches("branch:protected", "develop")
    assert _scope_matches("branch:protected", "release/2026.05")
    assert not _scope_matches("branch:protected", "feature/my-branch")

def test_scope_matches_regex_pattern():
    assert _scope_matches("regex:^feature/.+$", "feature/my-branch")
    assert not _scope_matches("regex:^feature/.+$", "fix/my-branch")

def test_scope_matches_branch_development_with_custom_protected_patterns():
    scope_config = {
        "scm": {
            "protectedBranchPatterns": [r"^main$", r"^prod/.*$"],
        }
    }
    assert _scope_matches("branch:development", "feature/my-branch", scope_config=scope_config)
    assert not _scope_matches("branch:development", "prod/2026.05", scope_config=scope_config)
    assert _branch_is_protected("", scope_config=scope_config)


# ---------------------------------------------------------------------------
# Load from permission files
# ---------------------------------------------------------------------------

def test_load_development_permissions():
    grant = load_permission_grant("development")
    assert grant.task_type == "development"
    # Jira read should be allowed
    assert grant.is_allowed("jira", "read")
    assert grant.is_allowed("jira", "ticket.read")
    # Comment add should be allowed
    assert grant.is_allowed("jira", "comment.add")
    # Transition should be allowed
    assert grant.is_allowed("jira", "transition")
    assert grant.is_allowed("jira", "ticket.transition")
    # Labels update should be allowed
    assert grant.is_allowed("jira", "issue.update.labels")
    # Summary update should be denied
    assert not grant.is_allowed("jira", "issue.update.summary")
    # Description update should be denied
    assert not grant.is_allowed("jira", "issue.update.description")
    # Issue delete should be denied
    assert not grant.is_allowed("jira", "issue.delete")

def test_load_office_permissions():
    grant = load_permission_grant("office")
    assert grant.task_type == "office"
    # Read in task root should be allowed
    assert grant.is_allowed("office", "read", "task_root")
    # Write should be denied
    assert not grant.is_allowed("office", "write")
    # Access outside root should be denied
    assert not grant.is_allowed("office", "access_outside_root")


def test_grant_permission_removes_denied_rule_and_allows_office_write():
    permissions = load_permission_grant("office").to_dict()
    updated = grant_permission(
        permissions,
        agent="office",
        action="write",
        scope="task_root",
        description="Approved by user",
    )
    restored = parse_permission_grant(updated)
    assert restored is not None
    allowed, reason = restored.check("office", "write", "task_root")
    assert allowed, reason


def test_extract_permission_denial_from_artifact_metadata():
    details = build_permission_denied_details(
        permission_agent="jira",
        target_agent="jira-agent",
        action="issue.update.description",
        target="PROJ-123",
        reason="Operation denied by task permissions.",
        task_id="jira-task-1",
        orchestrator_task_id="compass-task-1",
    )
    task = {"artifacts": [build_permission_denied_artifact(details, agent_id="jira-agent")]}
    parsed = extract_permission_denial(task)
    assert parsed is not None
    assert parsed.permission_agent == "jira"
    assert parsed.target_agent == "jira-agent"
    assert parsed.action == "issue.update.description"
    assert parsed.target == "PROJ-123"

def test_load_missing_task_type():
    grant = load_permission_grant("nonexistent_type")
    assert grant.task_type == "nonexistent_type"
    assert grant.fallback == "deny_and_escalate"
    # Everything should be denied since there's no allowed list
    assert not grant.is_allowed("any", "any_action")


# ---------------------------------------------------------------------------
# Permission check with reason
# ---------------------------------------------------------------------------

def test_check_allowed_operation():
    grant = load_permission_grant("development")
    allowed, reason = grant.check("jira", "read")
    assert allowed
    assert reason == "allowed"
    allowed, reason = grant.check("jira", "ticket.read")
    assert allowed
    assert reason == "allowed"

def test_check_denied_operation():
    grant = load_permission_grant("development")
    allowed, reason = grant.check("jira", "issue.update.summary")
    assert not allowed
    assert "denied" in reason.lower() or "not in" in reason.lower()

def test_check_unknown_operation_fallback():
    grant = load_permission_grant("development")
    allowed, reason = grant.check("jira", "some.unknown.operation")
    assert not allowed  # deny_and_escalate fallback
    assert "not in the allowed list" in reason.lower() or "escalation" in reason.lower()


# ---------------------------------------------------------------------------
# SCM permissions
# ---------------------------------------------------------------------------

def test_scm_clone_allowed():
    grant = load_permission_grant("development")
    assert grant.is_allowed("scm", "repo.clone")
    assert grant.is_allowed("scm", "repo.search")
    assert grant.is_allowed("scm", "repo.inspect")
    assert grant.is_allowed("scm", "repo.tree")
    assert grant.is_allowed("scm", "repo.file")
    assert grant.is_allowed("scm", "branch.list")

def test_scm_pr_comment_allowed():
    grant = load_permission_grant("development")
    assert grant.is_allowed("scm", "pr.get")
    assert grant.is_allowed("scm", "pr.list")
    assert grant.is_allowed("scm", "pr.comment.list")
    assert grant.is_allowed("scm", "pr.comment", "self")

def test_scm_branch_push_dev():
    grant = load_permission_grant("development")
    assert grant.is_allowed("scm", "branch.push", "feature/my-branch")
    assert grant.is_allowed("scm", "branch.push", "dev/my-branch")
    assert not grant.is_allowed("scm", "branch.push", "main")
    assert not grant.is_allowed("scm", "branch.push", "develop")
    assert not grant.is_allowed("scm", "branch.push", "release/1.0")

def test_scm_push_default_denied():
    grant = load_permission_grant("development")
    allowed, reason = grant.check("scm", "branch.push", "main")
    assert not allowed
    assert "denied" in reason.lower() or "not in the allowed list" in reason.lower()

def test_scm_repo_delete_denied():
    grant = load_permission_grant("development")
    assert not grant.is_allowed("scm", "repo.delete")


# ---------------------------------------------------------------------------
# Denied takes priority over allowed
# ---------------------------------------------------------------------------

def test_denied_overrides_allowed():
    """If an operation appears in both allowed and denied, denied wins."""
    grant = PermissionGrant(
        task_type="test",
        allowed=[
            __import__("common.task_permissions", fromlist=["AgentPermissions"]).AgentPermissions(
                agent="jira",
                operations=[
                    __import__("common.task_permissions", fromlist=["OperationRule"]).OperationRule(
                        action="*", scope="*"
                    )
                ],
            )
        ],
        denied=[
            __import__("common.task_permissions", fromlist=["AgentPermissions"]).AgentPermissions(
                agent="jira",
                operations=[
                    __import__("common.task_permissions", fromlist=["OperationRule"]).OperationRule(
                        action="issue.delete"
                    )
                ],
                escalation="require_user_approval",
            )
        ],
    )
    assert grant.is_allowed("jira", "read")
    assert not grant.is_allowed("jira", "issue.delete")


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------

def test_to_dict_and_parse():
    grant = load_permission_grant("development")
    data = grant.to_dict()
    restored = parse_permission_grant(data)
    assert restored is not None
    assert restored.task_type == grant.task_type
    assert restored.scope_config == grant.scope_config
    assert restored.is_allowed("jira", "read") == grant.is_allowed("jira", "read")
    assert restored.is_allowed("jira", "issue.update.summary") == grant.is_allowed("jira", "issue.update.summary")


def test_parse_none():
    assert parse_permission_grant(None) is None

def test_parse_empty_dict():
    # Empty dict is treated as "no permissions" — returns None
    assert parse_permission_grant({}) is None

def test_parse_malformed_entries_without_agent():
    result = parse_permission_grant(
        {
            "taskType": "development",
            "allowed": [{"operations": [{"action": "read"}]}],
            "denied": [{"operations": [{"action": "issue.delete"}]}],
        }
    )
    assert result is not None
    assert result.task_type == "development"
    assert not result.allowed
    assert not result.denied


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def test_audit_log_structure(capsys=None):
    entry = audit_permission_check(
        task_id="task-001",
        orchestrator_task_id="compass-001",
        request_agent="team-lead",
        target_agent="jira-agent",
        action="issue.update.description",
        target="PROJ-2903",
        decision="denied",
        reason="not in allowed list",
        agent_id="jira-agent",
    )
    assert entry["event"] == "PERMISSION_CHECK"
    assert entry["decision"] == "denied"
    assert entry["action"] == "issue.update.description"


# ---------------------------------------------------------------------------
# Prompt file loading (test_stitch_ui.py)
# ---------------------------------------------------------------------------

def test_design_to_code_prompt_loads_from_files():
    """Verify the design-to-code prompt is assembled from external files, not inlined."""
    workflow_file = os.path.join(_REPO_ROOT, "team-lead", "workflows", "design-to-code-workflow.md")
    web_file = os.path.join(_REPO_ROOT, "web", "prompts", "react-tailwind-workflow.md")
    assert os.path.isfile(workflow_file), f"Missing: {workflow_file}"
    assert os.path.isfile(web_file), f"Missing: {web_file}"

    with open(workflow_file, encoding="utf-8") as fh:
        content = fh.read()
    assert "Source-of-Truth" in content or "source-of-truth" in content.lower()
    assert "Colour Discipline" in content or "colour discipline" in content.lower()
    assert "Design Audit" in content or "design audit" in content.lower()

    with open(web_file, encoding="utf-8") as fh:
        web_content = fh.read()
    assert "Tailwind" in web_content
    assert "CSS Bundle" in web_content or "css bundle" in web_content.lower()


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main():
    tests = [
        fn for name, fn in sorted(globals().items())
        if name.startswith("test_") and callable(fn)
    ]
    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            print(f"  ✅ {test_fn.__name__}")
            passed += 1
        except Exception as exc:
            print(f"  ❌ {test_fn.__name__}: {exc}")
            failed += 1

    print(f"\nPassed: {passed}  Failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
