"""Team Lead Agent tools — Python implementations called by the LLM via ReAct.

The LLM decides when and in what order to call these tools.  No predefined
workflow orchestration — intelligence comes from the LLM + instructions.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from typing import Any

from framework.launcher import get_launcher
from framework.tools.base import BaseTool, ToolResult
from framework.tools.registry import get_registry


def _discover_via_registry(capability: str) -> str:
    """Look up the first healthy instance URL for *capability* from the Registry.

    Uses the v2 RegistryClient abstraction for cached, config-aware discovery.
    """
    try:
        from framework.registry_client import RegistryClient
        client = RegistryClient.from_config()
        return client.discover(capability)
    except Exception:
        pass
    return ""


def _resolve_agent_url(env_var: str, config_key: str, default: str, capability: str = "") -> str:
    """Resolve an agent's live URL via Registry only."""
    if capability:
        discovered = _discover_via_registry(capability)
        if discovered:
            return discovered
    return ""


def _capability_definition(capability: str) -> dict[str, Any]:
    try:
        from framework.registry_client import RegistryClient

        return RegistryClient.from_config().get_capability_definition(capability) or {}
    except Exception:
        return {}


def _is_per_task_definition(definition: dict[str, Any]) -> bool:
    execution_mode = str(
        definition.get("execution_mode") or definition.get("executionMode") or ""
    ).strip().lower()
    return execution_mode == "per-task" or bool(definition.get("launch_spec") or definition.get("launchSpec"))


def _wait_for_agent_ready(base_url: str, timeout: int = 30) -> None:
    deadline = time.time() + timeout
    health_url = f"{base_url.rstrip('/')}/health"
    last_error = "agent did not become ready"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(health_url, timeout=2):
                return
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            time.sleep(0.5)
    raise TimeoutError(f"Timed out waiting for launched agent: {last_error}")


def _downstream_timeout_seconds(kind: str) -> int:
    env_map = {
        "web_dev": ("TEAM_LEAD_WEB_DEV_TIMEOUT_SECONDS", 3600),
        "code_review": ("TEAM_LEAD_CODE_REVIEW_TIMEOUT_SECONDS", 1200),
    }
    env_name, default = env_map.get(kind, ("TEAM_LEAD_DOWNSTREAM_TIMEOUT_SECONDS", 600))
    raw_value = os.environ.get(env_name, "").strip()
    if not raw_value:
        return default
    try:
        value = int(raw_value)
        return value if value > 0 else default
    except ValueError:
        return default


def _derive_launch_task_id(
    orchestrator_task_id: str = "",
    task_id: str = "",
    workspace_path: str = "",
) -> str:
    explicit = (orchestrator_task_id or task_id or "").strip()
    if explicit:
        return explicit

    workspace = (workspace_path or "").strip().rstrip("/")
    if workspace:
        leaf = os.path.basename(workspace)
        if leaf:
            return leaf
    return ""


