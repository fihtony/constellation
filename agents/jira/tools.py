"""Jira boundary tools — in-process implementations using JiraRESTProvider.

Registered by JiraAgentAdapter.start() so the global ToolRegistry has live
Jira tools before Team Lead calls register_team_lead_tools() (which is
idempotent and will not override these).
"""
from __future__ import annotations

import json
import os

from pathlib import Path as _Path

from framework.config import load_agent_config as _load_agent_cfg
from framework.devlog import AgentLogger
from framework.tools.base import BaseTool, ToolResult
from framework.tools.registry import get_registry

# Load agent_id from config.yaml — single source of truth for identity
_AGENT_ID: str = _load_agent_cfg(
    _Path(__file__).parent.name.replace("_", "-")
).get("agent_id", _Path(__file__).parent.name.replace("_", "-"))


def _log(task_id: str) -> AgentLogger:
    return AgentLogger(task_id=task_id, agent_name=_AGENT_ID)


def _get_provider():
    from agents.jira.adapter import _make_provider
    backend = os.environ.get("JIRA_BACKEND", "rest")
    return _make_provider(backend)


class FetchJiraTicket(BaseTool):
    name = "fetch_jira_ticket"
    description = "Fetch the details of a Jira ticket (summary, description, status, labels)."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string", "description": "Jira ticket key, e.g. PROJ-123."},
            "task_id": {"type": "string", "description": "Caller task ID for log correlation (optional)."},
        },
        "required": ["ticket_key"],
    }

    def execute_sync(self, ticket_key: str = "", task_id: str = "") -> ToolResult:
        log = _log(task_id)
        log.info("fetch_jira_ticket called", ticket_key=ticket_key)
        data, status = _get_provider().fetch_issue(ticket_key)
        log.debug("fetch_jira_ticket result", status=status)
        return ToolResult(output=json.dumps({"ticket": data, "status": status}))


class JiraTransition(BaseTool):
    name = "jira_transition"
    description = "Transition a Jira ticket to a new status."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string"},
            "transition_name": {"type": "string"},
            "task_id": {"type": "string"},
        },
        "required": ["ticket_key", "transition_name"],
    }

    def execute_sync(self, ticket_key: str = "", transition_name: str = "", task_id: str = "") -> ToolResult:
        log = _log(task_id)
        log.info("jira_transition called", ticket_key=ticket_key, transition=transition_name)
        data, status = _get_provider().transition_issue(ticket_key, transition_name)
        log.debug("jira_transition result", status=status)
        return ToolResult(output=json.dumps({"transitionId": data, "status": status}))


class JiraComment(BaseTool):
    name = "jira_comment"
    description = "Add a comment to a Jira ticket."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string"},
            "comment": {"type": "string"},
            "task_id": {"type": "string"},
        },
        "required": ["ticket_key", "comment"],
    }

    def execute_sync(self, ticket_key: str = "", comment: str = "", task_id: str = "") -> ToolResult:
        log = _log(task_id)
        log.info("jira_comment called", ticket_key=ticket_key, comment_len=len(comment))
        data, status = _get_provider().add_comment(ticket_key, comment)
        log.debug("jira_comment result", status=status)
        return ToolResult(output=json.dumps({"comment": data, "status": status}))


class JiraUpdate(BaseTool):
    name = "jira_update"
    description = "Update fields on a Jira ticket (e.g. assignee, labels)."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string"},
            "fields": {"type": "object"},
            "task_id": {"type": "string"},
        },
        "required": ["ticket_key"],
    }

    def execute_sync(self, ticket_key: str = "", fields: dict | None = None, task_id: str = "") -> ToolResult:
        log = _log(task_id)
        log.info("jira_update called", ticket_key=ticket_key, fields=list((fields or {}).keys()))
        data, status = _get_provider().update_issue_fields(ticket_key, fields or {})
        log.debug("jira_update result", status=status)
        return ToolResult(output=json.dumps({"result": data, "status": status}))


class JiraListTransitions(BaseTool):
    name = "jira_list_transitions"
    description = "List available workflow transitions for a Jira ticket."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string"},
            "task_id": {"type": "string"},
        },
        "required": ["ticket_key"],
    }

    def execute_sync(self, ticket_key: str = "", task_id: str = "") -> ToolResult:
        log = _log(task_id)
        log.debug("jira_list_transitions called", ticket_key=ticket_key)
        data, status = _get_provider().get_transitions(ticket_key)
        names = [t.get("name") for t in data if isinstance(t, dict)]
        log.debug("jira_list_transitions result", status=status, names=names)
        print(f"[jira-tools] get_transitions({ticket_key}): status={status}, names={names}")
        return ToolResult(output=json.dumps({"transitions": data, "status": status}))


class JiraGetTokenUser(BaseTool):
    name = "jira_get_token_user"
    description = "Get the Jira user associated with the current API token."
    parameters_schema = {
        "type": "object",
        "properties": {"task_id": {"type": "string"}},
        "required": [],
    }

    def execute_sync(self, task_id: str = "") -> ToolResult:
        log = _log(task_id)
        log.debug("jira_get_token_user called")
        data, status = _get_provider().get_myself()
        log.debug("jira_get_token_user result", status=status)
        return ToolResult(output=json.dumps({"user": data, "status": status}))


class JiraListComments(BaseTool):
    name = "jira_list_comments"
    description = "List comments on a Jira ticket (for idempotency checks)."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string"},
            "task_id": {"type": "string"},
        },
        "required": ["ticket_key"],
    }

    def execute_sync(self, ticket_key: str = "", task_id: str = "") -> ToolResult:
        log = _log(task_id)
        log.debug("jira_list_comments called", ticket_key=ticket_key)
        data, status = _get_provider().list_comments(ticket_key)
        log.debug("jira_list_comments result", status=status, count=len(data) if isinstance(data, list) else 0)
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
