"""Web Dev Agent boundary tools — Jira and SCM operations via A2A dispatch.

These tools allow the Web Dev Agent's workflow nodes to call boundary agents
(Jira, SCM) through the standard A2A protocol.
"""
from __future__ import annotations

import json
import os

from framework.tools.base import BaseTool, ToolResult
from framework.tools.registry import get_registry


def _resolve_agent_url(capability: str, env_var: str = "", default: str = "") -> str:
    """Resolve an agent's URL via Registry discovery, env var, or default."""
    if env_var:
        env_val = os.environ.get(env_var)
        if env_val:
            return env_val

    try:
        from framework.registry_client import RegistryClient
        client = RegistryClient.from_config()
        url = client.discover(capability)
        if url:
            return url
    except Exception:
        pass

    if not default:
        try:
            from framework.config import load_global_config
            cfg = load_global_config()
            services = cfg.get("services") or {}
            if "jira" in capability:
                return services.get("jira_agent_url", "http://jira:8010")
            if "scm" in capability:
                return services.get("scm_agent_url", "http://scm:8020")
        except Exception:
            pass

    return default


def _dispatch_jira(capability: str, ticket_key: str = "", **meta) -> dict:
    """Dispatch a Jira capability via A2A."""
    jira_url = _resolve_agent_url(capability, "JIRA_AGENT_URL", "http://jira:8010")
    try:
        from framework.a2a.client import dispatch_sync
        result = dispatch_sync(
            url=jira_url,
            capability=capability,
            message_parts=[{"text": ticket_key}],
            metadata={"ticketKey": ticket_key, **meta},
        )
        artifacts = result.get("task", result).get("artifacts", [])
        if artifacts:
            parts = artifacts[0].get("parts", [])
            if parts:
                return json.loads(parts[0].get("text", "{}"))
        return {}
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Jira tools for Web Dev
# ---------------------------------------------------------------------------

class JiraTransition(BaseTool):
    """Transition a Jira ticket to a new status."""
    name = "jira_transition"
    description = "Transition a Jira ticket to a new status (e.g. In Progress, In Review)."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string", "description": "Jira ticket key"},
            "transition_name": {"type": "string", "description": "Target transition name"},
        },
        "required": ["ticket_key", "transition_name"],
    }

    def execute_sync(self, ticket_key: str = "", transition_name: str = "") -> ToolResult:
        result = _dispatch_jira(
            "jira.ticket.transition",
            ticket_key=ticket_key,
            transitionName=transition_name,
        )
        return ToolResult(output=json.dumps(result))


class JiraComment(BaseTool):
    """Add a comment to a Jira ticket."""
    name = "jira_comment"
    description = "Add a comment to a Jira ticket."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string", "description": "Jira ticket key"},
            "comment": {"type": "string", "description": "Comment text to add"},
        },
        "required": ["ticket_key", "comment"],
    }

    def execute_sync(self, ticket_key: str = "", comment: str = "") -> ToolResult:
        result = _dispatch_jira(
            "jira.ticket.comment",
            ticket_key=ticket_key,
            comment=comment,
        )
        return ToolResult(output=json.dumps(result))


class JiraUpdate(BaseTool):
    """Update Jira ticket fields."""
    name = "jira_update"
    description = "Update fields on a Jira ticket (e.g. assignee)."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string", "description": "Jira ticket key"},
            "fields": {"type": "object", "description": "Fields to update"},
        },
        "required": ["ticket_key", "fields"],
    }

    def execute_sync(self, ticket_key: str = "", fields: dict | None = None) -> ToolResult:
        result = _dispatch_jira(
            "jira.ticket.update",
            ticket_key=ticket_key,
            fields=fields or {},
        )
        return ToolResult(output=json.dumps(result))


class JiraListTransitions(BaseTool):
    """List available transitions for a Jira ticket."""
    name = "jira_list_transitions"
    description = "List the available status transitions for a Jira ticket."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string", "description": "Jira ticket key"},
        },
        "required": ["ticket_key"],
    }

    def execute_sync(self, ticket_key: str = "") -> ToolResult:
        result = _dispatch_jira("jira.transitions.list", ticket_key=ticket_key)
        return ToolResult(output=json.dumps(result))


class JiraGetTokenUser(BaseTool):
    """Resolve the current Jira token user identity."""
    name = "jira_get_token_user"
    description = "Get the identity of the Jira user associated with the current API token."
    parameters_schema = {"type": "object", "properties": {}, "required": []}

    def execute_sync(self) -> ToolResult:
        result = _dispatch_jira("jira.user.me")
        return ToolResult(output=json.dumps(result))


class JiraListComments(BaseTool):
    """List comments on a Jira ticket."""
    name = "jira_list_comments"
    description = "List comments on a Jira ticket for idempotency checks."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string", "description": "Jira ticket key"},
        },
        "required": ["ticket_key"],
    }

    def execute_sync(self, ticket_key: str = "") -> ToolResult:
        result = _dispatch_jira("jira.comment.list", ticket_key=ticket_key)
        return ToolResult(output=json.dumps(result))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_web_dev_tools():
    """Register all Web Dev boundary tools in the global ToolRegistry."""
    registry = get_registry()
    for tool_cls in (
        JiraTransition,
        JiraComment,
        JiraUpdate,
        JiraListTransitions,
        JiraGetTokenUser,
        JiraListComments,
    ):
        registry.register(tool_cls())
