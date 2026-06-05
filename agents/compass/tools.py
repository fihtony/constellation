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
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from framework.launcher import get_launcher
from framework.launcher_dispatch import (
    destroy_launch_instance,
    dispatch_via_launcher,
    wait_for_agent_ready,
)
import framework.launcher_dispatch as _launcher_dispatch
from framework.permissions import PermissionEngine
from framework.tools.base import BaseTool, ToolResult
from framework.tools.registry import get_registry


# A development task runs through analyze → plan → implement → review with up
# to 3 revision rounds; each round can take 15–25 minutes for a real
# repository.  90 minutes (5400s) is the empirically observed worst-case ceiling
# and aligns with the e2e poll timeout.  Office tasks are typically much
# shorter (single LLM call per document), so they keep a tighter default.
# Operators can override either via env var without touching code.
_DEV_DISPATCH_TIMEOUT_DEFAULT_SECONDS = 5400
_OFFICE_DISPATCH_TIMEOUT_DEFAULT_SECONDS = 1800


def _positive_int_env(var_name: str, default_seconds: int) -> int:
    """Return ``int(os.environ[var_name])`` if positive, else *default_seconds*."""
    raw_value = str(os.environ.get(var_name, "")).strip()
    if not raw_value:
        return default_seconds
    try:
        parsed = int(raw_value)
    except ValueError:
        return default_seconds
    return parsed if parsed > 0 else default_seconds


def _dev_dispatch_timeout() -> int:
    """Compass→team-lead A2A timeout (seconds) for development tasks."""
    return _positive_int_env(
        "COMPASS_DEV_DISPATCH_TIMEOUT_SECONDS",
        _DEV_DISPATCH_TIMEOUT_DEFAULT_SECONDS,
    )


def _office_dispatch_timeout() -> int:
    """Compass→office A2A timeout (seconds) for office tasks."""
    return _positive_int_env(
        "COMPASS_OFFICE_DISPATCH_TIMEOUT_SECONDS",
        _OFFICE_DISPATCH_TIMEOUT_DEFAULT_SECONDS,
    )


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
    """Legacy env-toggle — kept as a shim for tests that still monkeypatch it.

    The on-demand launch path is the only path now (the static office
    service is gone from the compose files). This stub always returns
    ``True`` so any code or test that still asks "should we launch
    per-task?" gets the correct answer without needing code changes.
    """
    return True


def _require_office_launch_permission() -> None:
    """Enforce the orchestrator→office launch grant at runtime.

    Loads ``config/permissions/compass.yaml`` and calls
    :meth:`PermissionEngine.require_agent_launching` for ``"office"``.
    The compass permission profile already declares
    ``agent_launching: true`` and ``allowed_agents: [team-lead, office]``;
    this is the runtime enforcement point that turns that declaration
    into a hard gate before any per-task container is spawned.
    """
    root = Path(__file__).resolve().parents[2]
    perm_engine = PermissionEngine.from_yaml(
        str(root / "config" / "permissions" / "compass.yaml")
    )
    perm_engine.require_agent_launching("office")


def _office_launch_definition(capability: str) -> dict[str, Any]:
    try:
        from framework.config import build_agent_definition_from_config
        from framework.registry_client import RegistryClient

        client = RegistryClient.from_config()
        requested_capability = _office_requested_capability(capability)
        for candidate in (requested_capability, "office.document.summarize"):
            definition = client.get_capability_definition(candidate)
            if definition and (definition.get("launch_spec") or definition.get("launchSpec")):
                return dict(definition)
        fallback = build_agent_definition_from_config("office")
        if fallback.get("launch_spec") or fallback.get("launchSpec"):
            return dict(fallback)
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


def _build_office_execution_contract(output_mode: str = "workspace") -> dict[str, Any]:
    """Build the parent-issued execution contract for Office per-task work."""
    contract, _permissions = _build_office_dispatch_contract(output_mode)
    return contract