def _dispatch_via_launcher(
    definition: dict[str, Any],
    *,
    capability: str,
    launch_task_id: str,
    message_parts: list[dict[str, Any]],
    metadata: dict[str, Any],
    timeout: int,
) -> dict:
    from framework.a2a.client import dispatch_sync

    launcher = get_launcher()
    launch = launcher.launch_instance(definition, launch_task_id or "per-task-agent")
    agent_id = str(definition.get("agent_id") or definition.get("agentId") or capability).strip() or capability

    try:
        _wait_for_agent_ready(launch["service_url"])
        return dispatch_sync(
            url=launch["service_url"],
            capability=capability,
            message_parts=message_parts,
            metadata=metadata,
            timeout=timeout,
        )
    finally:
        try:
            launcher.destroy_instance(agent_id, launch["container_name"])
        except Exception:
            pass


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

    def execute_sync(
        self,
        ticket_key: str = "",
        task_id: str = "",
        workspace_path: str = "",
        **_: Any,
    ) -> ToolResult:
        jira_url = _resolve_agent_url("JIRA_AGENT_URL", "jira_agent_url", "http://jira:8010", "jira.ticket.fetch")
        if not jira_url:
            return ToolResult(output=json.dumps({"error": "No registered Jira instance was found in the registry.", "ticketKey": ticket_key}))
        try:
            from framework.a2a.client import dispatch_sync
            metadata: dict[str, Any] = {"ticketKey": ticket_key}
            if task_id:
                metadata["taskId"] = task_id
            if workspace_path:
                metadata["workspacePath"] = workspace_path
            result = dispatch_sync(
                url=jira_url,
                capability="jira.ticket.fetch",
                message_parts=[{"text": ticket_key}],
                metadata=metadata,
            )
            task = result.get("task", result)
            if _task_state(task) != "TASK_STATE_COMPLETED":
                return ToolResult(output=json.dumps({"error": _task_error(task, f"Jira fetch failed: {ticket_key}")}))
            artifacts = task.get("artifacts", [])
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
            "stitch_screen_id": {
                "type": "string",
                "description": "Google Stitch screen ID (optional, fetches a specific screen).",
            },
            "screen_name": {
                "type": "string",
                "description": "Screen name for Stitch (optional, used when screen_id is unknown).",
            },
        },
        "required": [],
    }

    def execute_sync(
        self,
        figma_url: str = "",
        stitch_project_id: str = "",
        stitch_screen_id: str = "",
        screen_name: str = "",
        task_id: str = "",
        workspace_path: str = "",
        **_: Any,
    ) -> ToolResult:
        ui_url = _resolve_agent_url("UI_DESIGN_AGENT_URL", "ui_design_agent_url", "http://ui-design:8040", "figma.file.fetch")
        if not ui_url:
            return ToolResult(output=json.dumps({"error": "No registered UI Design instance was found in the registry."}))
        try:
            from framework.a2a.client import dispatch_sync
            if figma_url:
                capability = "figma.file.fetch"
                meta: dict[str, Any] = {"figmaUrl": figma_url}
                text = figma_url
            elif stitch_project_id:
                if stitch_screen_id or screen_name:
                    capability = "stitch.screen.fetch"
                else:
                    capability = "stitch.screens.list"
                meta = {
                    "stitchProjectId": stitch_project_id,
                    "stitchScreenId": stitch_screen_id,
                    "screenName": screen_name,
                }
                text = stitch_project_id
            else:
                return ToolResult(output=json.dumps({"error": "No design URL or project ID provided"}))

            if task_id:
                meta["taskId"] = task_id
            if workspace_path:
                meta["workspacePath"] = workspace_path

            result = dispatch_sync(
                url=ui_url,
                capability=capability,
                message_parts=[{"text": text}],
                metadata=meta,
            )
            task = result.get("task", result)
            if _task_state(task) != "TASK_STATE_COMPLETED":
                return ToolResult(output=json.dumps({"error": _task_error(task, "Design fetch failed")}))
            artifacts = task.get("artifacts", [])
            payload = _first_artifact_json(artifacts)
            if not payload.get("local_folder"):
                payload["local_folder"] = _find_metadata(artifacts, "localFolder")
            if not payload.get("design_code_path"):
                payload["design_code_path"] = _find_metadata(artifacts, "designCodePath")
            if not payload.get("design_md_path"):
                payload["design_md_path"] = _find_metadata(artifacts, "designMdPath")
            if not payload.get("design_screen_path"):
                payload["design_screen_path"] = _find_metadata(artifacts, "designScreenPath")
            if not payload.get("files"):
                files_json = _find_metadata(artifacts, "filesJson")
                if files_json:
                    try:
                        payload["files"] = json.loads(files_json)
                    except json.JSONDecodeError:
                        pass
            return ToolResult(output=json.dumps(payload))
        except Exception as exc:
            return ToolResult(output=json.dumps({"error": str(exc)}))


