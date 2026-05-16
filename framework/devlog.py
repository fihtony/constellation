"""Workspace logging helpers for Constellation agents.

Every agent that participates in a task should create a ``WorkspaceLogger``
and write to it throughout the task lifecycle.  Log files live at:

    {workspace_path}/{agent_id}/agent.log

This gives operators a single file per agent per task that captures the
complete execution trace — timestamps, step progress, errors, and outcomes.

Usage
-----
::

    logger = WorkspaceLogger(workspace_path, "web-dev")
    logger.info("Starting implementation", step="implement_changes")
    logger.error("Build failed", step="run_tests", details=err_msg)
    logger.info("PR created", step="create_pr", pr_url=pr_url)
"""
from __future__ import annotations

import json
import os
import time
from typing import Any


def _ts() -> str:
    """Return ISO-8601 local timestamp string."""
    return time.strftime("%Y-%m-%dT%H:%M:%S")


class WorkspaceLogger:
    """Append-only, thread-safe logger that writes to a workspace log file.

    Parameters
    ----------
    workspace_path:
        Root workspace directory for the task.
    agent_id:
        Identifier for this agent (e.g. ``"web-dev"``, ``"team-lead"``).
        The log file is written to ``{workspace_path}/{agent_id}/agent.log``.
    """

    def __init__(self, workspace_path: str, agent_id: str) -> None:
        self._agent_id = agent_id
        self._log_path: str = ""

        if not workspace_path:
            return

        agent_dir = os.path.join(workspace_path, agent_id)
        try:
            os.makedirs(agent_dir, exist_ok=True)
        except OSError:
            return  # non-fatal: logging is best-effort

        self._log_path = os.path.join(agent_dir, "agent.log")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def info(self, message: str, **kwargs: Any) -> None:
        """Write an INFO-level log entry."""
        self._write("INFO", message, **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        """Write an ERROR-level log entry."""
        self._write("ERROR", message, **kwargs)

    def warn(self, message: str, **kwargs: Any) -> None:
        """Write a WARN-level log entry."""
        self._write("WARN", message, **kwargs)

    def step(self, step_name: str, **kwargs: Any) -> None:
        """Write a step-start INFO entry (convenience wrapper)."""
        self._write("STEP", f"→ {step_name}", **kwargs)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write(self, level: str, message: str, **kwargs: Any) -> None:
        """Append a single log line in JSON-line format."""
        if not self._log_path:
            return
        entry: dict[str, Any] = {
            "ts": _ts(),
            "level": level,
            "agent": self._agent_id,
            "msg": message,
        }
        entry.update(kwargs)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        try:
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass  # non-fatal: console print() is the primary log stream


def get_agent_log_path(workspace_path: str, agent_id: str) -> str:
    """Return the expected log file path for an agent without creating it."""
    return os.path.join(workspace_path, agent_id, "agent.log") if workspace_path else ""
