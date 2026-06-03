"""Team Lead Agent tools — Python implementations called by the LLM via ReAct.

The LLM decides when and in what order to call these tools.  No predefined
workflow orchestration — intelligence comes from the LLM + instructions.
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from typing import Any

from framework.launcher import get_launcher
from framework.launcher_dispatch import (
    destroy_launch_instance,
    dispatch_via_launcher,
    wait_for_agent_ready,
)
from framework.tools.base import BaseTool, ToolResult
from framework.tools.registry import get_registry


# Per-task agent instances get a process-local unique task_id so the orchestrator
# can track them across re-review rounds.  The counter resets to 1 on import.
_REVIEW_TASK_COUNTER_LOCK = threading.Lock()
_review_task_counter = 0


def _next_review_task_id() -> str:
    """Return the next monotonically-increasing per-task review ID.

    The id is purely a Team-Lead-side handle — the launched code-review instance
    may assign its own A2A task_id, but the orchestrator's session cache keys on
    this handle so that the CR container can be reused across review rounds.
    """
    global _review_task_counter
    with _REVIEW_TASK_COUNTER_LOCK:
        _review_task_counter += 1
        return f"task-review-{_review_task_counter}"


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


def _orchestrator_callback_url(task_id: str) -> str:
    """Build Team Lead's callback URL for child notifications when discoverable."""
    task_key = str(task_id or "").strip()
    if not task_key:
        return ""
    base_url = _discover_via_registry("team-lead.task.analyze")
    if not base_url:
        return ""
    return f"{base_url.rstrip('/')}/tasks/{task_key}/callbacks"


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
    """Deprecated shim — use :func:`framework.launcher_dispatch.wait_for_agent_ready`."""
    return wait_for_agent_ready(base_url, timeout)


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


def _replacement_confirm_seconds() -> float:
    raw_value = os.environ.get("TEAM_LEAD_CHILD_REPLACEMENT_CONFIRM_SECONDS", "30").strip()
    if not raw_value:
        return 30.0
    try:
        value = float(raw_value)
        return value if value >= 0 else 30.0
    except ValueError:
        return 30.0


def _confirm_child_instance_exited(agent_id: str, task_id: str) -> tuple[bool, list[dict[str, Any]]]:
    """Wait briefly for a prior child instance to disappear before replacement."""
    if not task_id:
        return True, []

    launcher = get_launcher()
    deadline = time.time() + _replacement_confirm_seconds()
    latest_live: list[dict[str, Any]] = []
    while True:
        latest_live = launcher.find_live_instances(agent_id, task_id)
        if not latest_live:
            return True, []
        if time.time() >= deadline:
            return False, latest_live
        time.sleep(1.0)


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


def _validate_task_workspace_root(workspace_path: str, agent_name: str) -> None:
    workspace = (workspace_path or "").strip()
    if not workspace:
        return
    artifact_root = os.path.abspath(os.environ.get("ARTIFACT_ROOT", "artifacts"))
    if os.path.abspath(workspace) == artifact_root:
        raise ValueError(
            f"{agent_name} requires a single task workspace root, not the shared ARTIFACT_ROOT directory"
        )


def _dispatch_boundary_capability(
    *,
    url: str,
    capability: str,
    text: str,
    metadata: dict[str, Any],
    timeout: int = 120,
) -> dict[str, Any]:
    from framework.a2a.client import dispatch_sync

    result = dispatch_sync(
        url=url,
        capability=capability,
        message_parts=[{"text": text}],
        metadata=metadata,
        timeout=timeout,
    )
    task = result.get("task", result)
    if _task_state(task) != "TASK_STATE_COMPLETED":
        return {"error": _task_error(task, f"{capability} failed")}
    return _first_artifact_json(task.get("artifacts", []))


