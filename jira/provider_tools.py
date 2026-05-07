"""Internal Jira provider tools for agentic runtime.

These tools wrap the local Jira provider directly (no A2A self-calls).
They are registered in the global tool registry so the connect-agent runtime
can expose them to the LLM running inside the Jira Agent process.

Usage in app.py:
    import jira.provider_tools as _jpt          # auto-registers tools
    _jpt.configure_jira_provider_tools(
        message=message,
        provider=PROVIDER,
        permission_fn=lambda action, target, scope="*": _require_jira_permission(
            action=action, target=target, scope=scope, message=message
        ),
    )
"""
from __future__ import annotations

import json
from typing import Any, Callable

from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import is_registered, register_tool

# ---------------------------------------------------------------------------
# Per-task context — configured by configure_jira_provider_tools() before
# run_agentic() is called.
# ---------------------------------------------------------------------------
_current_message: dict = {}
_current_provider: Any = None
_permission_fn: Callable[[str, str, str], None] | None = None


def configure_jira_provider_tools(
    *,
    message: dict,
    provider: Any,
    permission_fn: Callable[[str, str, str], None] | None = None,
) -> None:
    """Wire up the provider and permission callback for the current task."""
    global _current_message, _current_provider, _permission_fn
    _current_message = message
    _current_provider = provider
    _permission_fn = permission_fn


def _require(action: str, target: str, scope: str = "*") -> None:
    if _permission_fn:
        _permission_fn(action, target, scope)


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

class _JiraIssueLookupTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_issue_lookup",
            description=(
                "Fetch a Jira issue by key (e.g. PROJ-123). "
                "Returns the full issue including summary, status, description, and comments."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_key": {
                        "type": "string",
                        "description": "Jira issue key, e.g. 'PROJ-123'.",
                    },
                },
                "required": ["ticket_key"],
            },
        )

    def execute(self, args: dict) -> dict:
        key = args.get("ticket_key", "")
        _require("ticket.read", key)
        issue, status = _current_provider.fetch_issue(key)
        if issue is None:
            return self.error(f"jira_issue_lookup: {status}")
        return self.ok(json.dumps(issue, ensure_ascii=False))


class _JiraSearchTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_search",
            description=(
                "Search Jira issues using JQL. "
                "Returns a list of matching issues with key, summary, status, and assignee."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "jql": {
                        "type": "string",
                        "description": "Jira Query Language expression, e.g. 'project = PROJ AND status = Open'.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Maximum number of issues to return (default: 20).",
                    },
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of field names to return (default: summary, status, assignee).",
                    },
                },
                "required": ["jql"],
            },
        )

    def execute(self, args: dict) -> dict:
        _require("ticket.read", "search")
        issues, status = _current_provider.search_issues(
            args.get("jql", ""),
            max_results=args.get("max_results", 20),
            fields=args.get("fields"),
        )
        if issues is None:
            return self.error(f"jira_search: {status}")
        return self.ok(json.dumps(issues, ensure_ascii=False))


class _JiraGetMyselfTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_get_myself",
            description="Get the current authenticated Jira user's account ID and display name.",
            input_schema={"type": "object", "properties": {}},
        )

    def execute(self, args: dict) -> dict:
        _require("ticket.read", "myself")
        myself, status = _current_provider.get_myself()
        if myself is None:
            return self.error(f"jira_get_myself: {status}")
        return self.ok(json.dumps(myself, ensure_ascii=False))


class _JiraGetTransitionsTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_get_transitions",
            description="Get available workflow transitions for a Jira issue.",
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_key": {"type": "string", "description": "Jira issue key."},
                },
                "required": ["ticket_key"],
            },
        )

    def execute(self, args: dict) -> dict:
        key = args.get("ticket_key", "")
        _require("ticket.read", key)
        transitions, status = _current_provider.get_transitions(key)
        if transitions is None:
            return self.error(f"jira_get_transitions: {status}")
        return self.ok(json.dumps(transitions, ensure_ascii=False))


class _JiraValidatePermissionsTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_validate_permissions",
            description=(
                "Validate whether the current request has permission for a given Jira action. "
                "Returns 'allowed' or 'denied' with a reason."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "description": "Action to check, e.g. 'ticket.write', 'comment.add'.",
                    },
                    "target": {
                        "type": "string",
                        "description": "Target resource, e.g. ticket key 'PROJ-123'.",
                    },
                },
                "required": ["action", "target"],
            },
        )

    def execute(self, args: dict) -> dict:
        action = args.get("action", "ticket.read")
        target = args.get("target", "")
        try:
            _require(action, target)
            return self.ok(json.dumps({"allowed": True, "reason": "Permission granted."}))
        except Exception as exc:
            return self.ok(json.dumps({"allowed": False, "reason": str(exc)}))


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------

class _JiraCommentTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_comment",
            description="Add a comment to a Jira issue.",
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_key": {"type": "string", "description": "Jira issue key."},
                    "body": {"type": "string", "description": "Comment body text (plain text or Markdown)."},
                },
                "required": ["ticket_key", "body"],
            },
        )

    def execute(self, args: dict) -> dict:
        key = args.get("ticket_key", "")
        _require("comment.add", key)
        result, status = _current_provider.add_comment(key, args.get("body", ""))
        if result is None:
            return self.error(f"jira_comment: {status}")
        return self.ok(json.dumps(result, ensure_ascii=False))


