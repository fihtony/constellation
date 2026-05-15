"""Compass Agent tools — Python implementations called by the LLM via ReAct.

Each tool is a BaseTool subclass registered in the global ToolRegistry.
The LLM decides *when* and *in what order* to call these tools based on the
user request and its reasoning.  No Python workflow orchestration here.
"""
from __future__ import annotations

import json
import os
from typing import Any

from framework.tools.base import BaseTool, ToolResult
from framework.tools.registry import get_registry


def _resolve_team_lead_url() -> str:
    """Resolve the Team Lead endpoint via Registry before falling back."""
    try:
        from framework.registry_client import RegistryClient

        discovered = RegistryClient.from_config().discover("team-lead.task.analyze")
        if discovered:
            return discovered
    except Exception:
        pass

    return os.environ.get("TEAM_LEAD_URL", "http://team-lead:8030")

# ---------------------------------------------------------------------------
# Tool: dispatch_development_task
# ---------------------------------------------------------------------------

class DispatchDevelopmentTask(BaseTool):
    """Route a software development task to the Team Lead Agent."""

    name = "dispatch_development_task"
    description = (
        "Dispatch a software development task (implement feature, fix bug, "
        "create PR, review code) to the Team Lead Agent.  Returns a summary "
        "of the result once Team Lead finishes."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "task_description": {
                "type": "string",
                "description": "Full description of what needs to be implemented or fixed.",
            },
            "jira_key": {
                "type": "string",
                "description": "Jira ticket key (e.g. PROJ-123).  Optional.",
            },
            "repo_url": {
                "type": "string",
                "description": "Repository URL if the task involves a specific repo.  Optional.",
            },
            "design_url": {
                "type": "string",
                "description": "Figma or Stitch design URL for UI tasks.  Optional.",
            },
        },
        "required": ["task_description"],
    }

    def execute_sync(
        self,
        task_description: str = "",
        jira_key: str = "",
        repo_url: str = "",
        design_url: str = "",
    ) -> ToolResult:
        import re as _re
        # Sanitize jira_key: MCP tool arguments may contain XML/control characters
        # Extract only the standard Jira key format (e.g. PROJ-123)
        if jira_key:
            _match = _re.search(r"[A-Z][A-Z0-9]+-\d+", jira_key)
            jira_key = _match.group(0) if _match else jira_key.strip().split()[0]

        team_lead_url = _resolve_team_lead_url()
        meta: dict[str, Any] = {}
        if jira_key:
            meta["jiraKey"] = jira_key
        if repo_url:
            meta["repoUrl"] = repo_url
        if design_url:
            meta["designUrl"] = design_url

        try:
            from framework.a2a.client import dispatch_sync
            result = dispatch_sync(
                url=team_lead_url,
                capability="team-lead.task.analyze",
                message_parts=[{"text": task_description}],
                metadata=meta,
            )
            task = result.get("task", result)
            task_state = task.get("status", {}).get("state", "")
            if task_state and task_state != "TASK_STATE_COMPLETED":
                return ToolResult(output=json.dumps({
                    "status": "error",
                    "state": task_state,
                    "message": _extract_status_text(task) or f"Team Lead ended in {task_state}",
                }))
            artifacts = task.get("artifacts", [])
            summary = _extract_text(artifacts) or "Task completed."
            return ToolResult(output=json.dumps({"status": "completed", "summary": summary}))
        except Exception as exc:
            return ToolResult(output=json.dumps({"status": "error", "message": str(exc)}))


class DispatchOfficeTask(BaseTool):
    """Route an office / document task to the Office Agent."""

    name = "dispatch_office_task"
    description = (
        "Dispatch a document or office task (summarize PDF/DOCX, analyze "
        "spreadsheet, organize folder) to the Office Agent."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "task_description": {
                "type": "string",
                "description": "What the user wants done with the document(s).",
            },
            "file_path": {
                "type": "string",
                "description": "Absolute path to the file or folder.  Optional.",
            },
        },
        "required": ["task_description"],
    }

    def execute_sync(
        self,
        task_description: str = "",
        file_path: str = "",
    ) -> ToolResult:
        office_url = os.environ.get("OFFICE_AGENT_URL", "http://office:8060")
        meta: dict[str, Any] = {}
        if file_path:
            meta["filePath"] = file_path

        try:
            from framework.a2a.client import dispatch_sync
            result = dispatch_sync(
                url=office_url,
                capability="office.document.summarize",
                message_parts=[{"text": task_description}],
                metadata=meta,
            )
            task = result.get("task", result)
            artifacts = task.get("artifacts", [])
            summary = _extract_text(artifacts) or "Task completed."
            return ToolResult(output=json.dumps({"status": "completed", "summary": summary}))
        except Exception as exc:
            return ToolResult(output=json.dumps({"status": "error", "message": str(exc)}))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_TOOLS = [DispatchDevelopmentTask(), DispatchOfficeTask()]
TOOL_NAMES = [t.name for t in _TOOLS]


def register_compass_tools() -> None:
    """Register Compass tools into the global ToolRegistry (idempotent)."""
    registry = get_registry()
    existing = {s["function"]["name"] for s in registry.list_schemas()}
    for tool in _TOOLS:
        if tool.name not in existing:
            registry.register(tool)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_text(artifacts: list[dict]) -> str:
    for art in artifacts:
        for part in art.get("parts", []):
            if "text" in part:
                return part["text"]
    return ""


def _extract_status_text(task: dict) -> str:
    parts = task.get("status", {}).get("message", {}).get("parts", [])
    for part in parts:
        if "text" in part:
            return part["text"]
    return ""
