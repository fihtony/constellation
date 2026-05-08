"""Structured debug logging helpers for development-time agent tracing."""

from __future__ import annotations

import json
import os
import sys
import threading

from common.time_utils import local_clock_time, local_iso_timestamp


def preview_data(value, limit=4000):
    if isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n...[truncated]..."


def debug_log(actor, event, **fields):
    payload = {
        "ts": local_iso_timestamp(),
        "actor": actor,
        "event": event,
        **fields,
    }
    print(f"[debug] {json.dumps(payload, ensure_ascii=False, default=str)}")


def _read_workspace_json(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _agent_identity_key(value):
    normalized = str(value or "").strip().rstrip("/").replace("_", "-")
    if normalized.endswith("-agent"):
        normalized = normalized[:-6]
    return normalized


def _agent_display_name(agent_id):
    normalized = _agent_identity_key(agent_id)
    if not normalized:
        return ""
    words = [part.capitalize() for part in normalized.split("-") if part]
    return " ".join(words) + " Agent"


def record_workspace_stage(workspace_path, relative_dir, phase, *, task_id="", extra=None):
    if not workspace_path or not relative_dir:
        return

    agent_dir = os.path.join(workspace_path, relative_dir)
    os.makedirs(agent_dir, exist_ok=True)

    extra = extra or {}
    source_agent = extra.get("sourceAgent") or extra.get("sourceAgentId") or ""
    source_prefix = ""
    if source_agent and _agent_identity_key(source_agent) != _agent_identity_key(relative_dir):
        display_name = extra.get("sourceAgentName") or _agent_display_name(source_agent)
        if display_name:
            source_prefix = f" [{display_name}]"

    entry = f"[{local_clock_time()}]{source_prefix} {phase}"
    log_path = os.path.join(agent_dir, "command-log.txt")
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(entry + "\n")

    summary_path = os.path.join(agent_dir, "stage-summary.json")
    summary = _read_workspace_json(summary_path)
    summary.update(extra)
    if task_id:
        summary["taskId"] = task_id
    summary["agentId"] = summary.get("agentId") or relative_dir.rstrip("/")
    summary["currentPhase"] = phase
    summary.pop("phases", None)
    summary.pop("phasesLog", None)
    summary["updatedAt"] = local_iso_timestamp()
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Stdout tee — capture all console output to command-log.txt
# ---------------------------------------------------------------------------

class _TeeToFile:
    """Write to both the original stdout and a log file.

    Thread-safe: each write acquires a file-level lock so concurrent print()
    calls from multiple threads never interleave partial lines in the file.
    Only intended for single-task per-task agents (office, team-lead, web,
    android) where one tee instance covers the full process lifetime.
    """

    def __init__(self, original, log_path: str) -> None:
        self._original = original
        self._log_path = log_path
        self._lock = threading.Lock()

    def write(self, text: str) -> int:
        self._original.write(text)
        if text:
            try:
                with self._lock:
                    with open(self._log_path, "a", encoding="utf-8", errors="replace") as fh:
                        fh.write(text)
            except Exception:  # noqa: BLE001
                pass
        return len(text)

    def flush(self) -> None:
        self._original.flush()

    def fileno(self) -> int:
        return self._original.fileno()

    def isatty(self) -> bool:
        try:
            return self._original.isatty()
        except Exception:  # noqa: BLE001
            return False

    def __getattr__(self, name: str):
        return getattr(self._original, name)


def install_stdout_tee(log_path: str) -> _TeeToFile:
    """Redirect sys.stdout so that ALL print() output is also appended to *log_path*.

    Call this once at the beginning of a per-task agent workflow.  The returned
    tee object replaces sys.stdout for the lifetime of the process.  The
    directory for *log_path* is created automatically if it does not exist.

    Returns the installed tee so callers can restore sys.stdout if needed::

        tee = install_stdout_tee("/app/artifacts/.../command-log.txt")
    """
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
    except OSError:
        pass
    tee = _TeeToFile(sys.stdout, log_path)
    sys.stdout = tee
    return tee


# ---------------------------------------------------------------------------
# Initial stage-summary helper
# ---------------------------------------------------------------------------

def write_initial_stage_summary(
    workspace_path: str,
    relative_dir: str,
    task_id: str,
    agent_id: str,
    **extra,
) -> None:
    """Write (or overwrite) stage-summary.json with currentPhase=STARTING.

    Should be called immediately after the agent workspace directory is created,
    before any long-running work begins, so that the file is always present.
    Subsequent calls to ``record_workspace_stage`` will update it in-place.
    """
    if not workspace_path or not relative_dir:
        return
    agent_dir = os.path.join(workspace_path, relative_dir)
    try:
        os.makedirs(agent_dir, exist_ok=True)
    except OSError:
        return
    now = local_iso_timestamp()
    payload: dict = {
        "taskId": task_id,
        "agentId": agent_id,
        "currentPhase": "STARTING",
        "startedAt": now,
        "updatedAt": now,
    }
    payload.update(extra)
    try:
        with open(os.path.join(agent_dir, "stage-summary.json"), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass