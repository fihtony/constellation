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