def _dispatch_via_launcher(
    definition: dict[str, Any],
    *,
    capability: str,
    launch_task_id: str,
    message_parts: list[dict[str, Any]],
    metadata: dict[str, Any],
    timeout: int,
    preserve_instance: bool = False,
    per_task_agent_task_id: str = "",
    launch_overrides: dict[str, Any] | None = None,
) -> dict:
    """Deprecated shim — use :func:`framework.launcher_dispatch.dispatch_via_launcher`."""
    return dispatch_via_launcher(
        definition,
        capability=capability,
        launch_task_id=launch_task_id,
        message_parts=message_parts,
        metadata=metadata,
        timeout=timeout,
        preserve_instance=preserve_instance,
        per_task_agent_task_id=per_task_agent_task_id,
        launch_overrides=launch_overrides,
    )


def _destroy_launch_instance(launch_info: dict[str, Any] | None) -> bool:
    """Deprecated shim — use :func:`framework.launcher_dispatch.destroy_launch_instance`."""
    return destroy_launch_instance(launch_info)


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
# Tool: jira_comment
# ---------------------------------------------------------------------------

class JiraComment(BaseTool):
    """Add a Jira comment via the Jira boundary agent."""

    name = "jira_comment"
    description = "Add a comment to a Jira ticket via the Jira Agent."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {
                "type": "string",
                "description": "Jira ticket key, e.g. PROJ-123.",
            },
            "comment": {
                "type": "string",
                "description": "Comment body to add to the ticket.",
            },
            "task_id": {
                "type": "string",
                "description": "Caller task ID for log correlation (optional).",
            },
        },
        "required": ["ticket_key", "comment"],
    }

    def execute_sync(
        self,
        ticket_key: str = "",
        comment: str = "",
        task_id: str = "",
        **_: Any,
    ) -> ToolResult:
        jira_url = _resolve_agent_url("JIRA_AGENT_URL", "jira_agent_url", "http://jira:8010", "jira.comment.add")
        if not jira_url:
            return ToolResult(output=json.dumps({"error": "No registered Jira instance was found in the registry.", "ticketKey": ticket_key}))
        try:
            metadata: dict[str, Any] = {"ticketKey": ticket_key, "comment": comment}
            if task_id:
                metadata["taskId"] = task_id
            payload = _dispatch_boundary_capability(
                url=jira_url,
                capability="jira.comment.add",
                text=comment,
                metadata=metadata,
            )
            return ToolResult(output=json.dumps(payload))
        except Exception as exc:
            return ToolResult(output=json.dumps({"error": str(exc), "ticketKey": ticket_key}))


# ---------------------------------------------------------------------------
# Tool: jira_transition
# ---------------------------------------------------------------------------

class JiraTransition(BaseTool):
    """Transition a Jira ticket via the Jira boundary agent."""

    name = "jira_transition"
    description = "Transition a Jira ticket to a new status via the Jira Agent."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {
                "type": "string",
                "description": "Jira ticket key, e.g. PROJ-123.",
            },
            "transition_name": {
                "type": "string",
                "description": "Human-readable transition name.",
            },
            "task_id": {
                "type": "string",
                "description": "Caller task ID for log correlation (optional).",
            },
        },
        "required": ["ticket_key", "transition_name"],
    }

    def execute_sync(
        self,
        ticket_key: str = "",
        transition_name: str = "",
        task_id: str = "",
        **_: Any,
    ) -> ToolResult:
        jira_url = _resolve_agent_url("JIRA_AGENT_URL", "jira_agent_url", "http://jira:8010", "jira.ticket.transition")
        if not jira_url:
            return ToolResult(output=json.dumps({"error": "No registered Jira instance was found in the registry.", "ticketKey": ticket_key}))
        try:
            metadata: dict[str, Any] = {"ticketKey": ticket_key, "transitionName": transition_name}
            if task_id:
                metadata["taskId"] = task_id
            payload = _dispatch_boundary_capability(
                url=jira_url,
                capability="jira.ticket.transition",
                text=transition_name,
                metadata=metadata,
            )
            return ToolResult(output=json.dumps(payload))
        except Exception as exc:
            return ToolResult(output=json.dumps({"error": str(exc), "ticketKey": ticket_key}))


# ---------------------------------------------------------------------------
# Tool: scm_add_pr_inline_comment
# ---------------------------------------------------------------------------

