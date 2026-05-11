"""Team Lead Agent tools — Python implementations called by the LLM via ReAct.

The LLM decides when and in what order to call these tools.  No predefined
workflow orchestration — intelligence comes from the LLM + instructions.
"""
from __future__ import annotations

import json
import os
from typing import Any

from framework.tools.base import BaseTool, ToolResult
from framework.tools.registry import get_registry


def _discover_via_registry(capability: str) -> str:
    """Look up the first healthy instance URL for *capability* from the Registry.

    Calls ``GET <REGISTRY_URL>/query?capability=<capability>`` and returns the
    ``serviceUrl`` of the first active instance.  Returns an empty string if
    the registry is unreachable or has no matching instance.
    """
    try:
        import urllib.request
        registry_url = (
            os.environ.get("REGISTRY_URL")
            or os.environ.get("CONSTELLATION_REGISTRY_URL")
            or ""
        )
        if not registry_url:
            from framework.config import load_global_config
            cfg = load_global_config()
            registry_url = (cfg.get("registry") or {}).get("url", "")
        if not registry_url:
            return ""

        url = f"{registry_url.rstrip('/')}/query?capability={capability}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = json.loads(resp.read().decode("utf-8"))

        # Registry returns a list of instance objects or a dict with "instances"
        instances = body if isinstance(body, list) else body.get("instances", [])
        for inst in instances:
            svc_url = inst.get("serviceUrl") or inst.get("service_url") or ""
            if svc_url:
                return svc_url
    except Exception:
        pass  # registry unreachable — fall through to defaults
    return ""


def _resolve_agent_url(env_var: str, config_key: str, default: str, capability: str = "") -> str:
    """Resolve an agent's URL with a defined priority order:

    1. Environment variable (``env_var``) — highest priority, deployment override.
    2. Capability Registry discovery via ``/query?capability=<capability>``.
    3. Global config ``services.<config_key>`` from constellation.yaml.
    4. Hardcoded ``default`` — last-resort fallback for bare-metal / test runs.
    """
    # Priority 1: explicit env var override
    env_val = os.environ.get(env_var)
    if env_val:
        return env_val

    # Priority 2: Registry capability discovery (preferred at runtime)
    if capability:
        discovered = _discover_via_registry(capability)
        if discovered:
            return discovered

    # Priority 3: global config services section
    try:
        from framework.config import load_global_config
        global_cfg = load_global_config()
        services = global_cfg.get("services") or {}
        cfg_url = services.get(config_key, "").strip()
        if cfg_url:
            return cfg_url
    except Exception:
        pass

    return default


# ---------------------------------------------------------------------------
# Tool: fetch_jira_ticket
# ---------------------------------------------------------------------------

class FetchJiraTicket(BaseTool):
    """Fetch a Jira ticket for context before planning."""

    name = "fetch_jira_ticket"
    description = "Fetch the details of a Jira ticket (summary, description, status, labels)."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {
                "type": "string",
                "description": "Jira ticket key, e.g. PROJ-123.",
            }
        },
        "required": ["ticket_key"],
    }

    def execute_sync(self, ticket_key: str = "") -> ToolResult:
        jira_url = _resolve_agent_url("JIRA_AGENT_URL", "jira_agent_url", "http://jira:8010", "jira.ticket.fetch")
        try:
            from framework.a2a.client import dispatch_sync
            result = dispatch_sync(
                url=jira_url,
                capability="jira.ticket.fetch",
                message_parts=[{"text": ticket_key}],
                metadata={"ticketKey": ticket_key},
            )
            artifacts = result.get("task", result).get("artifacts", [])
            payload = _first_artifact_json(artifacts)
            return ToolResult(output=json.dumps(payload))
        except Exception as exc:
            return ToolResult(output=json.dumps({"error": str(exc), "ticketKey": ticket_key}))


# ---------------------------------------------------------------------------
# Tool: fetch_design
# ---------------------------------------------------------------------------

class FetchDesign(BaseTool):
    """Fetch design context from Figma or Google Stitch."""

    name = "fetch_design"
    description = (
        "Fetch design specification from a Figma URL or a Google Stitch project. "
        "Provide either figma_url or stitch_project_id."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "figma_url": {
                "type": "string",
                "description": "Full Figma file URL.",
            },
            "stitch_project_id": {
                "type": "string",
                "description": "Google Stitch project ID.",
            },
            "screen_name": {
                "type": "string",
                "description": "Screen name for Stitch (optional).",
            },
        },
        "required": [],
    }

    def execute_sync(
        self,
        figma_url: str = "",
        stitch_project_id: str = "",
        screen_name: str = "",
    ) -> ToolResult:
        ui_url = _resolve_agent_url("UI_DESIGN_AGENT_URL", "ui_design_agent_url", "http://ui-design:8040", "figma.file.fetch")
        try:
            from framework.a2a.client import dispatch_sync
            if figma_url:
                capability = "figma.file.fetch"
                meta: dict[str, Any] = {"figmaUrl": figma_url}
                text = figma_url
            elif stitch_project_id:
                capability = "stitch.screen.fetch" if screen_name else "stitch.screens.list"
                meta = {
                    "stitchProjectId": stitch_project_id,
                    "screenName": screen_name,
                }
                text = stitch_project_id
            else:
                return ToolResult(output=json.dumps({"error": "No design URL or project ID provided"}))

            result = dispatch_sync(
                url=ui_url,
                capability=capability,
                message_parts=[{"text": text}],
                metadata=meta,
            )
            artifacts = result.get("task", result).get("artifacts", [])
            payload = _first_artifact_json(artifacts)
            return ToolResult(output=json.dumps(payload))
        except Exception as exc:
            return ToolResult(output=json.dumps({"error": str(exc)}))


