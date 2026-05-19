"""Constellation agent logging helpers.

Every agent that participates in a task writes to its own log file at:

    {ARTIFACT_ROOT}/{task_id}/{agent_name}/agent.log

The ``ARTIFACT_ROOT`` is resolved from the ``ARTIFACT_ROOT`` environment
variable (default ``artifacts/``).  When running inside a container the
host path must be mounted to the same ``ARTIFACT_ROOT`` value.

Log format (plain text, human-readable):

    2026-05-16 14:30:00 [INFO ] [team-lead] Starting implementation step=gather_context
    2026-05-16 14:30:01 [DEBUG] [team-lead] LLM response received tokens=512
    2026-05-16 14:30:02 [WARN ] [jira] Ticket not found key=PROJ-99
    2026-05-16 14:30:03 [ERROR] [web-agent] Build failed exit_code=1

Log levels (default DEBUG):
    DEBUG  10  — detailed processing within nodes/edges
    INFO   20  — major steps, A2A messages, LangGraph node transitions
    WARN   30  — recoverable issues
    ERROR  40  — failures that affect the task outcome

One agent must ONLY log to its own agent directory.  No agent may write
log entries on behalf of another agent.

Usage
-----
::

    from framework.devlog import AgentLogger

    log = AgentLogger(task_id="abc123", agent_name="team-lead")
    log.info("Starting gather_context node")
    log.debug("LLM call complete", tokens=512)
    log.warn("Jira ticket fetch returned empty fields")
    log.error("Build failed", exit_code=1, stderr=stderr[:200])

Backward-compatible alias
-------------------------
``WorkspaceLogger(workspace_path, agent_id)`` still works — it derives
``task_id`` from the last path component of ``workspace_path`` and wraps
the new ``AgentLogger``.
"""
from __future__ import annotations

import os
import time
from typing import Any

# ---------------------------------------------------------------------------
# Log levels
# ---------------------------------------------------------------------------

DEBUG = 10
INFO = 20
WARN = 30
ERROR = 40

_LEVEL_NAMES: dict[int, str] = {
    DEBUG: "DEBUG",
    INFO:  "INFO ",
    WARN:  "WARN ",
    ERROR: "ERROR",
}

_DEFAULT_LEVEL = DEBUG

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ts() -> str:
    """Return a human-readable local timestamp: ``2026-05-16 14:30:00``."""
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _artifact_root() -> str:
    """Return ARTIFACT_ROOT from env, defaulting to ``artifacts/``."""
    return os.environ.get("ARTIFACT_ROOT", "artifacts/")


def _format_kwargs(kwargs: dict) -> str:
    """Format extra key=value pairs for the log line."""
    if not kwargs:
        return ""
    parts = []
    for k, v in kwargs.items():
        v_str = str(v)
        # Keep values short to avoid unreadable single-line blobs
        if len(v_str) > 200:
            v_str = v_str[:197] + "..."
        parts.append(f"{k}={v_str!r}")
    return " " + " ".join(parts)


# ---------------------------------------------------------------------------
# AgentLogger — primary API
# ---------------------------------------------------------------------------

