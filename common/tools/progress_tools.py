"""Progress reporting tool for agentic runtimes.

Allows a dev agent or team lead agent to report progress back to the parent
(Compass) via the callback URL.  Import this module to register the tool.
"""

from __future__ import annotations

import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen

from common.agent_directory import AgentDirectory
from common.orchestrator import resolve_orchestrator_base_url
from common.registry_client import RegistryClient
from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import register_tool

_TASK_ID = os.environ.get("TASK_ID", "")
_AGENT_ID = os.environ.get("AGENT_ID", "")
_AGENT_DIRECTORY: AgentDirectory | None = None


def _get_effective_task_id() -> str:
    """Return the orchestrator (Compass) task ID for progress reporting.

    Preference order:
    1. ``compassTaskId`` from the control-tools task context — set explicitly
       by agent configure_*_control_tools() with the parent compass task ID.
    2. ``taskId`` from the task context (fallback).
    3. ``TASK_ID`` env var (legacy / host-process agents).
    """
    try:
        from common.tools.control_tools import _task_context as _ctx  # noqa: PLC0415
        compass = str(_ctx.get("compassTaskId") or "").strip()
        if compass:
            return compass
        task = str(_ctx.get("taskId") or "").strip()
        if task:
            return task
    except Exception:  # noqa: BLE001
        pass
    return _TASK_ID


def _get_effective_agent_id() -> str:
    """Return the agent ID to include in progress payloads."""
    try:
        from common.tools.control_tools import _task_context as _ctx  # noqa: PLC0415
        agent = str(_ctx.get("agentId") or "").strip()
        if agent:
            return agent
    except Exception:  # noqa: BLE001
        pass
    return _AGENT_ID


def _get_agent_directory() -> AgentDirectory | None:
    global _AGENT_DIRECTORY
    if _AGENT_DIRECTORY is not None:
        return _AGENT_DIRECTORY

    registry_url = str(os.environ.get("REGISTRY_URL") or "").strip()
    owner_agent_id = str(os.environ.get("AGENT_ID") or "progress-tool").strip() or "progress-tool"
    if not registry_url:
        return None

    try:
        _AGENT_DIRECTORY = AgentDirectory(owner_agent_id, RegistryClient(registry_url))
    except Exception as exc:  # noqa: BLE001
        print(f"[progress] warning: could not initialize agent directory: {exc}")
        _AGENT_DIRECTORY = None
    return _AGENT_DIRECTORY


def _resolve_progress_base_url(args: dict) -> str:
    payload = {
        "orchestratorCallbackUrl": (
            str(args.get("orchestrator_callback_url") or "").strip()
            or str(os.environ.get("ORCHESTRATOR_CALLBACK_URL") or "").strip()
        ),
        "orchestratorUrl": (
            str(args.get("orchestrator_url") or "").strip()
            or str(os.environ.get("ORCHESTRATOR_URL") or "").strip()
            or str(os.environ.get("COMPASS_URL") or "").strip()
        ),
        "compassUrl": str(os.environ.get("COMPASS_URL") or "").strip(),
    }
    return resolve_orchestrator_base_url(payload, agent_directory=_get_agent_directory())


class ReportProgressTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="report_progress",
            description=(
                "Report a progress update to the parent orchestrator. "
                "Call this after completing a significant step so the user "
                "can see what the agent is doing."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "message": {
                        "type": "string",
                        "description": "Human-readable progress message",
                    },
                    "step": {
                        "type": "string",
                        "description": "Short step identifier, e.g. 'build', 'test', 'push'",
                    },
                    "task_id": {
                        "type": "string",
                        "description": "Task ID (optional; defaults to TASK_ID env var)",
                    },
                    "orchestrator_callback_url": {
                        "type": "string",
                        "description": "Optional callback URL for the parent orchestrator",
                    },
                    "orchestrator_url": {
                        "type": "string",
                        "description": "Optional base URL for the parent orchestrator",
                    },
                },
                "required": ["message"],
            },
        )

    def execute(self, args: dict) -> dict:
        message = args.get("message", "").strip()
        step = args.get("step", "progress").strip()
        # Prefer explicitly passed task_id; fall back to compass task ID from context.
        task_id = args.get("task_id", "").strip() or _get_effective_task_id()
        agent_id = args.get("agent_id", "").strip() or _get_effective_agent_id()

        if not message:
            return self.error("Missing required argument: message")

        if not task_id:
            # No task_id available — log locally and return success
            print(f"[progress] step={step} msg={message}")
            return self.ok(f"Progress logged locally (no task_id): {message}")

        payload = {"step": step, "message": message, "agentId": agent_id}
        orchestrator_base_url = _resolve_progress_base_url(args)
        if not orchestrator_base_url:
            print(f"[progress] step={step} msg={message}")
            return self.ok(f"Progress logged locally (no orchestrator URL): {message}")

        url = f"{orchestrator_base_url}/tasks/{task_id}/progress"
        req = Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(req, timeout=5) as resp:
                resp.read()
            return self.ok(f"Progress reported: {message}")
        except (URLError, OSError) as exc:
            # Non-fatal — print and continue
            print(f"[progress] warning: could not report to {url}: {exc}")
            return self.ok(f"Progress logged (callback failed): {message}")


register_tool(ReportProgressTool())
