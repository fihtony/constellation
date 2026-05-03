"""Progress reporting tool for agentic runtimes.

Allows a dev agent or team lead agent to report progress back to the parent
(Compass) via the callback URL.  Import this module to register the tool.
"""

from __future__ import annotations

import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen

from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import register_tool

_COMPASS_URL = os.environ.get("COMPASS_URL", "http://compass:8080")
_TASK_ID = os.environ.get("TASK_ID", "")


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
                },
                "required": ["message"],
            },
        )

    def execute(self, args: dict) -> dict:
        message = args.get("message", "").strip()
        step = args.get("step", "progress").strip()
        task_id = args.get("task_id", "").strip() or _TASK_ID

        if not message:
            return self.error("Missing required argument: message")

        if not task_id:
            # No task_id available — log locally and return success
            print(f"[progress] step={step} msg={message}")
            return self.ok(f"Progress logged locally (no task_id): {message}")

        payload = {"step": step, "message": message}
        url = f"{_COMPASS_URL}/tasks/{task_id}/progress"
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
