"""Office Agent — Graph outside, ReAct inside.

Handles generic office work across documents, spreadsheets, presentations,
and folder organization using agentic tool calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode
from framework.config import build_agent_definition_from_config
from framework.workflow import Workflow, START, END
from framework.a2a.protocol import Artifact

from agents.office.nodes import (
    receive_task,
    analyze_request,
    execute_office_work,
    report_result,
)
from agents.office.office_tools import register_office_tools


logger = logging.getLogger(__name__)


def _append_office_log(task_id: str, message: str, level: str = "INFO ", **kwargs: Any) -> None:
    if not task_id:
        return
    artifact_root = os.environ.get("ARTIFACT_ROOT", "artifacts/")
    log_path = os.path.join(artifact_root, task_id, "office", "agent.log")
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        extra = ""
        if kwargs:
            parts = []
            for key, value in kwargs.items():
                rendered = str(value)
                if len(rendered) > 200:
                    rendered = rendered[:197] + "..."
                parts.append(f"{key}={rendered!r}")
            extra = " " + " ".join(parts)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] [office] {message}{extra}\n")
    except OSError:
        return


def _normalize_source_paths(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return []


def _normalize_capability(value: str) -> str:
    capability = (value or "").strip().lower()
    mapping = {
        "office.document.summarize": "summarize",
        "office.folder.summarize": "summarize",
        "office.data.analyze": "analyze",
        "office.folder.organize": "organize",
    }
    capability = mapping.get(capability, capability)
    return capability if capability in {"summarize", "analyze", "organize"} else ""


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

office_workflow = Workflow(
    name="office",
    edges=[
        (START, receive_task, analyze_request),
        (analyze_request, execute_office_work),
        (execute_office_work, report_result, END),
    ],
)


# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------

def _build_office_definition() -> AgentDefinition:
    cfg = build_agent_definition_from_config("office")
    return AgentDefinition(
        agent_id=cfg.get("agent_id", "office"),
        name=cfg.get("name", "Office Agent"),
        description=cfg.get("description", "Document processing and data analysis"),
        version="1.0.0",
        mode=AgentMode.TASK,
        execution_mode=ExecutionMode.PER_TASK,
        skills=cfg.get("skills", []),
        tools=cfg.get("tools", []),
        permissions=cfg.get("permissions", {}),
        permission_profile=cfg.get("permission_profile", "office"),
        runtime_backend=cfg.get("runtime_backend", "connect-agent"),
        model=cfg.get("model", "gpt-5-mini"),
        workflow=office_workflow,
        config=cfg,
        launch_spec=cfg.get("launch_spec"),
    )


office_definition = _build_office_definition()


# ---------------------------------------------------------------------------
# OfficeAgent
# ---------------------------------------------------------------------------

class OfficeAgent(BaseAgent):
    async def start(self) -> None:
        await super().start()
        register_office_tools()

    async def handle_message(self, message: dict) -> dict:
        """Handle incoming A2A message.

        Non-blocking: returns task dict immediately, runs workflow in background thread.
        """
        from framework.workflow import RunConfig
        from framework.devlog import AgentLogger

        msg = message.get("message", message)
        parts = msg.get("parts", [])
        user_text = parts[0].get("text", "") if parts else ""
        metadata = msg.get("metadata", {})
        callback_url = metadata.get("callbackUrl", "") or metadata.get("orchestratorCallbackUrl", "")
        source_paths = _normalize_source_paths(
            metadata.get("source_paths") or metadata.get("officeTargetPaths")
        )
        capability = _normalize_capability(
            str(
                metadata.get("capability")
                or metadata.get("officeCapability")
                or metadata.get("requestedCapability")
                or ""
            )
        )
        output_mode = str(
            metadata.get("output_mode") or metadata.get("officeOutputMode") or "workspace"
        ).strip().lower()
        if output_mode not in {"workspace", "inplace"}:
            output_mode = "workspace"

        # Get compass task ID for workspace scoping
        compass_task_id = metadata.get("compassTaskId", metadata.get("taskId", ""))

        # Create task via task store
        task_store = self.services.task_store
        canonical_task_id = compass_task_id or ""
        task = task_store.create_task(
            agent_id=self.definition.agent_id,
            metadata={
                "compass_task_id": compass_task_id,
                "user_text": user_text,
                "source_paths": source_paths,
                "capability": capability,
                "output_mode": output_mode,
            },
            task_id=canonical_task_id or None,
        )

        canonical_task_id = task.id

        # Setup logging - use compass_task_id if available, else own task id
        log_task_id = compass_task_id if compass_task_id else canonical_task_id
        log = AgentLogger(task_id=log_task_id, agent_name=self.definition.agent_id)
        log.node("handle_message", compass_task_id=compass_task_id, office_task_id=canonical_task_id,
                 request_preview=user_text[:200])
        _append_office_log(
            log_task_id,
            "[NODE] handle_message",
            compass_task_id=compass_task_id,
            office_task_id=canonical_task_id,
            request_preview=user_text[:200],
        )
        log.info("office agent started", output_mode=metadata.get("output_mode", "workspace"),
                 has_callback=bool(callback_url))
        _append_office_log(
            log_task_id,
            "office agent started",
            output_mode=metadata.get("output_mode", "workspace"),
            has_callback=bool(callback_url),
        )
        log.a2a("←", "compass", event="task received",
                compass_task_id=compass_task_id, office_task_id=canonical_task_id)
        _append_office_log(
            log_task_id,
            "[A2A] ← compass",
            capability=capability,
            compass_task_id=compass_task_id,
            office_task_id=canonical_task_id,
        )

        # Build initial state
        state: dict[str, Any] = {
            "_task_id": canonical_task_id,
            "_compass_task_id": compass_task_id,
            "_task_logger": log,
            "_message_metadata": dict(metadata),
            "_runtime": self.services.runtime,
            "_skills_registry": self.skills_registry,
            "_plugin_manager": self.plugin_manager,
            "_allowed_tools": metadata.get("allowed_tools"),
            "_permission_engine": getattr(self, "_permission_engine", None),
            "required_skills": list(self.definition.skills or []),
            "user_request": user_text,
            "output_mode": output_mode,
            "source_paths": source_paths,
            "capability": capability or "summarize",
            "test_cycles": 0,
        }

        # Set OFFICE_WORKSPACE_ROOT for this task
        # Workspace path: {ARTIFACT_ROOT}/{compass_task_id}/office/
        # All office tasks under the same compass task share the same workspace
        artifact_root = os.environ.get("ARTIFACT_ROOT", "")
        if artifact_root:
            # Use compass_task_id if available, otherwise use canonical_task_id for standalone operation
            ws_id = compass_task_id if compass_task_id else canonical_task_id
            workspace_root = os.path.join(artifact_root, ws_id, "office")
            artifacts_dir = os.path.join(workspace_root, "artifacts")
            os.makedirs(artifacts_dir, exist_ok=True)
            os.environ["OFFICE_WORKSPACE_ROOT"] = artifacts_dir
            log.info("office workspace prepared", workspace_root=workspace_root, artifacts_dir=artifacts_dir)
            _append_office_log(
                log_task_id,
                "office workspace prepared",
                workspace_root=workspace_root,
                artifacts_dir=artifacts_dir,
            )

        # Run workflow in background thread
        def _run() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                config = self._build_run_config(canonical_task_id, max_steps=50, timeout_seconds=3600)
                result = loop.run_until_complete(
                    self._compiled_workflow.invoke(state, config)
                )
                artifacts = [
                    Artifact(
                        name="office-result",
                        artifact_type="text/plain",
                        parts=[{"text": result.get("summary", "Office task completed.")}],
                        metadata={
                            "capability": result.get("capability", "summarize"),
                            "output_mode": result.get("output_mode", "workspace"),
                        },
                    )
                ]
                if result.get("success", False):
                    task_store.complete_task(canonical_task_id, artifacts=artifacts)
                else:
                    task_store.fail_task(canonical_task_id, result.get("summary", "Office task failed."))
                    return

                if callback_url:
                    _send_callback(callback_url, canonical_task_id, result)
            except Exception as exc:
                logger.exception(f"Office workflow failed: {exc}")
                task_store.fail_task(canonical_task_id, str(exc))
            finally:
                loop.close()

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()

        return task_store.get_task_dict(canonical_task_id)

    async def get_task(self, task_id: str) -> dict:
        """Return real task state from TaskStore."""
        return self.services.task_store.get_task_dict(task_id)


def _send_callback(callback_url: str, task_id: str, result: dict) -> None:
    """Send completion callback to orchestrator."""
    import urllib.request
    # Sanitize result - remove non-JSON-serializable objects
    safe_result = {
        "status": result.get("status", "completed"),
        "summary": result.get("summary", ""),
        "capability": result.get("capability", ""),
        "output_mode": result.get("output_mode", "workspace"),
        "warnings_count": result.get("warnings_count", 0),
    }
    payload = json.dumps({
        "task_id": task_id,
        "state": "completed",
        "result": safe_result,
    }).encode("utf-8")
    req = urllib.request.Request(
        callback_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# In-process dispatch (overrides HTTP dispatch)
# ---------------------------------------------------------------------------

