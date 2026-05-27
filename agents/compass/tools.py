"""Compass Agent tools — Python implementations called by the LLM via ReAct.

Each tool is a BaseTool subclass registered in the global ToolRegistry.
The LLM decides *when* and *in what order* to call these tools based on the
user request and its reasoning.  No Python workflow orchestration here.
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from types import SimpleNamespace
from typing import Any

from framework.launcher import get_launcher
from framework.tools.base import BaseTool, ToolResult
from framework.tools.registry import get_registry


def _resolve_team_lead_url() -> str:
    """Resolve the Team Lead endpoint via Registry only."""
    try:
        from framework.registry_client import RegistryClient

        discovered = RegistryClient.from_config().discover("team-lead.task.analyze")
        if discovered:
            return discovered
    except Exception:
        pass
    return ""


def _resolve_office_url() -> str:
    """Resolve the Office endpoint via Registry only."""
    try:
        from framework.registry_client import RegistryClient

        client = RegistryClient.from_config()
        for capability in ("office.document.summarize", "office.agent"):
            discovered = client.discover(capability)
            if discovered:
                return discovered
    except Exception:
        pass
    return ""


def _office_requested_capability(capability: str) -> str:
    mapping = {
        "analyze": "office.data.analyze",
        "organize": "office.folder.organize",
        "summarize": "office.document.summarize",
    }
    return mapping.get((capability or "").strip().lower(), "office.document.summarize")


def _is_containerized_process() -> bool:
    return any((
        bool(os.environ.get("CONTAINER_ID", "").strip()),
        os.path.exists("/.dockerenv"),
        os.path.exists("/run/.containerenv"),
    ))


def _should_use_per_task_office_launch() -> bool:
    if os.environ.get("CONSTELLATION_FORCE_DIRECT_OFFICE_URL", "").strip().lower() in {"1", "true", "yes"}:
        return False
    if not _is_containerized_process():
        return False
    return bool(_office_launch_definition("summarize"))


def _office_launch_definition(capability: str) -> dict[str, Any]:
    def _as_definition_object(definition: dict[str, Any]) -> Any:
        return SimpleNamespace(**definition)

    try:
        from framework.config import build_agent_definition_from_config
        from framework.registry_client import RegistryClient

        client = RegistryClient.from_config()
        requested_capability = _office_requested_capability(capability)
        for candidate in (requested_capability, "office.document.summarize"):
            definition = client.get_capability_definition(candidate)
            if definition and (definition.get("launch_spec") or definition.get("launchSpec")):
                return _as_definition_object(definition)
        fallback = build_agent_definition_from_config("office")
        if fallback.get("launch_spec") or fallback.get("launchSpec"):
            return _as_definition_object(fallback)
    except Exception:
        pass
    return {}


def _office_mount_plan(source_paths: list[str], output_mode: str, launcher) -> dict[str, Any]:
    if not source_paths:
        return {
            "translated_paths": [],
            "extra_binds": [],
            "env": {
                "OFFICE_SOURCE_ROOT": "/app/userdata",
                "OFFICE_ALLOWED_BASE_PATHS": "",
                "OFFICE_ALLOW_INPLACE_WRITES": "true" if output_mode == "inplace" else "false",
            },
        }

    translated_paths: list[str] = []
    allowed_paths: list[str] = []
    extra_binds: list[str] = []
    mount_targets: dict[tuple[str, bool], str] = {}
    read_only = output_mode != "inplace"
    source_root = "/app/userdata"

    for raw_path in source_paths:
        requested_path = str(raw_path or "").strip()
        if not requested_path or not os.path.isabs(requested_path):
            raise ValueError(f"Office source path must be absolute: {raw_path}")

        host_path = os.path.realpath(requested_path)
        visible_path = requested_path if os.path.exists(requested_path) else ""
        if os.path.exists(requested_path):
            resolved_host_path = str(launcher.resolve_host_path(requested_path) or "").strip()
            if resolved_host_path:
                host_path = os.path.realpath(resolved_host_path)
        else:
            resolve_container_path = getattr(launcher, "resolve_container_path", None)
            if callable(resolve_container_path):
                translated = str(resolve_container_path(requested_path) or "").strip()
                if translated and os.path.exists(translated):
                    visible_path = translated
                    resolved_host_path = str(launcher.resolve_host_path(translated) or "").strip()
                    if resolved_host_path:
                        host_path = os.path.realpath(resolved_host_path)

        host_path_stripped = host_path.rstrip(os.sep)
        if not host_path_stripped:
            host_path_stripped = os.sep

        relative_path = os.path.basename(host_path_stripped)
        is_directory_request = False
        if visible_path:
            is_directory_request = os.path.isdir(visible_path)
        elif requested_path.endswith(os.sep):
            is_directory_request = True
        elif not os.path.splitext(os.path.basename(host_path_stripped))[1]:
            is_directory_request = True

        if is_directory_request:
            host_bind_source = host_path_stripped
            bind_relative_path = os.path.basename(host_path_stripped)
            relative_path = ""
        elif relative_path:
            host_bind_source = os.path.dirname(host_path_stripped) or os.sep
            bind_relative_path = ""
        else:
            host_bind_source = host_path_stripped
            bind_relative_path = ""

        bind_key = (os.path.realpath(host_bind_source), read_only, bind_relative_path)
        mount_target = mount_targets.get(bind_key)
        if not mount_target:
            mount_base = f"{source_root}/input-{len(mount_targets)}"
            mount_target = os.path.join(mount_base, bind_relative_path) if bind_relative_path else mount_base
            mount_targets[bind_key] = mount_target
            bind_suffix = ":ro" if read_only else ""
            extra_binds.append(f"{host_bind_source}:{mount_target}{bind_suffix}")

        translated_path = mount_target if not relative_path else os.path.join(mount_target, relative_path)
        translated_paths.append(translated_path)
        allowed_paths.append(translated_path)

    return {
        "translated_paths": translated_paths,
        "extra_binds": extra_binds,
        "env": {
            "OFFICE_SOURCE_ROOT": source_root,
            "OFFICE_ALLOWED_BASE_PATHS": ":".join(allowed_paths),
            "OFFICE_ALLOW_INPLACE_WRITES": "true" if output_mode == "inplace" else "false",
        },
    }


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
    raise TimeoutError(f"Timed out waiting for per-task office agent: {last_error}")


def _dispatch_office_task_via_launcher(
    task_description: str,
    source_paths: list[str],
    output_mode: str,
    capability: str,
    orchestrator_task_id: str,
    office_definition: dict[str, Any] | None = None,
) -> dict:
    from framework.a2a.client import dispatch_sync

    definition = office_definition or _office_launch_definition(capability)
    if not definition:
        raise RuntimeError("No registered Office launch definition was found in the registry.")

    launcher = get_launcher()
    mount_plan = _office_mount_plan(source_paths, output_mode, launcher)
    launch = launcher.launch_instance(
        definition,
        orchestrator_task_id or "office-task",
        launch_overrides={
            "env": mount_plan["env"],
            "extra_binds": mount_plan["extra_binds"],
        },
    )

    try:
        _wait_for_agent_ready(launch["service_url"])
        result = dispatch_sync(
            url=launch["service_url"],
            capability=_office_requested_capability(capability),
            message_parts=[{"text": task_description}],
            metadata={
                "source_paths": mount_plan["translated_paths"],
                "output_mode": output_mode,
                "capability": capability,
                "compassTaskId": orchestrator_task_id,
            },
            timeout=3600,
        )
    finally:
        try:
            agent_id = getattr(definition, "agent_id", None)
            if not agent_id and isinstance(definition, dict):
                agent_id = definition.get("agent_id") or definition.get("agentId")
            launcher.destroy_instance(
                str(agent_id or "office"),
                launch["container_name"],
            )
        except Exception:
            pass

    task = result.get("task", result)
    task_state = task.get("status", {}).get("state", "")
    artifacts = task.get("artifacts", [])
    summary = _extract_text(artifacts) or _extract_status_text(task) or "Task completed."
    status = "completed" if task_state == "TASK_STATE_COMPLETED" else (
        "input-required" if task_state == "TASK_STATE_INPUT_REQUIRED" else "error"
    )
    return {
        "status": status,
        "state": task_state,
        "taskId": task.get("id", ""),
        "summary": summary,
    }

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
            "orchestratorTaskId": {
                "type": "string",
                "description": "Caller task id used as the shared workflow task id. Optional.",
            },
            "workspacePath": {
                "type": "string",
                "description": "Shared workspace path created by Compass. Optional.",
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
        orchestratorTaskId: str = "",
        workspacePath: str = "",
        **_kwargs,
    ) -> ToolResult:
        import re as _re
        # Sanitize jira_key: MCP tool arguments may contain XML/control characters
        # Extract only the standard Jira key format (e.g. PROJ-123)
        if jira_key:
            _match = _re.search(r"[A-Z][A-Z0-9]+-\d+", jira_key)
            jira_key = _match.group(0) if _match else jira_key.strip().split()[0]

        team_lead_url = _resolve_team_lead_url()
        if not team_lead_url:
            return ToolResult(output=json.dumps({
                "status": "error",
                "message": "No registered Team Lead instance was found in the registry.",
            }))
        meta: dict[str, Any] = {}
        if jira_key:
            meta["jiraKey"] = jira_key
        if repo_url:
            meta["repoUrl"] = repo_url
        if design_url:
            meta["designUrl"] = design_url
        if orchestratorTaskId:
            meta["orchestratorTaskId"] = orchestratorTaskId
        if workspacePath:
            meta["workspacePath"] = workspacePath

        try:
            from framework.a2a.client import dispatch_sync
            result = dispatch_sync(
                url=team_lead_url,
                capability="team-lead.task.analyze",
                message_parts=[{"text": task_description}],
                metadata=meta,
                timeout=3600,
            )
            task = result.get("task", result)
            task_id = task.get("id", "")
            task_state = task.get("status", {}).get("state", "")
            if task_state and task_state != "TASK_STATE_COMPLETED":
                return ToolResult(output=json.dumps({
                    "status": "error",
                    "state": task_state,
                    "taskId": task_id,
                    "message": _extract_status_text(task) or f"Team Lead ended in {task_state}",
                }))
            artifacts = task.get("artifacts", [])
            summary = _extract_text(artifacts) or "Task completed."
            payload = {
                "status": "completed",
                "summary": summary,
            }
            if task_id:
                payload["taskId"] = task_id
            artifact_metadata = next(
                (
                    artifact.get("metadata", {})
                    for artifact in artifacts
                    if isinstance(artifact, dict) and isinstance(artifact.get("metadata"), dict)
                ),
                {},
            )
            for key in ("prUrl", "branch", "jiraInReview"):
                if artifact_metadata.get(key) not in (None, ""):
                    payload[key] = artifact_metadata.get(key)
            return ToolResult(output=json.dumps(payload))
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
            "source_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Authorized source file or folder paths.",
            },
            "output_mode": {
                "type": "string",
                "description": "workspace or inplace",
            },
            "capability": {
                "type": "string",
                "description": "summarize, analyze, or organize",
            },
            "orchestrator_task_id": {
                "type": "string",
                "description": "The Compass task ID for workspace scoping.",
            },
            "callback_url": {
                "type": "string",
                "description": "Optional orchestrator callback URL.",
            },
        },
        "required": ["task_description"],
    }

    def execute_sync(
        self,
        task_description: str = "",
        file_path: str = "",
        source_paths: list[str] | None = None,
        output_mode: str = "workspace",
        capability: str = "summarize",
        orchestrator_task_id: str = "",
        callback_url: str = "",
    ) -> ToolResult:
        normalized_source_paths = [str(item) for item in (source_paths or []) if item]
        if file_path and file_path not in normalized_source_paths:
            normalized_source_paths.append(file_path)

        try:
            if _should_use_per_task_office_launch():
                office_definition = _office_launch_definition(capability)
                result = _dispatch_office_task_via_launcher(
                    task_description=task_description,
                    source_paths=normalized_source_paths,
                    output_mode=output_mode,
                    capability=capability,
                    orchestrator_task_id=orchestrator_task_id,
                    office_definition=office_definition,
                )
                return ToolResult(output=json.dumps(result))

            office_url = _resolve_office_url()
            if not office_url:
                return ToolResult(output=json.dumps({
                    "status": "error",
                    "message": "No registered Office instance was found in the registry.",
                }))
            meta: dict[str, Any] = {}
            if normalized_source_paths:
                meta["source_paths"] = normalized_source_paths
            if output_mode in {"workspace", "inplace"}:
                meta["output_mode"] = output_mode
            if capability:
                meta["capability"] = capability
            if orchestrator_task_id:
                meta["compassTaskId"] = orchestrator_task_id
            if callback_url:
                meta["orchestratorCallbackUrl"] = callback_url

            from framework.a2a.client import dispatch_sync

            result = dispatch_sync(
                url=office_url,
                capability=_office_requested_capability(capability),
                message_parts=[{"text": task_description}],
                metadata=meta,
                timeout=3600,
            )
            task = result.get("task", result)
            task_state = task.get("status", {}).get("state", "")
            artifacts = task.get("artifacts", [])
            summary = _extract_text(artifacts) or _extract_status_text(task) or "Task completed."
            status = "completed" if task_state == "TASK_STATE_COMPLETED" else (
                "input-required" if task_state == "TASK_STATE_INPUT_REQUIRED" else "error"
            )
            return ToolResult(output=json.dumps({
                "status": status,
                "state": task_state,
                "taskId": task.get("id", ""),
                "summary": summary,
            }))
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
