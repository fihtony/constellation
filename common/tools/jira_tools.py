"""Jira tools for agentic runtimes.

Provides tools that allow an agentic runtime to interact with the Jira Agent
via A2A HTTP calls.  Import this module to register all Jira tools.

Uses the standard A2A ``POST /message:send`` envelope with
``configuration.returnImmediately`` and polls ``/tasks/{id}`` until terminal.
"""

from __future__ import annotations

import json
import os
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from common.tools.agent_discovery import discover_capability_url
from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import is_registered, register_tool

_REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")
_ACK_TIMEOUT = int(os.environ.get("A2A_ACK_TIMEOUT_SECONDS", "15"))
_TASK_TIMEOUT = int(os.environ.get("A2A_TASK_TIMEOUT_SECONDS", "60"))
_TASK_POLL_INTERVAL = float(os.environ.get("A2A_TASK_POLL_INTERVAL_SECONDS", "1.0"))


def _discover_jira_url(capability: str) -> str | None:
    try:
        return discover_capability_url(_REGISTRY_URL, capability)
    except Exception:  # noqa: BLE001
        return None


def _a2a_send(agent_url: str, capability: str, params: dict) -> dict:
    """Send an A2A message and poll until completion.

    Uses the standard A2A envelope (``message`` + ``configuration``)
    instead of JSON-RPC.
    """
    agent_url = agent_url.rstrip("/")
    payload = {
        "message": {
            "role": "user",
            "parts": [{"text": json.dumps(params, ensure_ascii=False)}],
            "metadata": {"requestedCapability": capability, **params},
        },
        "configuration": {"returnImmediately": True},
    }
    req = Request(
        f"{agent_url}/message:send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=_ACK_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    task = data.get("task") or {}
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        return data
    if _is_terminal_task(task):
        return task
    return _poll_task(agent_url, task_id, timeout=_TASK_TIMEOUT)


def _is_terminal_task(task: dict) -> bool:
    state = str(((task or {}).get("status") or {}).get("state") or "")
    return state in {
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_INPUT_REQUIRED",
    }


def _poll_task(agent_url: str, task_id: str, *, timeout: int) -> dict:
    deadline = time.time() + max(1, timeout)
    last_task: dict = {"id": task_id}
    while time.time() < deadline:
        req = Request(
            f"{agent_url}/tasks/{task_id}",
            headers={"Accept": "application/json"},
        )
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        task = data.get("task") or {}
        if task:
            last_task = task
        if _is_terminal_task(task):
            return task
        time.sleep(_TASK_POLL_INTERVAL)

    status = last_task.setdefault("status", {})
    status.setdefault("state", "TASK_STATE_FAILED")
    status.setdefault("message", {"parts": [{"text": f"Jira task {task_id} timed out"}]})
    return last_task


def _extract_result_text(task: dict) -> str:
    """Extract text result from a completed A2A task."""
    for artifact in task.get("artifacts") or []:
        for part in artifact.get("parts") or []:
            if "text" in part:
                return part["text"]
    status = task.get("status") or {}
    msg = status.get("message") or {}
    for part in msg.get("parts") or []:
        if "text" in part:
            return part["text"]
    return json.dumps(task, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


class JiraGetTicketTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_get_ticket",
            description="Fetch Jira ticket details by key (e.g. PROJ-123).",
            input_schema={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Jira ticket key, e.g. PROJ-123",
                    }
                },
                "required": ["key"],
            },
        )

    def execute(self, args: dict) -> dict:
        key = args.get("key", "").strip()
        if not key:
            return self.error("Missing required argument: key")
        url = _discover_jira_url("jira.ticket.fetch")
        if not url:
            return self.error("Jira Agent is not available (capability jira.ticket.fetch not found).")
        try:
            task = _a2a_send(url, "jira.ticket.fetch", {"ticketKey": key})
            return self.ok(_extract_result_text(task))
        except (URLError, OSError) as exc:
            return self.error(f"Failed to fetch Jira ticket {key}: {exc}")


class JiraAddCommentTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_add_comment",
            description="Add a comment to a Jira ticket.",
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Jira ticket key"},
                    "comment": {"type": "string", "description": "Comment text to add"},
                },
                "required": ["key", "comment"],
            },
        )

    def execute(self, args: dict) -> dict:
        key = args.get("key", "").strip()
        comment = args.get("comment", "").strip()
        if not key or not comment:
            return self.error("Both 'key' and 'comment' are required.")
        url = _discover_jira_url("jira.comment.add")
        if not url:
            return self.error("Jira Agent is not available (capability jira.comment.add not found).")
        try:
            task = _a2a_send(url, "jira.comment.add", {"ticketKey": key, "comment": comment})
            return self.ok(_extract_result_text(task))
        except (URLError, OSError) as exc:
            return self.error(f"Failed to add comment to {key}: {exc}")


class JiraSearchTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_search",
            description="Search Jira issues using JQL (Jira Query Language).",
            input_schema={
                "type": "object",
                "properties": {
                    "jql": {"type": "string", "description": "JQL query string"},
                    "max_results": {"type": "integer", "description": "Max results (default 20)"},
                    "fields": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Fields to return (optional)",
                    },
                },
                "required": ["jql"],
            },
        )

    def execute(self, args: dict) -> dict:
        jql = args.get("jql", "").strip()
        if not jql:
            return self.error("Missing required argument: jql")
        url = _discover_jira_url("jira.issue.search")
        if not url:
            return self.error("Jira Agent is not available (capability jira.issue.search not found).")
        try:
            params: dict = {"jql": jql}
            if args.get("max_results"):
                params["max_results"] = args["max_results"]
            if args.get("fields"):
                params["fields"] = args["fields"]
            task = _a2a_send(url, "jira.issue.search", params)
            return self.ok(_extract_result_text(task))
        except (URLError, OSError) as exc:
            return self.error(f"Jira search failed: {exc}")


class JiraTransitionTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_transition",
            description="Transition a Jira ticket to a new status (e.g. 'In Review', 'Done').",
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Jira ticket key"},
                    "transition_name": {"type": "string", "description": "Target status name"},
                },
                "required": ["key", "transition_name"],
            },
        )

    def execute(self, args: dict) -> dict:
        key = args.get("key", "").strip()
        transition_name = args.get("transition_name", "").strip()
        if not key or not transition_name:
            return self.error("Both 'key' and 'transition_name' are required.")
        url = _discover_jira_url("jira.ticket.transition")
        if not url:
            return self.error("Jira Agent is not available (capability jira.ticket.transition not found).")
        try:
            task = _a2a_send(url, "jira.ticket.transition", {"ticketKey": key, "transition_name": transition_name})
            return self.ok(_extract_result_text(task))
        except (URLError, OSError) as exc:
            return self.error(f"Failed to transition {key}: {exc}")


class JiraAssignTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_assign",
            description="Assign a Jira ticket to a user by account ID.",
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Jira ticket key"},
                    "account_id": {"type": "string", "description": "Jira account ID of the assignee"},
                },
                "required": ["key", "account_id"],
            },
        )

    def execute(self, args: dict) -> dict:
        key = args.get("key", "").strip()
        account_id = args.get("account_id", "").strip()
        if not key or not account_id:
            return self.error("Both 'key' and 'account_id' are required.")
        url = _discover_jira_url("jira.ticket.assignee")
        if not url:
            return self.error("Jira Agent is not available (capability jira.ticket.assignee not found).")
        try:
            task = _a2a_send(url, "jira.ticket.assignee", {"ticketKey": key, "account_id": account_id})
            return self.ok(_extract_result_text(task))
        except (URLError, OSError) as exc:
            return self.error(f"Failed to assign {key}: {exc}")


class JiraGetTransitionsTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_get_transitions",
            description="Get available status transitions for a Jira ticket.",
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Jira ticket key"},
                },
                "required": ["key"],
            },
        )

    def execute(self, args: dict) -> dict:
        key = args.get("key", "").strip()
        if not key:
            return self.error("Missing required argument: key")
        url = _discover_jira_url("jira.ticket.fetch")
        if not url:
            return self.error("Jira Agent is not available.")
        try:
            task = _a2a_send(url, "jira.ticket.fetch", {"ticketKey": key, "action": "get_transitions"})
            return self.ok(_extract_result_text(task))
        except (URLError, OSError) as exc:
            return self.error(f"Failed to get transitions for {key}: {exc}")


class JiraGetMyselfTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_get_myself",
            description="Get the currently authenticated Jira user's info.",
            input_schema={"type": "object", "properties": {}},
        )

    def execute(self, args: dict) -> dict:
        url = _discover_jira_url("jira.user.myself")
        if not url:
            return self.error("Jira Agent is not available (capability jira.user.myself not found).")
        try:
            task = _a2a_send(url, "jira.user.myself", {})
            return self.ok(_extract_result_text(task))
        except (URLError, OSError) as exc:
            return self.error(f"Failed to get Jira user info: {exc}")


class JiraCreateIssueTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_create_issue",
            description="Create a new Jira issue in a project.",
            input_schema={
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project key (e.g. PROJ)"},
                    "summary": {"type": "string", "description": "Issue summary"},
                    "description": {"type": "string", "description": "Issue description (optional)"},
                    "issue_type": {"type": "string", "description": "Issue type (default: Task)"},
                    "fields": {"type": "object", "description": "Additional fields as key-value pairs"},
                },
                "required": ["project", "summary"],
            },
        )

    def execute(self, args: dict) -> dict:
        project = args.get("project", "").strip()
        summary = args.get("summary", "").strip()
        if not project or not summary:
            return self.error("Both 'project' and 'summary' are required.")
        url = _discover_jira_url("jira.issue.create")
        if not url:
            return self.error("Jira Agent is not available (capability jira.issue.create not found).")
        try:
            params: dict = {"project": project, "summary": summary}
            if args.get("description"):
                params["description"] = args["description"]
            if args.get("issue_type"):
                params["issue_type"] = args["issue_type"]
            if args.get("fields"):
                params["fields"] = args["fields"]
            task = _a2a_send(url, "jira.issue.create", params)
            return self.ok(_extract_result_text(task))
        except (URLError, OSError) as exc:
            return self.error(f"Failed to create Jira issue: {exc}")


class JiraUpdateFieldsTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_update_fields",
            description="Update fields on a Jira ticket.",
            input_schema={
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Jira ticket key"},
                    "fields": {"type": "object", "description": "Fields to update as key-value pairs"},
                },
                "required": ["key", "fields"],
            },
        )

    def execute(self, args: dict) -> dict:
        key = args.get("key", "").strip()
        fields = args.get("fields")
        if not key or not fields:
            return self.error("Both 'key' and 'fields' are required.")
        url = _discover_jira_url("jira.issue.update")
        if not url:
            return self.error("Jira Agent is not available (capability jira.issue.update not found).")
        try:
            task = _a2a_send(url, "jira.issue.update", {"ticketKey": key, "fields": fields})
            return self.ok(_extract_result_text(task))
        except (URLError, OSError) as exc:
            return self.error(f"Failed to update {key}: {exc}")


class JiraValidatePermissionsTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="jira_validate_permissions",
            description="Check whether a specific Jira action is permitted for a ticket.",
            input_schema={
                "type": "object",
                "properties": {
                    "action": {"type": "string", "description": "Action to check (e.g. ticket.write, comment.add)"},
                    "target": {"type": "string", "description": "Target (e.g. PROJ-123)"},
                },
                "required": ["action", "target"],
            },
        )

    def execute(self, args: dict) -> dict:
        action = args.get("action", "").strip()
        target = args.get("target", "").strip()
        if not action or not target:
            return self.error("Both 'action' and 'target' are required.")
        url = _discover_jira_url("jira.ticket.fetch")
        if not url:
            return self.error("Jira Agent is not available.")
        try:
            task = _a2a_send(url, "jira.ticket.fetch", {"action": "validate_permissions", "target_action": action, "target": target})
            return self.ok(_extract_result_text(task))
        except (URLError, OSError) as exc:
            return self.error(f"Failed to validate Jira permissions: {exc}")


# ---------------------------------------------------------------------------
# Register all tools (skip if already registered — Jira Agent's own
# provider_tools.py may have registered same-name local versions first)
# ---------------------------------------------------------------------------

_BOUNDARY_TOOLS = [
    JiraGetTicketTool(),
    JiraAddCommentTool(),
    JiraSearchTool(),
    JiraTransitionTool(),
    JiraAssignTool(),
    JiraGetTransitionsTool(),
    JiraGetMyselfTool(),
    JiraCreateIssueTool(),
    JiraUpdateFieldsTool(),
    JiraValidatePermissionsTool(),
]

for _t in _BOUNDARY_TOOLS:
    if not is_registered(_t.schema.name):
        register_tool(_t)