def _build_office_dispatch_contract(output_mode: str = "workspace") -> tuple[dict[str, Any], dict[str, Any]]:
    """Build both executionContract and child-scoped permission snapshot for Office."""
    from framework.execution_contract import (
        build_execution_contract,
        load_child_profiles,
        permission_snapshot_from_permission_set,
        resolve_execution_contract_permission_set,
    )

    root = Path(__file__).resolve().parents[2]
    child_profiles = load_child_profiles({
        "office": str(root / "config" / "permissions" / "office.yaml"),
    })
    definition_of_done = {
        "output_mode": output_mode if output_mode in {"workspace", "inplace"} else "workspace",
        "delivery_report_required": True,
        "workspace_output_required": output_mode != "inplace",
    }
    contract = build_execution_contract(
        profile=child_profiles["office"],
        workflow_ref="config/workflows/office_task.yaml",
        rule_refs=[],
        workspace_root=os.environ.get("ARTIFACT_ROOT", ""),
        definition_of_done=definition_of_done,
    )
    if not contract.allowed_tools:
        raise ValueError("office permission profile has no allowed_tools")
    contract_dict = contract.to_dict()
    _resolved_contract, permission_set = resolve_execution_contract_permission_set(
        "office",
        contract_dict,
    )
    return contract_dict, permission_snapshot_from_permission_set(permission_set)


def _wait_for_agent_ready(base_url: str, timeout: int = 30) -> None:
    """Deprecated shim — use :func:`framework.launcher_dispatch.wait_for_agent_ready`."""
    return wait_for_agent_ready(base_url, timeout)


