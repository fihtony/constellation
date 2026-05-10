"""UI Design tools for agentic runtimes.

Provides tools to fetch design specs from Figma/Stitch via the UI Design Agent.
Import this module to register all design tools.
"""

from __future__ import annotations

import json
import os
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from common.tools.agent_discovery import discover_capability_url
from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import register_tool

_REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")
_ACK_TIMEOUT = int(os.environ.get("A2A_ACK_TIMEOUT_SECONDS", "15"))
_TASK_TIMEOUT = int(os.environ.get("A2A_TASK_TIMEOUT_SECONDS", "60"))
_TASK_POLL_INTERVAL = float(os.environ.get("A2A_TASK_POLL_INTERVAL_SECONDS", "1.0"))


def _figma_capability_for_args(args: dict) -> str:
    if str(args.get("node_id") or "").strip():
        return "figma.node.get"
    return "figma.page.fetch"


def _discover_design_url(capability: str) -> str | None:
    try:
        return discover_capability_url(_REGISTRY_URL, capability)
    except Exception:  # noqa: BLE001
        return None


def _a2a_send(agent_url: str, capability: str, params: dict) -> dict:
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
    status.setdefault(
        "message",
        {"parts": [{"text": f"UI Design task {task_id} timed out"}]},
    )
    return last_task


class FigmaFetchScreenTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="design_fetch_figma_screen",
            description=(
                "Fetch a Figma screen / component specification. "
                "Returns the design spec including layout, colors, and component details."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "figma_url": {
                        "type": "string",
                        "description": "Figma node URL or file URL",
                    },
                    "node_id": {
                        "type": "string",
                        "description": "Optional Figma node ID to fetch a specific component",
                    },
                },
                "required": ["figma_url"],
            },
        )

    def execute(self, args: dict) -> dict:
        capability = _figma_capability_for_args(args)
        url = _discover_design_url(capability)
        if not url:
            return self.error(f"UI Design Agent is not available (capability {capability} not found).")
        try:
            result = _a2a_send(url, capability, args)
            return self.ok(json.dumps(result, ensure_ascii=False, indent=2))
        except (URLError, OSError) as exc:
            return self.error(f"Failed to fetch Figma screen: {exc}")


class StitchFetchScreenTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="design_fetch_stitch_screen",
            description="Fetch a Stitch design screen specification.",
            input_schema={
                "type": "object",
                "properties": {
                    "screen_id": {
                        "type": "string",
                        "description": "Stitch screen ID",
                    },
                },
                "required": ["screen_id"],
            },
        )

    def execute(self, args: dict) -> dict:
        capability = "stitch.screen.fetch"
        url = _discover_design_url(capability)
        if not url:
            return self.error(f"UI Design Agent is not available (capability {capability} not found).")
        try:
            result = _a2a_send(url, capability, args)
            return self.ok(json.dumps(result, ensure_ascii=False, indent=2))
        except (URLError, OSError) as exc:
            return self.error(f"Failed to fetch Stitch screen: {exc}")


register_tool(FigmaFetchScreenTool())
register_tool(StitchFetchScreenTool())