# ---------------------------------------------------------------------------
# Tool: dispatch_web_dev
# ---------------------------------------------------------------------------

class DispatchWebDev(BaseTool):
    """Dispatch a web development task to the Web Dev Agent."""

    name = "dispatch_web_dev"
    description = (
        "Dispatch a web development implementation task to the Web Dev Agent. "
        "Include all gathered context: Jira ticket details, design spec, repo URL."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "task_description": {
                "type": "string",
                "description": "Full implementation task description with context.",
            },
            "jira_context": {
                "type": "object",
                "description": "Jira ticket data (from fetch_jira_ticket). Optional.",
            },
            "design_context": {
                "type": "object",
                "description": "Design spec data (from fetch_design). Optional.",
            },
            "repo_url": {
                "type": "string",
                "description": "Git repository URL. Optional.",
            },
            "revision_feedback": {
                "type": "string",
                "description": "Code review rejection reason for revision. Optional.",
            },
        },
        "required": ["task_description"],
    }

    def execute_sync(
        self,
        task_description: str = "",
        jira_context: dict | None = None,
        design_context: dict | None = None,
        repo_url: str = "",
        revision_feedback: str = "",
    ) -> ToolResult:
        web_dev_url = _resolve_agent_url("WEB_DEV_AGENT_URL", "web_dev_agent_url", "http://web-dev:8050", "web-dev.task.execute")
        meta: dict[str, Any] = {}
        if jira_context:
            meta["jiraContext"] = jira_context
        if design_context:
            meta["designContext"] = design_context
        if repo_url:
            meta["repoUrl"] = repo_url
        if revision_feedback:
            meta["revisionFeedback"] = revision_feedback

        try:
            from framework.a2a.client import dispatch_sync
            result = dispatch_sync(
                url=web_dev_url,
                capability="web-dev.task.execute",
                message_parts=[{"text": task_description}],
                metadata=meta,
                timeout=600,
            )
            task = result.get("task", result)
            artifacts = task.get("artifacts", [])
            summary = _extract_text(artifacts) or "Dev task completed."
            pr_url = _find_metadata(artifacts, "prUrl")
            branch = _find_metadata(artifacts, "branch")
            return ToolResult(output=json.dumps({
                "status": "completed",
                "summary": summary,
                "prUrl": pr_url,
                "branch": branch,
            }))
        except Exception as exc:
            return ToolResult(output=json.dumps({"status": "error", "message": str(exc)}))


# ---------------------------------------------------------------------------
# Tool: dispatch_code_review
# ---------------------------------------------------------------------------

class DispatchCodeReview(BaseTool):
    """Dispatch the dev agent output to the Code Review Agent."""

    name = "dispatch_code_review"
    description = (
        "Send the dev agent's output (PR URL or diff) to the Code Review Agent "
        "for quality, security, and requirements validation."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "pr_url": {
                "type": "string",
                "description": "Pull request URL to review.",
            },
            "diff_summary": {
                "type": "string",
                "description": "Summary of the changes made.",
            },
            "requirements": {
                "type": "string",
                "description": "Original requirements to check compliance against.",
            },
        },
        "required": [],
    }

    def execute_sync(
        self,
        pr_url: str = "",
        diff_summary: str = "",
        requirements: str = "",
    ) -> ToolResult:
        review_url = _resolve_agent_url("CODE_REVIEW_AGENT_URL", "code_review_agent_url", "http://code-review:8050", "review.code.check")
        meta: dict[str, Any] = {}
        if pr_url:
            meta["prUrl"] = pr_url
        if requirements:
            meta["originalRequirements"] = requirements

        try:
            from framework.a2a.client import dispatch_sync
            result = dispatch_sync(
                url=review_url,
                capability="review.code.check",
                message_parts=[{"text": diff_summary or pr_url}],
                metadata=meta,
                timeout=300,
            )
            artifacts = result.get("task", result).get("artifacts", [])
            payload = _first_artifact_json(artifacts) or {"verdict": "unknown"}
            return ToolResult(output=json.dumps(payload))
        except Exception as exc:
            return ToolResult(output=json.dumps({"verdict": "error", "message": str(exc)}))


# ---------------------------------------------------------------------------
# Tool: request_clarification
# ---------------------------------------------------------------------------

class RequestClarification(BaseTool):
    """Ask the user for clarification when the request is ambiguous."""

    name = "request_clarification"
    description = (
        "Ask the user a question to clarify missing or ambiguous information "
        "before proceeding.  Returns the user's answer."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "The question to ask the user.",
            }
        },
        "required": ["question"],
    }

    def execute_sync(self, question: str = "") -> ToolResult:
        # In production this triggers INPUT_REQUIRED via the workflow interrupt mechanism.
        # For now, signal the LLM that user input is needed.
        return ToolResult(output=json.dumps({
            "status": "input_required",
            "question": question,
            "instruction": (
                "Pause and present this question to the user before continuing."
            ),
        }))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

_TOOLS = [
    FetchJiraTicket(),
    FetchDesign(),
    DispatchWebDev(),
    DispatchCodeReview(),
    RequestClarification(),
]
TOOL_NAMES = [t.name for t in _TOOLS]


def register_team_lead_tools() -> None:
    """Register Team Lead tools into the global ToolRegistry (idempotent)."""
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


def _first_artifact_json(artifacts: list[dict]) -> dict:
    text = _extract_text(artifacts)
    if text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"raw": text}
    return {}


def _find_metadata(artifacts: list[dict], key: str) -> str:
    for art in artifacts:
        val = art.get("metadata", {}).get(key)
        if val:
            return val
    return ""