def _dispatch_office_task_via_launcher(
    task_description: str,
    source_paths: list[str],
    output_mode: str,
    capability: str,
    orchestrator_task_id: str,
    office_definition: dict[str, Any] | None = None,
    organize_group_by: str = "",
    extra: dict[str, Any] | None = None,
) -> dict:
    definition = office_definition or _office_launch_definition(capability)
    if not definition:
        raise RuntimeError("No registered Office launch definition was found in the registry.")

    _launcher = _launcher_dispatch.get_launcher()
    mount_plan = _office_mount_plan(source_paths, output_mode, _launcher)
    execution_contract, permissions = _build_office_dispatch_contract(output_mode)
    metadata: dict[str, Any] = {
        "source_paths": mount_plan["translated_paths"],
        "output_mode": output_mode,
        "capability": capability,
        "compassTaskId": orchestrator_task_id,
        "executionContract": execution_contract,
        "permissions": permissions,
    }
    # When the user already picked a dimension (after a clarification
    # round-trip), forward it to office so ``parse_dimension`` resolves
    # it from metadata on the first call.
    if organize_group_by:
        metadata["organizeGroupBy"] = organize_group_by
    # The custom-dimension plan-then-execute flow also needs the
    # user's custom-dimension hint (so office's planner knows what
    # to group by) and any approved plan / modify note forwarded by
    # the compass resume handler.
    extra = extra or {}
    custom_hint = str(extra.get("customDimensionHint") or "").strip()
    if custom_hint:
        metadata["customDimensionHint"] = custom_hint
    custom_plan = extra.get("organize_custom_plan") or {}
    if custom_plan:
        metadata["organizeCustomPlan"] = custom_plan
    custom_action = str(extra.get("organize_custom_action") or "").strip()
    if custom_action:
        metadata["organizeCustomAction"] = custom_action
    result = dispatch_via_launcher(
        definition,
        capability=_office_requested_capability(capability),
        launch_task_id=orchestrator_task_id or "office-task",
        message_parts=[{"text": task_description}],
        metadata=metadata,
        timeout=_office_dispatch_timeout(),
        # Office is a single-shot document task — spawn a fresh
        # container per call and tear it down as soon as the A2A
        # request completes. There is no review/revision cycle that
        # would benefit from reusing the container.
        preserve_instance=False,
        launch_overrides={
            "env": mount_plan["env"],
            "extra_binds": mount_plan["extra_binds"],
        },
    )

    task = result.get("task", result)
    task_state = task.get("status", {}).get("state", "")
    artifacts = task.get("artifacts", [])
    summary = _extract_text(artifacts) or _extract_status_text(task) or "Task completed."
    status = "completed" if task_state == "TASK_STATE_COMPLETED" else (
        "input-required" if task_state == "TASK_STATE_INPUT_REQUIRED" else "error"
    )
    # When office pauses the task with a needs_clarification payload, the
    # office task's ``_interrupt`` metadata carries the structured payload.
    # Surface it so the orchestrator can re-prompt the user with the
    # suggested options instead of treating the run as a generic failure.
    interrupt_metadata = task.get("metadata", {}).get("_interrupt") or {}
    needs_clarification = (
        interrupt_metadata.get("needs_clarification")
        if isinstance(interrupt_metadata, dict) else None
    )
    question = ""
    if needs_clarification and isinstance(needs_clarification, dict):
        question = str(needs_clarification.get("user_message") or "").strip()
    if not question:
        # Fall back to the status message parts (set by task_store.pause_task).
        status_message = task.get("status", {}).get("message", {}) or {}
        for part in status_message.get("parts", []) or []:
            text = part.get("text")
            if text:
                question = str(text).strip()
                break
    return {
        "status": status,
        "state": task_state,
        "taskId": task.get("id", ""),
        "summary": summary,
        "question": question,
        "needs_clarification": needs_clarification or {},
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
                timeout=_dev_dispatch_timeout(),
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
            "organizeGroupBy": {
                "type": "string",
                "description": (
                    "Optional organize grouping dimension (size, type, "
                    "created_time, modified_time, accessed_time, filename). "
                    "When the user did not supply one, the office agent "
                    "surfaces a needs_clarification reply and the orchestrator "
                    "re-prompts. Compass fills this in once the user picks."
                ),
            },
            "customDimensionHint": {
                "type": "string",
                "description": (
                    "Optional natural-language grouping hint for the "
                    "LLM-driven custom-dimension path.  When set, office "
                    "skips the six built-in dimensions and produces a "
                    "plan via the LLM planner (e.g. 'student name', "
                    "'subject area', 'department')."
                ),
            },
            "organizeCustomPlan": {
                "type": "object",
                "description": (
                    "Optional pre-approved plan (from a previous "
                    "plan-then-execute round-trip).  When set, office "
                    "skips planning and runs the LLM executor pass "
                    "directly.  The plan is the JSON object office's "
                    "planner produced on the previous call."
                ),
            },
            "organizeCustomAction": {
                "type": "string",
                "description": (
                    "Optional user action on the custom plan: "
                    "'approve' or 'modify'.  Compass fills this in "
                    "after the user replies to the plan-approval "
                    "question."
                ),
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
        organizeGroupBy: str = "",
        customDimensionHint: str = "",
        organizeCustomPlan: dict | None = None,
        organizeCustomAction: str = "",
    ) -> ToolResult:
        # Permission gate: compass is the orchestrator for office work,
        # so it must hold an explicit "agent_launching" grant for the
        # office agent before any per-task container can be spawned.
        # ``config/permissions/compass.yaml`` already lists ``office``
        # in ``allowed_agents``; this call is the runtime enforcement
        # point that turns that declaration into a hard gate.
        _require_office_launch_permission()

        normalized_source_paths = [str(item) for item in (source_paths or []) if item]
        if file_path and file_path not in normalized_source_paths:
            normalized_source_paths.append(file_path)

        extra: dict[str, Any] = {}
        if customDimensionHint:
            extra["customDimensionHint"] = str(customDimensionHint).strip()
        if organizeCustomPlan:
            extra["organize_custom_plan"] = dict(organizeCustomPlan)
        if organizeCustomAction:
            extra["organize_custom_action"] = str(organizeCustomAction).strip()

        try:
            office_definition = _office_launch_definition(capability)
            result = _dispatch_office_task_via_launcher(
                task_description=task_description,
                source_paths=normalized_source_paths,
                output_mode=output_mode,
                capability=capability,
                orchestrator_task_id=orchestrator_task_id,
                organize_group_by=str(organizeGroupBy or "").strip(),
                office_definition=office_definition,
                extra=extra,
            )
            return ToolResult(output=json.dumps(result))
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
