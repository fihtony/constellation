"""Office Agent — Graph outside, ReAct inside.

Handles document summarization (PDF/DOCX/TXT) and CSV data analysis.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
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
        permission_profile=cfg.get("permission_profile", "office_readonly"),
        runtime_backend=cfg.get("runtime_backend", "connect-agent"),
        model=cfg.get("model", "gpt-5-mini"),
        workflow=office_workflow,
        config=cfg,
    )


office_definition = _build_office_definition()


# ---------------------------------------------------------------------------
# OfficeAgent
# ---------------------------------------------------------------------------

class OfficeAgent(BaseAgent):
    async def start(self) -> None:
        await super().start()
        register_office_tools()
        _register_office_dispatch(self)

    async def handle_message(self, message: dict) -> dict:
        """Handle incoming A2A message.

        Non-blocking: returns task dict immediately, runs workflow in background thread.
        """
        from framework.workflow import RunConfig

        msg = message.get("message", message)
        parts = msg.get("parts", [])
        user_text = parts[0].get("text", "") if parts else ""
        metadata = msg.get("metadata", {})
        callback_url = metadata.get("callbackUrl", "") or metadata.get("orchestratorCallbackUrl", "")

        # Get compass task ID for workspace scoping
        compass_task_id = metadata.get("compassTaskId", metadata.get("taskId", ""))

        # Create task via task store
        task_store = self.services.task_store
        task = task_store.create_task(
            agent_id=self.definition.agent_id,
            metadata={
                "compass_task_id": compass_task_id,
                "user_text": user_text,
            },
        )

        canonical_task_id = task.id

        # Build initial state
        state: dict[str, Any] = {
            "_task_id": canonical_task_id,
            "_compass_task_id": compass_task_id,
            "_runtime": self.services.runtime,
            "_skills_registry": self.skills_registry,
            "_plugin_manager": self.plugin_manager,
            "_allowed_tools": metadata.get("allowed_tools"),
            "_permission_engine": getattr(self, "_permission_engine", None),
            "user_request": user_text,
            "output_mode": metadata.get("output_mode", "workspace"),
            "source_paths": [],
            "capability": "summarize",
            "test_cycles": 0,
        }

        # Set OFFICE_WORKSPACE_ROOT for this task
        artifact_root = os.environ.get("ARTIFACT_ROOT", "")
        if artifact_root and compass_task_id:
            workspace_root = os.path.join(artifact_root, compass_task_id, canonical_task_id, "office")
            artifacts_dir = os.path.join(workspace_root, "artifacts")
            os.makedirs(artifacts_dir, exist_ok=True)
            os.environ["OFFICE_WORKSPACE_ROOT"] = artifacts_dir

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
                task_store.complete_task(canonical_task_id, artifacts=artifacts)

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


def _send_callback(callback_url: str, task_id: str, result: dict) -> None:
    """Send completion callback to orchestrator."""
    import urllib.request
    payload = json.dumps({
        "task_id": task_id,
        "state": "completed",
        "result": result,
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

def _register_office_dispatch(office_agent: "OfficeAgent") -> None:
    """Register in-process dispatch_office_task tool (overrides HTTP-based dispatch)."""
    from framework.tools.base import BaseTool, ToolResult
    from framework.tools.registry import get_registry

    class InProcessDispatchOffice(BaseTool):
        name = "dispatch_office_task"
        description = "Dispatch an office task (summarize documents, analyze CSV data) to the Office Agent."
        parameters_schema = {
            "type": "object",
            "properties": {
                "task_description": {"type": "string", "description": "Task description text"},
                "output_mode": {"type": "string", "description": "Output mode: workspace or inplace"},
                "source_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Source file/folder paths",
                },
            },
            "required": ["task_description"],
        }

        def execute_sync(
            self,
            task_description: str = "",
            output_mode: str = "workspace",
            source_paths: list = None,
        ) -> ToolResult:
            from framework.a2a.client import dispatch_sync

            source_paths = source_paths or []
            msg = {
                "message": {
                    "messageId": f"dispatch-office-{id(self)}",
                    "role": "ROLE_USER",
                    "parts": [{"text": task_description}],
                    "metadata": {
                        "output_mode": output_mode,
                        "source_paths": source_paths,
                    },
                }
            }
            try:
                result = dispatch_sync(
                    agent_id="office",
                    message=msg,
                    timeout=3600,
                )
                return ToolResult(output=json.dumps(result))
            except Exception as exc:
                return ToolResult(output="", error=f"dispatch_office_task: {exc}")

    registry = get_registry()
    try:
        registry.unregister("dispatch_office_task")
    except KeyError:
        pass
    registry.register(InProcessDispatchOffice())