class _JiraTransitionTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_transition",
            description=(
                "Transition a Jira issue to a new status (e.g. 'In Progress', 'Done'). "
                "Use jira_get_transitions first to see valid transitions."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_key": {"type": "string", "description": "Jira issue key."},
                    "transition_name": {
                        "type": "string",
                        "description": "Target status name (case-insensitive) or transition ID.",
                    },
                },
                "required": ["ticket_key", "transition_name"],
            },
        )

    def execute(self, args: dict) -> dict:
        key = args.get("ticket_key", "")
        _require("ticket.transition", key)
        result, status = _current_provider.transition_issue(key, args.get("transition_name", ""))
        if "error" in (status or "").lower() and not result:
            return self.error(f"jira_transition: {status}")
        return self.ok(json.dumps(result or {"status": status}, ensure_ascii=False))


class _JiraAssignTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_assign",
            description="Assign a Jira issue to a user by their account ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_key": {"type": "string", "description": "Jira issue key."},
                    "account_id": {
                        "type": "string",
                        "description": "Jira account ID of the assignee. Use jira_get_myself for your own ID.",
                    },
                },
                "required": ["ticket_key", "account_id"],
            },
        )

    def execute(self, args: dict) -> dict:
        key = args.get("ticket_key", "")
        _require("ticket.assign", key)
        result, status = _current_provider.change_assignee(key, args.get("account_id", ""))
        if "error" in (status or "").lower() and not result:
            return self.error(f"jira_assign: {status}")
        return self.ok(json.dumps(result or {"status": status}, ensure_ascii=False))


class _JiraCreateIssueTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_create_issue",
            description="Create a new Jira issue.",
            input_schema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project key (e.g. 'PROJ')."},
                    "summary": {"type": "string", "description": "Issue summary/title."},
                    "description": {"type": "string", "description": "Issue description (plain text or Markdown)."},
                    "issue_type": {
                        "type": "string",
                        "description": "Issue type name, e.g. 'Bug', 'Story', 'Task' (default: Task).",
                    },
                    "fields": {
                        "type": "object",
                        "description": "Additional fields as key-value pairs.",
                    },
                },
                "required": ["project", "summary"],
            },
        )

    def execute(self, args: dict) -> dict:
        _require("ticket.create", args.get("project", ""))
        issue, status = _current_provider.create_issue(
            args.get("project", ""),
            args.get("summary", ""),
            args.get("description", ""),
            args.get("issue_type", "Task"),
            args.get("fields", {}),
        )
        if issue is None:
            return self.error(f"jira_create_issue: {status}")
        return self.ok(json.dumps(issue, ensure_ascii=False))


class _JiraUpdateFieldsTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_update_fields",
            description="Update fields on an existing Jira issue.",
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_key": {"type": "string", "description": "Jira issue key."},
                    "fields": {
                        "type": "object",
                        "description": "Fields to update as key-value pairs, e.g. {\"labels\": [\"bug\"]}.",
                    },
                },
                "required": ["ticket_key", "fields"],
            },
        )

    def execute(self, args: dict) -> dict:
        key = args.get("ticket_key", "")
        _require("ticket.write", key)
        result, status = _current_provider.update_issue_fields(key, args.get("fields", {}))
        if "error" in (status or "").lower() and not result:
            return self.error(f"jira_update_fields: {status}")
        return self.ok(json.dumps(result or {"status": status}, ensure_ascii=False))


class _JiraUpdateCommentTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_update_comment",
            description="Update an existing comment on a Jira issue.",
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_key": {"type": "string", "description": "Jira issue key."},
                    "comment_id": {"type": "string", "description": "Comment ID to update."},
                    "body": {"type": "string", "description": "New comment body text."},
                },
                "required": ["ticket_key", "comment_id", "body"],
            },
        )

    def execute(self, args: dict) -> dict:
        key = args.get("ticket_key", "")
        _require("comment.edit", key)
        result, status = _current_provider.update_comment(
            key, args.get("comment_id", ""), args.get("body", "")
        )
        if "error" in (status or "").lower() and not result:
            return self.error(f"jira_update_comment: {status}")
        return self.ok(json.dumps(result or {"status": status}, ensure_ascii=False))


class _JiraDeleteCommentTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_delete_comment",
            description="Delete a comment from a Jira issue.",
            input_schema={
                "type": "object",
                "properties": {
                    "ticket_key": {"type": "string", "description": "Jira issue key."},
                    "comment_id": {"type": "string", "description": "Comment ID to delete."},
                },
                "required": ["ticket_key", "comment_id"],
            },
        )

    def execute(self, args: dict) -> dict:
        key = args.get("ticket_key", "")
        _require("comment.delete", key)
        result, status = _current_provider.delete_comment(key, args.get("comment_id", ""))
        if "error" in (status or "").lower() and not result:
            return self.error(f"jira_delete_comment: {status}")
        return self.ok(json.dumps(result or {"status": status}, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Self-registration — runs once at import time.
# ---------------------------------------------------------------------------
_TOOLS = [
    _JiraIssueLookupTool(),
    _JiraSearchTool(),
    _JiraGetMyselfTool(),
    _JiraGetTransitionsTool(),
    _JiraValidatePermissionsTool(),
    _JiraCommentTool(),
    _JiraTransitionTool(),
    _JiraAssignTool(),
    _JiraCreateIssueTool(),
    _JiraUpdateFieldsTool(),
    _JiraUpdateCommentTool(),
    _JiraDeleteCommentTool(),
]

for _t in _TOOLS:
    if not is_registered(_t.schema.name):
        register_tool(_t)
