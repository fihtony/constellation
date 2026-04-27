"""Structured debug logging helpers for development-time agent tracing."""

from __future__ import annotations

import json
import os

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


def record_workspace_stage(workspace_path, relative_dir, phase, *, task_id="", extra=None):
    if not workspace_path or not relative_dir:
        return

    agent_dir = os.path.join(workspace_path, relative_dir)
    os.makedirs(agent_dir, exist_ok=True)

    entry = f"[{local_clock_time()}] {phase}"
    log_path = os.path.join(agent_dir, "command-log.txt")
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(entry + "\n")

    summary_path = os.path.join(agent_dir, "stage-summary.json")
    summary = _read_workspace_json(summary_path)
    phases = summary.get("phases") if isinstance(summary.get("phases"), list) else []
    phases.append(phase)
    summary.update(extra or {})
    if task_id:
        summary["taskId"] = task_id
    summary["agentId"] = summary.get("agentId") or relative_dir.rstrip("/")
    summary["currentPhase"] = phase
    summary["phases"] = phases
    summary["updatedAt"] = local_iso_timestamp()
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)