# ---------------------------------------------------------------------------
# Tool: clone_repo
# ---------------------------------------------------------------------------

class CloneRepo(BaseTool):
    """Clone a code repository to a target path via SCM Agent."""

    name = "clone_repo"
    description = "Clone a Git repository to a local workspace path via the SCM Agent."
    parameters_schema = {
        "type": "object",
        "properties": {
            "repo_url": {
                "type": "string",
                "description": "Git repository URL to clone.",
            },
            "target_path": {
                "type": "string",
                "description": "Local filesystem path to clone into.",
            },
        },
        "required": ["repo_url", "target_path"],
    }

    def execute_sync(
        self,
        repo_url: str = "",
        target_path: str = "",
        task_id: str = "",
        **_: Any,
    ) -> ToolResult:
        scm_url = _resolve_agent_url(
            "SCM_AGENT_URL", "scm_agent_url", "http://scm:8020", "scm.repo.clone"
        )
        if not scm_url:
            return ToolResult(output=json.dumps({
                "error": "No registered SCM instance was found in the registry.",
                "repoUrl": repo_url,
                "targetPath": target_path,
            }))
        try:
            from framework.a2a.client import dispatch_sync
            metadata: dict[str, Any] = {"repoUrl": repo_url, "targetPath": target_path}
            if task_id:
                metadata["taskId"] = task_id
            result = dispatch_sync(
                url=scm_url,
                capability="scm.repo.clone",
                message_parts=[{"text": repo_url}],
                metadata=metadata,
                timeout=120,
            )
            task = result.get("task", result)
            if _task_state(task) != "TASK_STATE_COMPLETED":
                return ToolResult(output=json.dumps({
                    "error": _task_error(task, f"Clone failed: {repo_url}"),
                    "repoUrl": repo_url,
                    "targetPath": target_path,
                }))
            artifacts = task.get("artifacts", [])
            payload = _first_artifact_json(artifacts)
            return ToolResult(output=json.dumps(payload))
        except Exception as exc:
            return ToolResult(output=json.dumps({
                "error": str(exc),
                "repoUrl": repo_url,
                "targetPath": target_path,
            }))


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
            "design_code_path": {
                "type": "string",
                "description": "Local path to design code HTML file (ui-design/stitch/code.html). Optional.",
            },
            "design_md_path": {
                "type": "string",
                "description": "Local path to DESIGN.md spec file (ui-design/stitch/DESIGN.md). Optional.",
            },
            "design_local_folder": {
                "type": "string",
                "description": "Local folder where design files were saved by the UI design agent. Optional.",
            },
            "jira_local_folder": {
                "type": "string",
                "description": "Local folder where Jira ticket files were saved by the jira agent. Optional.",
            },
            "repo_url": {
                "type": "string",
                "description": "Git repository URL. Optional.",
            },
            "repo_path": {
                "type": "string",
                "description": "Local clone path. Optional.",
            },
            "workspace_path": {
                "type": "string",
                "description": "Shared workspace root. Optional.",
            },
            "context_manifest_path": {
                "type": "string",
                "description": "Path to context-manifest.json. Optional.",
            },
            "jira_files": {
                "type": "array",
                "description": "Jira context file paths. Optional.",
            },
            "design_files": {
                "type": "array",
                "description": "Design context file paths. Optional.",
            },
            "tech_stack": {
                "type": "array",
                "description": "List of technologies extracted from Jira ticket (e.g. ['react', 'typescript']). Optional.",
            },
            "stitch_screen_name": {
                "type": "string",
                "description": "Target Stitch screen name to implement (e.g. 'Lesson Library'). Optional.",
            },
            "revision_feedback": {
                "type": "string",
                "description": "Code review rejection reason for revision. Optional.",
            },
            "definition_of_done": {
                "type": "object",
                "description": "Acceptance gate criteria (build, tests, PR, screenshot). Optional.",
            },
        },
        "required": ["task_description"],
    }

    def execute_sync(
        self,
        task_description: str = "",
        jira_context: dict | None = None,
        design_context: dict | None = None,
        design_code_path: str = "",
        design_md_path: str = "",
        design_local_folder: str = "",
        jira_local_folder: str = "",
        repo_url: str = "",
        repo_path: str = "",
        workspace_path: str = "",
        context_manifest_path: str = "",
        jira_files: list | None = None,
        design_files: list | None = None,
        tech_stack: list | None = None,
        stitch_screen_name: str = "",
        orchestrator_task_id: str = "",
        revision_feedback: str = "",
        definition_of_done: dict | None = None,
        task_id: str = "",
        **_: Any,
    ) -> ToolResult:
        capability = "web-dev.task.execute"
        definition = _capability_definition(capability)
        meta: dict[str, Any] = {}
        if jira_context:
            meta["jiraContext"] = jira_context
        if design_context:
            meta["designContext"] = design_context
        if repo_url:
            meta["repoUrl"] = repo_url
        if repo_path:
            meta["repoPath"] = repo_path
        if workspace_path:
            meta["workspacePath"] = workspace_path
        if context_manifest_path:
            meta["contextManifestPath"] = context_manifest_path
        if jira_files:
            meta["jiraFiles"] = jira_files
        if jira_local_folder:
            meta["jiraLocalFolder"] = jira_local_folder
        if design_files:
            meta["designFiles"] = design_files
        if design_local_folder:
            meta["designLocalFolder"] = design_local_folder
        if design_code_path:
            meta["designCodePath"] = design_code_path
        if design_md_path:
            meta["designMdPath"] = design_md_path
        if tech_stack:
            meta["techStack"] = tech_stack
        if stitch_screen_name:
            meta["stitchScreenName"] = stitch_screen_name
        if orchestrator_task_id:
            meta["orchestratorTaskId"] = orchestrator_task_id
        if revision_feedback:
            meta["revisionFeedback"] = revision_feedback
        if definition_of_done:
            meta["definitionOfDone"] = definition_of_done
        if task_id:
            meta["taskId"] = task_id

        try:
            from framework.a2a.client import dispatch_sync
            timeout_seconds = _downstream_timeout_seconds("web_dev")
            launch_task_id = _derive_launch_task_id(
                orchestrator_task_id=orchestrator_task_id,
                task_id=task_id,
                workspace_path=workspace_path,
            )
            if _is_per_task_definition(definition):
                result = _dispatch_via_launcher(
                    definition,
                    capability=capability,
                    launch_task_id=launch_task_id or "web-dev-task",
                    message_parts=[{"text": task_description}],
                    metadata=meta,
                    timeout=timeout_seconds,
                )
            else:
                web_dev_url = _resolve_agent_url("WEB_DEV_AGENT_URL", "web_dev_agent_url", "http://web-dev:8050", capability)
                if not web_dev_url:
                    return ToolResult(output=json.dumps({
                        "status": "error",
                        "message": "No registered Web Dev instance was found in the registry.",
                    }))
                result = dispatch_sync(
                    url=web_dev_url,
                    capability=capability,
                    message_parts=[{"text": task_description}],
                    metadata=meta,
                    timeout=timeout_seconds,
                )
            task = result.get("task", result)
            task_state = _task_state(task)
            if task_state != "TASK_STATE_COMPLETED":
                return ToolResult(output=json.dumps({
                    "status": "error",
                    "state": task_state,
                    "message": _task_error(task, "Web Dev task failed"),
                }))
            artifacts = task.get("artifacts", [])
            summary = _extract_text(artifacts) or "Dev task completed."
            pr_url = _find_metadata(artifacts, "prUrl")
            branch = _find_metadata(artifacts, "branch")
            jira_in_review_raw = _find_metadata(artifacts, "jiraInReview")
            screenshot_included_raw = _find_metadata(artifacts, "screenshotIncluded")
            screenshot_uploaded_raw = _find_metadata(artifacts, "screenshotUploaded")
            # _find_metadata returns a string; normalise to bool
            jira_in_review = jira_in_review_raw in (True, "True", "true", "1")
            screenshot_included = screenshot_included_raw in (True, "True", "true", "1")
            screenshot_uploaded = screenshot_uploaded_raw in (True, "True", "true", "1")
            return ToolResult(output=json.dumps({
                "status": "completed",
                "summary": summary,
                "prUrl": pr_url,
                "branch": branch,
                "jiraInReview": jira_in_review,
                "screenshotIncluded": screenshot_included,
                "screenshotUploaded": screenshot_uploaded,
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
            "jira_context": {
                "type": "object",
                "description": "Jira context for requirements-aware review. Optional.",
            },
            "design_context": {
                "type": "object",
                "description": "Design context for review. Optional.",
            },
            "workspace_path": {
                "type": "string",
                "description": "Shared workspace root. Optional.",
            },
            "context_manifest_path": {
                "type": "string",
                "description": "Path to context-manifest.json. Optional.",
            },
        },
        "required": [],
    }

    def execute_sync(
        self,
        pr_url: str = "",
        diff_summary: str = "",
        requirements: str = "",
        jira_context: dict | None = None,
        design_context: dict | None = None,
        workspace_path: str = "",
        context_manifest_path: str = "",
        orchestrator_task_id: str = "",
        task_id: str = "",
        **_: Any,
    ) -> ToolResult:
        capability = "review.code.check"
        definition = _capability_definition(capability)
        meta: dict[str, Any] = {}
        if pr_url:
            meta["prUrl"] = pr_url
        if requirements:
            meta["originalRequirements"] = requirements
        if jira_context:
            meta["jiraContext"] = jira_context
        if design_context:
            meta["designContext"] = design_context
        if workspace_path:
            meta["workspacePath"] = workspace_path
        if context_manifest_path:
            meta["contextManifestPath"] = context_manifest_path
        if orchestrator_task_id:
            meta["orchestratorTaskId"] = orchestrator_task_id
        if task_id:
            meta["taskId"] = task_id

        try:
            from framework.a2a.client import dispatch_sync
            timeout_seconds = _downstream_timeout_seconds("code_review")
            launch_task_id = _derive_launch_task_id(
                orchestrator_task_id=orchestrator_task_id,
                task_id=task_id,
                workspace_path=workspace_path,
            )
            if _is_per_task_definition(definition):
                result = _dispatch_via_launcher(
                    definition,
                    capability=capability,
                    launch_task_id=launch_task_id or "code-review-task",
                    message_parts=[{"text": diff_summary or pr_url}],
                    metadata=meta,
                    timeout=timeout_seconds,
                )
            else:
                review_url = _resolve_agent_url("CODE_REVIEW_AGENT_URL", "code_review_agent_url", "http://code-review:8050", capability)
                if not review_url:
                    return ToolResult(output=json.dumps({
                        "verdict": "error",
                        "message": "No registered Code Review instance was found in the registry.",
                    }))
                result = dispatch_sync(
                    url=review_url,
                    capability=capability,
                    message_parts=[{"text": diff_summary or pr_url}],
                    metadata=meta,
                    timeout=timeout_seconds,
                )
            task = result.get("task", result)
            if _task_state(task) != "TASK_STATE_COMPLETED":
                return ToolResult(output=json.dumps({
                    "verdict": "error",
                    "message": _task_error(task, "Code review task failed"),
                }))
            artifacts = task.get("artifacts", [])
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

    def execute_sync(self, question: str = "", **_: Any) -> ToolResult:
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
    CloneRepo(),
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


def _task_state(task: dict) -> str:
    return task.get("status", {}).get("state", "")


def _task_error(task: dict, default: str) -> str:
    parts = task.get("status", {}).get("message", {}).get("parts", [])
    for part in parts:
        if "text" in part and part["text"]:
            return part["text"]
    return default