class SCMAddPRInlineComment(BaseTool):
    """Post an inline PR review comment via the SCM boundary agent."""

    name = "scm_add_pr_inline_comment"
    description = "Post an inline review comment on a PR diff line via the SCM Agent."
    parameters_schema = {
        "type": "object",
        "properties": {
            "repo_url": {
                "type": "string",
                "description": "Full repository URL.",
            },
            "pr_number": {
                "type": "integer",
                "description": "Pull request number.",
            },
            "file_path": {
                "type": "string",
                "description": "Path of the file to comment on (relative to repo root).",
            },
            "line": {
                "type": "integer",
                "description": "Line number in the file to comment on.",
            },
            "comment": {
                "type": "string",
                "description": "Markdown comment body.",
            },
            "commit_id": {
                "type": "string",
                "description": "Commit SHA to attach comment to (optional).",
            },
            "task_id": {
                "type": "string",
                "description": "Caller task ID for log correlation (optional).",
            },
        },
        "required": ["repo_url", "pr_number", "file_path", "line", "comment"],
    }

    def execute_sync(
        self,
        repo_url: str = "",
        pr_number: int = 0,
        file_path: str = "",
        line: int = 0,
        comment: str = "",
        commit_id: str = "",
        task_id: str = "",
        **_: Any,
    ) -> ToolResult:
        scm_url = _resolve_agent_url(
            "SCM_AGENT_URL",
            "scm_agent_url",
            "http://scm:8020",
            "scm.pr.comment.inline",
        )
        if not scm_url:
            return ToolResult(output=json.dumps({"error": "No registered SCM instance was found in the registry.", "repoUrl": repo_url, "prNumber": pr_number}))
        try:
            metadata: dict[str, Any] = {
                "repoUrl": repo_url,
                "prNumber": pr_number,
                "filePath": file_path,
                "line": line,
                "comment": comment,
            }
            if commit_id:
                metadata["commitId"] = commit_id
            if task_id:
                metadata["taskId"] = task_id
            payload = _dispatch_boundary_capability(
                url=scm_url,
                capability="scm.pr.comment.inline",
                text=comment,
                metadata=metadata,
            )
            return ToolResult(output=json.dumps(payload))
        except Exception as exc:
            return ToolResult(output=json.dumps({"error": str(exc), "repoUrl": repo_url, "prNumber": pr_number}))


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
            "branch_name": {
                "type": "string",
                "description": "Existing development branch to continue using for revisions. Optional.",
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
            "review_report_path": {
                "type": "string",
                "description": "Relative path to the prior code review report for revision mode. Optional.",
            },
            "revision_mode": {
                "type": "boolean",
                "description": "Whether the child task is a revision against an existing PR.",
            },
            "revision_round": {
                "type": "integer",
                "description": "Current revision round number (1-based when revising). Optional.",
            },
            "existing_pr_url": {
                "type": "string",
                "description": "Existing PR URL to update during revision mode. Optional.",
            },
            "existing_pr_number": {
                "type": "integer",
                "description": "Existing PR number to update during revision mode. Optional.",
            },
            "existing_branch": {
                "type": "string",
                "description": "Existing branch name to reuse during revision mode. Optional.",
            },
            "definition_of_done": {
                "type": "object",
                "description": "Acceptance gate criteria (build, tests, PR, screenshot). Optional.",
            },
            "execution_contract": {
                "type": "object",
                "description": "Parent-issued execution contract for the Web Dev child agent.",
            },
            "child_service_url": {
                "type": "string",
                "description": "Existing container service URL to reuse for a revision dispatch (avoids launching a new container). Optional.",
            },
            "child_container_name": {
                "type": "string",
                "description": "Existing container name matching child_service_url. Returned unchanged in response so dev_agent_session stays consistent. Optional.",
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
        branch_name: str = "",
        workspace_path: str = "",
        context_manifest_path: str = "",
        jira_files: list | None = None,
        design_files: list | None = None,
        tech_stack: list | None = None,
        stitch_screen_name: str = "",
        orchestrator_task_id: str = "",
        revision_feedback: str = "",
        review_report_path: str = "",
        revision_mode: bool = False,
        revision_round: int = 0,
        existing_pr_url: str = "",
        existing_pr_number: int = 0,
        existing_branch: str = "",
        definition_of_done: dict | None = None,
        task_id: str = "",
        child_service_url: str = "",
        child_container_name: str = "",
        **_: Any,
    ) -> ToolResult:
        capability = "web-dev.task.execute"
        definition: dict[str, Any] = {}
        meta: dict[str, Any] = {}
        web_dev_url = ""
        launch_info: dict[str, Any] | None = None
        if jira_context:
            meta["jiraContext"] = jira_context
        if design_context:
            meta["designContext"] = design_context
        if repo_url:
            meta["repoUrl"] = repo_url
        if repo_path:
            meta["repoPath"] = repo_path
        if branch_name:
            meta["branchName"] = branch_name
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
        if review_report_path:
            meta["reviewReportPath"] = review_report_path
        if revision_mode:
            meta["revisionMode"] = True
        if revision_round:
            meta["revisionRound"] = revision_round
        if existing_pr_url:
            meta["existingPrUrl"] = existing_pr_url
        if existing_pr_number:
            meta["existingPrNumber"] = existing_pr_number
        if existing_branch:
            meta["existingBranch"] = existing_branch
        if definition_of_done:
            meta["definitionOfDone"] = definition_of_done
        if task_id:
            meta["taskId"] = task_id
        callback_url = _orchestrator_callback_url(task_id or orchestrator_task_id)
        if callback_url:
            meta["orchestratorCallbackUrl"] = callback_url

        # Pass execution contract for child agent permission enforcement
        execution_contract = _.get("execution_contract") if isinstance(_, dict) else None
        if execution_contract:
            meta["executionContract"] = execution_contract
        permissions = _.get("permissions") if isinstance(_, dict) else None
        if isinstance(permissions, dict):
            meta["permissions"] = permissions

        new_launch = False  # True only when we create a fresh container (destroy on error)
        try:
            from framework.a2a.client import dispatch_sync
            timeout_seconds = _downstream_timeout_seconds("web_dev")
            launch_task_id = _derive_launch_task_id(
                orchestrator_task_id=orchestrator_task_id,
                task_id=task_id,
                workspace_path=workspace_path,
            )
            # Reuse an existing container when one is provided (revision cycles).
            # Per architecture spec: Team Lead must NOT launch a new container for
            # revisions — it sends a new /message:send to the same container.
            if child_service_url:
                try:
                    result = dispatch_sync(
                        url=child_service_url,
                        capability=capability,
                        message_parts=[{"text": task_description}],
                        metadata=meta,
                        timeout=timeout_seconds,
                    )
                    result = dict(result)
                    result["_launch"] = {
                        "agentId": "web-dev",
                        "serviceUrl": child_service_url,
                        "containerName": child_container_name,
                    }
                    launch_info = result["_launch"]
                except Exception as reuse_exc:
                    confirmed_exited, live_instances = _confirm_child_instance_exited(
                        "web-dev",
                        launch_task_id or orchestrator_task_id,
                    )
                    if not confirmed_exited:
                        live_names = [
                            str(instance.get("container_name") or "").strip()
                            for instance in live_instances
                            if str(instance.get("container_name") or "").strip()
                        ]
                        return ToolResult(output=json.dumps({
                            "status": "error",
                            "message": (
                                "Existing web-dev container is still live but unreachable; "
                                "refusing replacement to avoid duplicate instances."
                            ),
                            "liveContainers": live_names,
                        }))
                    print(f"[team-lead] Existing web-dev container unreachable ({reuse_exc}), launching new one.")
                    child_service_url = ""
                    child_container_name = ""

            if not child_service_url:
                definition = _capability_definition(capability)
                if _is_per_task_definition(definition):
                    result = _dispatch_via_launcher(
                        definition,
                        capability=capability,
                        launch_task_id=launch_task_id or "web-dev-task",
                        message_parts=[{"text": task_description}],
                        metadata=meta,
                        timeout=timeout_seconds,
                        preserve_instance=True,
                    )
                    new_launch = True
                    if isinstance(result, dict) and isinstance(result.get("_launch"), dict):
                        launch_info = dict(result["_launch"])
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
                if new_launch:
                    _destroy_launch_instance(launch_info)
                return ToolResult(output=json.dumps({
                    "status": "error",
                    "state": task_state,
                    "message": _task_error(task, "Web Dev task failed"),
                }))
            artifacts = task.get("artifacts", [])
            summary = _extract_text(artifacts) or "Dev task completed."
            pr_url = _find_metadata(artifacts, "prUrl")
            pr_number = _find_metadata(artifacts, "prNumber")
            result_repo_url = _find_metadata(artifacts, "repoUrl")
            branch = _find_metadata(artifacts, "branch")
            changed_files = _find_metadata(artifacts, "changedFiles")
            child_task_id = str(task.get("id") or "").strip()
            child_service_url = str((launch_info or {}).get("serviceUrl") or web_dev_url or "").strip()
            child_container_name = str((launch_info or {}).get("containerName") or "").strip()
            child_agent_id = str((launch_info or {}).get("agentId") or "web-dev").strip() or "web-dev"
            jira_in_review_raw = _find_metadata(artifacts, "jiraInReview")
            screenshot_included_raw = _find_metadata(artifacts, "screenshotIncluded")
            screenshot_uploaded_raw = _find_metadata(artifacts, "screenshotUploaded")
            # _find_metadata returns a string; normalise to bool
            jira_in_review = jira_in_review_raw in (True, "True", "true", "1")
            screenshot_included = screenshot_included_raw in (True, "True", "true", "1")
            screenshot_uploaded = screenshot_uploaded_raw in (True, "True", "true", "1")
            payload = {
                "status": "completed",
                "summary": summary,
                "prUrl": pr_url,
                "prNumber": pr_number,
                "repoUrl": result_repo_url,
                "branch": branch,
                "changedFiles": changed_files,
                "jiraInReview": jira_in_review,
                "screenshotIncluded": screenshot_included,
                "screenshotUploaded": screenshot_uploaded,
            }
            if child_task_id:
                payload["childTaskId"] = child_task_id
            if child_service_url:
                payload["childServiceUrl"] = child_service_url
            if child_container_name:
                payload["childContainerName"] = child_container_name
            if child_agent_id:
                payload["childAgentId"] = child_agent_id
            return ToolResult(output=json.dumps(payload))
        except Exception as exc:
            if new_launch:
                _destroy_launch_instance(launch_info)
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
            "pr_number": {
                "type": "integer",
                "description": "Pull request number to review.",
            },
            "repo_url": {
                "type": "string",
                "description": "Repository URL that owns the pull request.",
            },
            "changed_files": {
                "type": "array",
                "description": "Changed files reported by the dev agent. Optional.",
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
            "execution_contract": {
                "type": "object",
                "description": "Parent-issued execution contract for the Code Review child agent.",
            },
            "child_service_url": {
                "type": "string",
                "description": "Existing CR container URL to reuse for re-review rounds. Optional.",
            },
            "child_container_name": {
                "type": "string",
                "description": "Container name matching child_service_url. Optional.",
            },
        },
        "required": [],
    }

    def execute_sync(
        self,
        pr_url: str = "",
        pr_number: int = 0,
        repo_url: str = "",
        changed_files: list | None = None,
        diff_summary: str = "",
        requirements: str = "",
        jira_context: dict | None = None,
        design_context: dict | None = None,
        workspace_path: str = "",
        context_manifest_path: str = "",
        orchestrator_task_id: str = "",
        task_id: str = "",
        child_service_url: str = "",
        child_container_name: str = "",
        **_: Any,
    ) -> ToolResult:
        capability = "review.code.check"
        definition: dict[str, Any] = {}
        meta: dict[str, Any] = {}
        _validate_task_workspace_root(workspace_path, "Code Review")
        if pr_url:
            meta["prUrl"] = pr_url
        if pr_number:
            meta["prNumber"] = pr_number
        if repo_url:
            meta["repoUrl"] = repo_url
        if changed_files:
            meta["changedFiles"] = changed_files
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
        callback_url = _orchestrator_callback_url(task_id or orchestrator_task_id)
        if callback_url:
            meta["orchestratorCallbackUrl"] = callback_url
        execution_contract = _.get("execution_contract") if isinstance(_, dict) else None
        if execution_contract:
            meta["executionContract"] = execution_contract
        permissions = _.get("permissions") if isinstance(_, dict) else None
        if isinstance(permissions, dict):
            meta["permissions"] = permissions
        # Pass through fields needed by CR agent (§12.1)
        _repo_path = _.get("repo_path", "") if isinstance(_, dict) else ""
        if _repo_path:
            meta["repoPath"] = _repo_path
        _review_round = _.get("review_round") if isinstance(_, dict) else None
        if _review_round:
            meta["reviewRound"] = _review_round
        _prev_review = _.get("previous_review_path") if isinstance(_, dict) else None
        if _prev_review:
            meta["previousReviewPath"] = _prev_review
        _tech_stack = _.get("tech_stack") if isinstance(_, dict) else None
        if _tech_stack:
            meta["techStack"] = _tech_stack

        try:
            from framework.a2a.client import dispatch_sync
            timeout_seconds = _downstream_timeout_seconds("code_review")
            launch_task_id = _derive_launch_task_id(
                orchestrator_task_id=orchestrator_task_id,
                task_id=task_id,
                workspace_path=workspace_path,
            )
            # Per-task code-review instance gets its own session task_id so the
            # orchestrator can reuse the same container across re-review rounds.
            per_task_agent_task_id = ""
            if child_service_url:
                try:
                    _wait_for_agent_ready(child_service_url)
                    result = dispatch_sync(
                        url=child_service_url,
                        capability=capability,
                        message_parts=[{"text": diff_summary or pr_url}],
                        metadata=meta,
                        timeout=timeout_seconds,
                    )
                    result = dict(result)
                    result["_launch"] = {
                        "agentId": "code-review",
                        "serviceUrl": child_service_url,
                        "containerName": child_container_name,
                        "perTaskAgentTaskId": per_task_agent_task_id,
                    }
                except Exception:
                    confirmed_exited, live_instances = _confirm_child_instance_exited(
                        "code-review",
                        launch_task_id or orchestrator_task_id,
                    )
                    if not confirmed_exited:
                        live_names = [
                            str(instance.get("container_name") or "").strip()
                            for instance in live_instances
                            if str(instance.get("container_name") or "").strip()
                        ]
                        return ToolResult(output=json.dumps({
                            "verdict": "error",
                            "message": (
                                "Existing code-review container is still live but unreachable; "
                                "refusing replacement to avoid duplicate instances."
                            ),
                            "liveContainers": live_names,
                        }))
                    child_service_url = ""

            if not child_service_url:
                definition = _capability_definition(capability)
            if child_service_url:
                pass
            elif _is_per_task_definition(definition):
                per_task_agent_task_id = _next_review_task_id()
                result = _dispatch_via_launcher(
                    definition,
                    capability=capability,
                    launch_task_id=launch_task_id or "code-review-task",
                    message_parts=[{"text": diff_summary or pr_url}],
                    metadata=meta,
                    timeout=timeout_seconds,
                    preserve_instance=True,
                    per_task_agent_task_id=per_task_agent_task_id,
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
            launch_info = result.get("_launch")
            if _task_state(task) != "TASK_STATE_COMPLETED":
                return ToolResult(output=json.dumps({
                    "verdict": "error",
                    "message": _task_error(task, "Code review task failed"),
                }))
            artifacts = task.get("artifacts", [])
            payload = _first_artifact_json(artifacts) or {"verdict": "unknown"}
            child_task_id = str(task.get("id") or "").strip()
            # Embed launch info so Team Lead can persist the CR session
            if launch_info:
                # Prefer the per-task handle generated at launch time so the
                # orchestrator can track the CR instance across re-review rounds
                # even when the A2A response omits the child task id.
                session_task_id = (
                    str(launch_info.get("perTaskAgentTaskId") or "").strip()
                    or child_task_id
                )
                payload["_crSession"] = {
                    "task_id": session_task_id,
                    "service_url": launch_info.get("serviceUrl", ""),
                    "container_name": launch_info.get("containerName", ""),
                    "agent_id": launch_info.get("agentId", "code-review"),
                }
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
    JiraComment(),
    JiraTransition(),
    SCMAddPRInlineComment(),
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
