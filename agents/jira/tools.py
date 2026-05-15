"""Jira boundary tools — in-process implementations using JiraRESTProvider.

Registered by JiraAgentAdapter.start() so the global ToolRegistry has live
Jira tools before Team Lead calls register_team_lead_tools() (which is
idempotent and will not override these).
"""
from __future__ import annotations

import json
import os

from framework.tools.base import BaseTool, ToolResult
from framework.tools.registry import get_registry


def _get_provider():
    from agents.jira.adapter import _make_provider
    backend = os.environ.get("JIRA_BACKEND", "rest")
    return _make_provider(backend)


class FetchJiraTicket(BaseTool):
    name = "fetch_jira_ticket"
    description = "Fetch the details of a Jira ticket (summary, description, status, labels)."
    parameters_schema = {
        "type": "object",
        "properties": {"ticket_key": {"type": "string", "description": "Jira ticket key, e.g. PROJ-123."}},
        "required": ["ticket_key"],
    }

    def execute_sync(self, ticket_key: str = "") -> ToolResult:
        data, status = _get_provider().fetch_issue(ticket_key)
        return ToolResult(output=json.dumps({"ticket": data, "status": status}))


class JiraTransition(BaseTool):
    name = "jira_transition"
    description = "Transition a Jira ticket to a new status."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string"},
            "transition_name": {"type": "string"},
        },
        "required": ["ticket_key", "transition_name"],
    }

    def execute_sync(self, ticket_key: str = "", transition_name: str = "") -> ToolResult:
        data, status = _get_provider().transition_issue(ticket_key, transition_name)
        return ToolResult(output=json.dumps({"transitionId": data, "status": status}))


class JiraComment(BaseTool):
    name = "jira_comment"
    description = "Add a comment to a Jira ticket."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string"},
            "comment": {"type": "string"},
        },
        "required": ["ticket_key", "comment"],
    }

    def execute_sync(self, ticket_key: str = "", comment: str = "") -> ToolResult:
        data, status = _get_provider().add_comment(ticket_key, comment)
        return ToolResult(output=json.dumps({"comment": data, "status": status}))


class JiraUpdate(BaseTool):
    name = "jira_update"
    description = "Update fields on a Jira ticket (e.g. assignee, labels)."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string"},
            "fields": {"type": "object"},
        },
        "required": ["ticket_key"],
    }

    def execute_sync(self, ticket_key: str = "", fields: dict | None = None) -> ToolResult:
        data, status = _get_provider().update_issue_fields(ticket_key, fields or {})
        return ToolResult(output=json.dumps({"result": data, "status": status}))


class JiraListTransitions(BaseTool):
    name = "jira_list_transitions"
    description = "List available workflow transitions for a Jira ticket."
    parameters_schema = {
        "type": "object",
        "properties": {"ticket_key": {"type": "string"}},
        "required": ["ticket_key"],
    }

    def execute_sync(self, ticket_key: str = "") -> ToolResult:
        data, status = _get_provider().get_transitions(ticket_key)
        names = [t.get("name") for t in data if isinstance(t, dict)]
        print(f"[jira-tools] get_transitions({ticket_key}): status={status}, names={names}")
        return ToolResult(output=json.dumps({"transitions": data, "status": status}))


class JiraGetTokenUser(BaseTool):
    name = "jira_get_token_user"
    description = "Get the Jira user associated with the current API token."
    parameters_schema = {"type": "object", "properties": {}, "required": []}

    def execute_sync(self) -> ToolResult:
        data, status = _get_provider().get_myself()
        return ToolResult(output=json.dumps({"user": data, "status": status}))


class JiraListComments(BaseTool):
    name = "jira_list_comments"
    description = "List comments on a Jira ticket (for idempotency checks)."
    parameters_schema = {
        "type": "object",
        "properties": {"ticket_key": {"type": "string"}},
        "required": ["ticket_key"],
    }

    def execute_sync(self, ticket_key: str = "") -> ToolResult:
        data, status = _get_provider().list_comments(ticket_key)
        return ToolResult(output=json.dumps({"comments": data, "status": status}))


_TOOLS = [
    FetchJiraTicket(),
    JiraTransition(),
    JiraComment(),
    JiraUpdate(),
    JiraListTransitions(),
    JiraGetTokenUser(),
    JiraListComments(),
]


def register_jira_tools() -> None:
    """Register in-process Jira tools (idempotent, won't override existing)."""
    registry = get_registry()
    existing = {s["function"]["name"] for s in registry.list_schemas()}
    for tool in _TOOLS:
        if tool.name not in existing:
            registry.register(tool)
    print(f"[jira-tools] Registered: {[t.name for t in _TOOLS if t.name not in existing]}")