class AgentLogger:
    """Append-only logger scoped to one agent within one task.

    Log file path: ``{ARTIFACT_ROOT}/{task_id}/{agent_name}/agent.log``

    Parameters
    ----------
    task_id:
        The Compass task ID that owns this workflow.  All agents in the
        same user request share the same task_id.
    agent_name:
        The agent's identifier string (e.g. ``"team-lead"``, ``"jira"``).
        Must match the agent's own ID — do NOT pass another agent's name.
    level:
        Minimum log level to emit.  Defaults to ``DEBUG`` so all messages
        are captured during development.
    """

    def __init__(
        self,
        task_id: str,
        agent_name: str,
        level: int = _DEFAULT_LEVEL,
    ) -> None:
        self._task_id = task_id or "unknown-task"
        self._agent_name = agent_name
        self._level = level
        self._log_path: str = ""

        if not task_id or not agent_name:
            return

        agent_dir = os.path.join(_artifact_root(), self._task_id, agent_name)
        try:
            os.makedirs(agent_dir, exist_ok=True)
        except OSError:
            return  # non-fatal

        self._log_path = os.path.join(agent_dir, "agent.log")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def debug(self, message: str, **kwargs: Any) -> None:
        """Write a DEBUG-level log entry."""
        self._write(DEBUG, message, **kwargs)

    def info(self, message: str, **kwargs: Any) -> None:
        """Write an INFO-level log entry."""
        self._write(INFO, message, **kwargs)

    def warn(self, message: str, **kwargs: Any) -> None:
        """Write a WARN-level log entry."""
        self._write(WARN, message, **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        """Write an ERROR-level log entry."""
        self._write(ERROR, message, **kwargs)

    def node(self, node_name: str, **kwargs: Any) -> None:
        """Write an INFO entry marking a LangGraph node entry (graph transition)."""
        self._write(INFO, f"[NODE] {node_name}", **kwargs)

    def edge(self, from_node: str, to_node: str, **kwargs: Any) -> None:
        """Write an INFO entry marking a LangGraph edge (routing decision)."""
        self._write(INFO, f"[EDGE] {from_node} → {to_node}", **kwargs)

    def a2a(self, direction: str, target: str, capability: str = "", **kwargs: Any) -> None:
        """Write an INFO entry for an A2A message send/receive."""
        self._write(INFO, f"[A2A] {direction} {target}", capability=capability, **kwargs)

    # Backward-compat alias
    def step(self, step_name: str, **kwargs: Any) -> None:
        """Alias for ``node()`` — kept for backward compatibility."""
        self.node(step_name, **kwargs)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write(self, level: int, message: str, **kwargs: Any) -> None:
        """Append a single log line in plain-text format."""
        if level < self._level:
            return
        if not self._log_path:
            return
        level_str = _LEVEL_NAMES.get(level, "?????")
        extra = _format_kwargs(kwargs)
        line = f"{_ts()} [{level_str}] [{self._agent_name}] {message}{extra}\n"
        try:
            with open(self._log_path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass  # non-fatal: console print() remains the primary stream


# ---------------------------------------------------------------------------
# WorkspaceLogger — backward-compatible wrapper
# ---------------------------------------------------------------------------

class WorkspaceLogger:
    """Backward-compatible wrapper around AgentLogger.

    Old call sites use ``WorkspaceLogger(workspace_path, agent_id)`` where
    ``workspace_path`` is structured as ``{ARTIFACT_ROOT}/{task_id}``.
    This wrapper derives ``task_id`` from ``workspace_path`` and delegates
    to ``AgentLogger``.
    """

    def __init__(self, workspace_path: str, agent_id: str, level: int = _DEFAULT_LEVEL) -> None:
        # Derive task_id: last non-empty component of workspace_path
        task_id = ""
        if workspace_path:
            task_id = os.path.basename(workspace_path.rstrip("/\\"))
        self._inner = AgentLogger(task_id=task_id, agent_name=agent_id, level=level)

    def debug(self, message: str, **kwargs: Any) -> None:
        self._inner.debug(message, **kwargs)

    def info(self, message: str, **kwargs: Any) -> None:
        self._inner.info(message, **kwargs)

    def warn(self, message: str, **kwargs: Any) -> None:
        self._inner.warn(message, **kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        self._inner.error(message, **kwargs)

    def step(self, step_name: str, **kwargs: Any) -> None:
        self._inner.node(step_name, **kwargs)

    def node(self, node_name: str, **kwargs: Any) -> None:
        self._inner.node(node_name, **kwargs)

    def edge(self, from_node: str, to_node: str, **kwargs: Any) -> None:
        self._inner.edge(from_node, to_node, **kwargs)

    def a2a(self, direction: str, target: str, **kwargs: Any) -> None:
        self._inner.a2a(direction, target, **kwargs)


# ---------------------------------------------------------------------------
# Convenience: get log path without creating the logger
# ---------------------------------------------------------------------------

def get_agent_log_path(task_id: str, agent_name: str) -> str:
    """Return the log file path for an agent/task pair without creating it."""
    if not task_id or not agent_name:
        return ""
    return os.path.join(_artifact_root(), task_id, agent_name, "agent.log")
