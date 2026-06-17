"""Constellation agent logging helpers.

Every agent that participates in a task writes to its own log file at:

    {ARTIFACT_ROOT}/{task_id}/{agent_name}/agent.log

The ``ARTIFACT_ROOT`` is resolved from the ``ARTIFACT_ROOT`` environment
variable (default ``artifacts/``).  When running inside a container the
host path must be mounted to the same ``ARTIFACT_ROOT`` value.

Log format (plain text, human-readable, UTC ISO with offset):

    2026-06-01T12:34:56+00:00 [INFO ] [team-lead] Starting implementation step=gather_context
    2026-06-01T12:34:57+00:00 [DEBUG] [team-lead] LLM response received tokens=512
    2026-06-01T12:34:58+00:00 [WARN ] [jira] Ticket not found key=PROJ-99
    2026-06-01T12:34:59+00:00 [ERROR] [web-agent] Build failed exit_code=1

All timestamps are emitted in UTC with an explicit ``+00:00`` offset so
that the Compass UI ``parseTimestamp`` function (in
``agents/compass/ui/templates.py``) can convert them to the viewer's
local timezone without ambiguity. The legacy naive ``YYYY-MM-DD
HH:MM:SS`` format is still accepted by the log aggregator for backward
compatibility with existing log files, but new lines MUST use the UTC
ISO form.

Log levels (default DEBUG):

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
from datetime import datetime
from pathlib import Path
from typing import Any


def _apply_default_timezone() -> None:
    """Resolve the default timezone from ``config/constellation.yaml``
    and apply it to ``os.environ['TZ']`` if no override is already set.

    This makes ``config/constellation.yaml:default_tz`` the single
    source of truth for every agent's wall-clock zone, regardless of
    how the process is launched (docker compose, ``python -m
    agents.<x>``, pytest, etc.). Operators can still override per
    deployment by exporting ``TZ`` in the shell, by setting ``TZ`` in
    ``config/.env`` (which docker compose forwards to every
    container), or by setting ``TZ`` in the test environment.
    """
    if os.environ.get("TZ"):
        return  # caller already pinned the zone — defer to them
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:  # pragma: no cover - yaml is a hard dep
        return
    project_root = Path(__file__).resolve().parent.parent
    yaml_path = project_root / "config" / "constellation.yaml"
    if not yaml_path.is_file():
        return
    try:
        with open(yaml_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except OSError:
        return
    default_tz = data.get("default_tz")
    if not default_tz or not isinstance(default_tz, str):
        return
    os.environ["TZ"] = default_tz
    # POSIX needs an explicit tzset so ``localtime`` reflects the new
    # zone immediately. ``time.tzset`` is unavailable on Windows; on
    # Linux/macOS (where our containers run) it is a no-op if the
    # value did not change.
    if hasattr(time, "tzset"):
        try:
            time.tzset()
        except OSError:
            pass


_apply_default_timezone()

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
    """Return the local-time ISO-8601 timestamp with explicit offset.

    Example: ``2026-06-01T08:34:56-07:00`` (container TZ = America/Los_Angeles).

    The container's ``TZ`` environment variable determines the wall-clock
    zone; agents MUST set ``TZ`` in their container definitions
    (``docker-compose-v2.yml``) so the produced offset is meaningful. The
    emitted offset is what makes the Compass UI ``parseTimestamp`` able
    to convert the timestamp to the viewer's local clock without
    ambiguity: the browser knows the exact instant the line was
    written, then renders it in whatever timezone the viewer is in.

    The colon in the offset is mandatory: the JS ``parseTimestamp``
    regex requires ``[+-]HH:MM`` (not ``[+-]HHMM``), which is the form
    ``datetime.isoformat(timespec="seconds")`` produces.
    """
    # ``astimezone()`` with no argument converts a naive ``now()`` to an
    # aware datetime in the process's local zone (driven by the ``TZ``
    # environment variable on POSIX, the host clock on Windows). The
    # resulting ``isoformat(timespec="seconds")`` carries the offset.
    return datetime.now().astimezone().isoformat(timespec="seconds")


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

    def debug(self, msg: str = "", **kwargs: Any) -> None:
        """Write a DEBUG-level log entry."""
        self._write(DEBUG, msg, **kwargs)

    def info(self, msg: str = "", **kwargs: Any) -> None:
        """Write an INFO-level log entry."""
        self._write(INFO, msg, **kwargs)

    def warn(self, msg: str = "", **kwargs: Any) -> None:
        """Write a WARN-level log entry."""
        self._write(WARN, msg, **kwargs)

    def error(self, msg: str = "", **kwargs: Any) -> None:
        """Write an ERROR-level log entry."""
        self._write(ERROR, msg, **kwargs)

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

    def _write(self, _level_value: int, _message_text: str, **kwargs: Any) -> None:
        """Append a single log line in plain-text format."""
        if _level_value < self._level:
            return
        if not self._log_path:
            return
        level_str = _LEVEL_NAMES.get(_level_value, "?????")
        extra = _format_kwargs(kwargs)
        line = f"{_ts()} [{level_str}] [{self._agent_name}] {_message_text}{extra}\n"
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

    def debug(self, msg: str = "", **kwargs: Any) -> None:
        self._inner.debug(msg, **kwargs)

    def info(self, msg: str = "", **kwargs: Any) -> None:
        self._inner.info(msg, **kwargs)

    def warn(self, msg: str = "", **kwargs: Any) -> None:
        self._inner.warn(msg, **kwargs)

    def error(self, msg: str = "", **kwargs: Any) -> None:
        self._inner.error(msg, **kwargs)

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
