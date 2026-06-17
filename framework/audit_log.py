"""Shared per-agent audit-log helpers.

Every agent that participates in a task workspace writes two audit-trail
artifacts alongside its ``agent.log``:

* ``command-log.txt`` — append-only, time-stamped record of every node /
  tool invocation, so an operator can reconstruct the order of actions
  without re-parsing the verbose agent.log.
* ``stage-summary.json`` — overwritten at each major stage transition,
  carrying a JSON snapshot of completed/pending/failed steps for the
  current stage.

These artifacts are referenced by the live container e2e suite, which
expects at least one of them under each per-task ``<agent>/`` directory.
Keeping the writer here means web-dev, code-review, team-lead, office,
and any future agent can drop them in one consistent location without
re-implementing the format.

All helpers are best-effort and never raise — audit-log failures must
never abort a real task.
"""
from __future__ import annotations

import json
import os
import threading
import time
from typing import Any

# Serialize command-log appends per-process to avoid interleaved writes
# when multiple workflow nodes execute concurrently within one container.
_AUDIT_LOCK = threading.Lock()
_AUDIT_CONTEXT = threading.local()


def _agent_audit_dir(workspace_path: str, agent_id: str) -> str:
    """Return ``<workspace>/<agent_id>`` and create it if missing."""
    audit_dir = os.path.join(workspace_path, agent_id)
    try:
        os.makedirs(audit_dir, exist_ok=True)
    except OSError:
        return audit_dir
    return audit_dir


def set_permission_audit_context(
    *,
    workspace_path: str,
    agent_id: str,
    task_id: str = "",
) -> None:
    """Set the current thread's permission-denial audit destination."""
    _AUDIT_CONTEXT.workspace_path = workspace_path or ""
    _AUDIT_CONTEXT.agent_id = agent_id or ""
    _AUDIT_CONTEXT.task_id = task_id or ""


def clear_permission_audit_context() -> None:
    """Clear the current thread's permission-denial audit destination."""
    for attr in ("workspace_path", "agent_id", "task_id"):
        try:
            delattr(_AUDIT_CONTEXT, attr)
        except AttributeError:
            pass


def permission_audit_context() -> dict[str, str]:
    """Return the current thread's audit context, if any."""
    return {
        "workspace_path": getattr(_AUDIT_CONTEXT, "workspace_path", "") or "",
        "agent_id": getattr(_AUDIT_CONTEXT, "agent_id", "") or "",
        "task_id": getattr(_AUDIT_CONTEXT, "task_id", "") or "",
    }


def append_permission_denial(
    *,
    workspace_path: str,
    agent_id: str,
    operation: str,
    reason: str,
    task_id: str = "",
    metadata: dict[str, Any] | None = None,
) -> str:
    """Append one structured permission denial to ``permission-denials.jsonl``.

    The record is intentionally line-oriented JSON so humans can review it
    with standard tools and automation can aggregate denials across agents.
    The helper is best-effort and never raises.
    """
    if not workspace_path or not agent_id or not operation:
        return ""
    audit_dir = _agent_audit_dir(workspace_path, agent_id)
    log_path = os.path.join(audit_dir, "permission-denials.jsonl")
    payload: dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "agent_id": agent_id,
        "task_id": task_id or "",
        "operation": operation,
        "status": "denied",
        "reason": reason or "",
    }
    if metadata:
        payload.update(metadata)
        payload["metadata"] = metadata
    try:
        line = json.dumps(payload, ensure_ascii=False, default=str) + "\n"
        with _AUDIT_LOCK:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line)
        return log_path
    except (OSError, TypeError, ValueError):
        return ""


def append_current_permission_denial(
    *,
    operation: str,
    reason: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    """Append a permission denial using the current thread audit context."""
    ctx = permission_audit_context()
    return append_permission_denial(
        workspace_path=ctx.get("workspace_path", ""),
        agent_id=ctx.get("agent_id", ""),
        task_id=ctx.get("task_id", ""),
        operation=operation,
        reason=reason,
        metadata=metadata,
    )


def append_command_log(
    workspace_path: str,
    agent_id: str,
    action: str,
    *,
    params: dict[str, Any] | None = None,
    step_id: int | str | None = None,
) -> None:
    """Append a single timestamped row to ``<workspace>/<agent>/command-log.txt``.

    The format is line-oriented and best-effort so it can be tailed
    cheaply from outside the container.  ``params`` is JSON-encoded with
    ``ensure_ascii=False`` to preserve non-ASCII task descriptions.
    """
    if not workspace_path or not agent_id or not action:
        return
    audit_dir = _agent_audit_dir(workspace_path, agent_id)
    log_path = os.path.join(audit_dir, "command-log.txt")
    try:
        encoded = json.dumps(params or {}, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        encoded = "{}"
    entry = (
        f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] "
        f"STEP {step_id if step_id not in (None, '') else '?'}: "
        f"{action} {encoded}\n"
    )
    try:
        with _AUDIT_LOCK:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(entry)
    except OSError:
        # Audit logging must never abort the real workflow.
        pass


def write_stage_summary(
    workspace_path: str,
    agent_id: str,
    stage: str,
    *,
    completed_steps: list[Any] | None = None,
    pending_steps: list[Any] | None = None,
    warnings: list[Any] | None = None,
    errors: list[Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> str:
    """Overwrite ``<workspace>/<agent>/stage-summary.json`` with the latest snapshot.

    Returns the absolute path written (empty string on failure).
    """
    if not workspace_path or not agent_id or not stage:
        return ""
    audit_dir = _agent_audit_dir(workspace_path, agent_id)
    summary_path = os.path.join(audit_dir, "stage-summary.json")
    payload: dict[str, Any] = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "agent_id": agent_id,
        "stage": stage,
        "completed_steps": list(completed_steps or []),
        "pending_steps": list(pending_steps or []),
        "warnings": list(warnings or []),
        "errors": list(errors or []),
    }
    if extra:
        for key, value in extra.items():
            payload.setdefault(key, value)
    try:
        with open(summary_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
        return summary_path
    except OSError:
        return ""
