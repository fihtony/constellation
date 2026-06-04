"""Compass UI HTTP route handlers.

Exposes:
  GET  /ui                        -> three-column workspace HTML
  GET  /api/tasks                 -> JSON list of tasks (newest-first), with
                                     ``createdAt``, ``updatedAt``,
                                     ``chatHistory``, ``userRequest``.
  GET  /api/tasks/{task_id}       -> JSON detail for a single task.
  GET  /tasks                     -> legacy list endpoint (kept for tests).
  GET  /poll                      -> polling fallback used when SSE is blocked.
  GET  /ui/events                 -> SSE stream of task lifecycle events.
  GET  /logs/{task_id}            -> proxies Log Store /logs/{task_id}.
  GET  /logs/stream/{task_id}     -> proxies Log Store SSE for a task.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Iterable

from agents.compass.ui.templates import render_compass_ui
from agents.log_store.log_aggregator import LogAggregator


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _office_task_report_path(task_id: str) -> str:
    artifact_root = os.environ.get("ARTIFACT_ROOT", "artifacts")
    return os.path.join(artifact_root, task_id, "office", "task-report.json")


def _load_office_task_report(task_id: str) -> dict:
    report_path = _office_task_report_path(task_id)
    if not os.path.exists(report_path):
        return {}
    try:
        with open(report_path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except Exception:
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def _gate_report_path(task_id: str) -> str:
    artifact_root = os.environ.get("ARTIFACT_ROOT", "artifacts")
    return os.path.join(artifact_root, task_id, "office", "artifacts", "plan-output-gate-report.json")


def _load_gate_report(task_id: str) -> dict:
    path = _gate_report_path(task_id)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def _organized_output_root(task_id: str) -> str:
    artifact_root = os.environ.get("ARTIFACT_ROOT", "artifacts")
    return os.path.join(artifact_root, task_id, "office", "artifacts", "organized-output", "files")


def _count_materialized_organized_files(task_id: str) -> int:
    root = _organized_output_root(task_id)
    if not os.path.isdir(root):
        return 0
    return sum(len(files) for _, _, files in os.walk(root))


def _count_source_files(root: str) -> int:
    """Recursively count non-hidden regular files under ``root``."""
    if not root or not os.path.isdir(root):
        return 0
    total = 0
    for current_root, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            total += 1
    return total


def _copy_row_with_summary_facts(row: dict, summary_facts: dict) -> dict:
    updated = dict(row)
    updated["summary_facts"] = summary_facts
    return updated


def _enrich_office_major_step_rows(task_id: str, metadata: dict, rows: dict) -> dict:
    if metadata.get("task_type") != "office" or not rows:
        return rows

    report_data = _load_office_task_report(task_id)
    if not report_data:
        return rows

    enriched = {key: dict(value) for key, value in rows.items()}
    office_request = metadata.get("office_request") or {}
    capability = str(
        office_request.get("capability")
        or report_data.get("capability")
        or ""
    ).strip().lower()

    if capability == "summarize":
        source_paths = report_data.get("source_paths") or []
        source_count = len(source_paths) if isinstance(source_paths, list) else 0
        original_inputs = office_request.get("source_paths") or []
        is_folder_input = (
            isinstance(original_inputs, list)
            and len(original_inputs) == 1
            and (
                os.path.isdir(str(original_inputs[0]))
                or source_count > 1
            )
        )
        received_key = "office.received#0"
        if received_key in enriched:
            facts = dict(enriched[received_key].get("summary_facts") or {})
            facts["capability"] = "summarize"
            facts["source_count"] = 1 if is_folder_input else len(original_inputs) or facts.get("source_count", 0)
            facts["source_kind"] = "folder" if is_folder_input else ("file" if facts.get("source_count") == 1 else "files")
            enriched[received_key] = _copy_row_with_summary_facts(enriched[received_key], facts)
        if source_count > 0:
            reading_key = "office.reading#0"
            if reading_key in enriched:
                facts = dict(enriched[reading_key].get("summary_facts") or {})
                facts["source_count"] = source_count
                facts["source_kind"] = "file" if source_count == 1 else "files"
                enriched[reading_key] = _copy_row_with_summary_facts(enriched[reading_key], facts)
            summarizing_key = "office.summarizing#0"
            if summarizing_key in enriched:
                facts = dict(enriched[summarizing_key].get("summary_facts") or {})
                facts["source_count"] = source_count
                enriched[summarizing_key] = _copy_row_with_summary_facts(enriched[summarizing_key], facts)
        return enriched

    if capability == "organize":
        file_count = _count_materialized_organized_files(task_id)
        if file_count <= 0:
            return enriched
        scanning_key = "office.scanning#0"
        if scanning_key in enriched:
            facts = dict(enriched[scanning_key].get("summary_facts") or {})
            facts["file_count"] = file_count
            enriched[scanning_key] = _copy_row_with_summary_facts(enriched[scanning_key], facts)
        moving_key = "office.moving_files#0"
        if moving_key in enriched:
            facts = dict(enriched[moving_key].get("summary_facts") or {})
            facts["file_count"] = file_count
            enriched[moving_key] = _copy_row_with_summary_facts(enriched[moving_key], facts)
        writing_plan_key = "office.writing_plan#0"
        if writing_plan_key in enriched:
            row = dict(enriched[writing_plan_key])
            row["lifecycle_state"] = "done"
            row["visual_state"] = "done"
            if row.get("ended_at") is None:
                row["ended_at"] = row.get("started_at") or ""
            enriched[writing_plan_key] = row

        # NEW: Backfill discovered_source_count for folder-backed organize input
        original_inputs = office_request.get("source_paths") or []
        if (
            isinstance(original_inputs, list)
            and len(original_inputs) == 1
            and os.path.isdir(str(original_inputs[0]))
        ):
            received_key = "office.received#0"
            if received_key in enriched:
                folder_count = _count_source_files(str(original_inputs[0]))
                if folder_count > 0:
                    facts = dict(enriched[received_key].get("summary_facts") or {})
                    facts["discovered_source_count"] = folder_count
                    enriched[received_key] = _copy_row_with_summary_facts(
                        enriched[received_key], facts
                    )

        # NEW: Surface the gate reason into office.validating_plan_output#0
        gate_report = _load_gate_report(task_id)
        if gate_report:
            validating_key = "office.validating_plan_output#0"
            if validating_key in enriched:
                row = dict(enriched[validating_key])
                facts = dict(row.get("summary_facts") or {})
                plan_status = str(gate_report.get("plan_status") or "").strip().lower()
                if plan_status:
                    facts["plan_status"] = plan_status
                invalid_entries = gate_report.get("invalid_plan_entries") or []
                if isinstance(invalid_entries, list) and invalid_entries:
                    facts["invalid_plan_entry_count"] = len(invalid_entries)
                # Append a "plan is {plan_status}" reason sentence to the template
                # so the UI can render the gate reason inline.
                existing_template = str(row.get("summary_template") or "").rstrip()
                if plan_status and "plan is {plan_status}" not in existing_template:
                    row["summary_template"] = (
                        existing_template + f" plan is {{plan_status}}."
                    )
                row["summary_facts"] = facts
                enriched[validating_key] = row

        return enriched

    return enriched


def _ui_status_kind(task) -> str:
    raw = getattr(getattr(task, "status", None), "state", "")
    value = getattr(raw, "value", raw)
    mapping = {
        "TASK_STATE_COMPLETED": "completed",
        "TASK_STATE_FAILED": "failed",
        "TASK_STATE_INPUT_REQUIRED": "waiting",
        "TASK_STATE_WORKING": "active",
        "TASK_STATE_SUBMITTED": "active",
    }
    return mapping.get(str(value), "active")


def _task_summary(task) -> str:
    metadata = getattr(task, "metadata", {}) or {}
    if metadata.get("summary"):
        return str(metadata["summary"])
    message = getattr(getattr(task, "status", None), "message", None)
    if message:
        try:
            text = message.text().strip()
        except Exception:
            text = ""
        if text:
            return text
    return ""


def _task_user_request(task) -> str:
    metadata = getattr(task, "metadata", {}) or {}
    if metadata.get("userRequest"):
        return str(metadata["userRequest"])
    history = metadata.get("chat_history") or []
    for entry in history:
        if str(entry.get("role", "")).lower() in {"user", "role_user"}:
            return str(entry.get("text", ""))
    return ""


def _serialize_ui_task(task) -> dict:
    metadata = getattr(task, "metadata", {}) or {}
    status_state = getattr(getattr(task, "status", None), "state", None)
    status_value = getattr(status_state, "value", str(status_state)) if status_state else ""
    created_at = getattr(task, "created_at", "") or ""
    updated_at = getattr(task, "updated_at", "") or ""
    completed_at = updated_at if status_value in {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED"} else ""
    elapsed_ms = 0
    if created_at and updated_at:
        try:
            elapsed_ms = max(
                0,
                int(
                    (
                        datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                        - datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                    ).total_seconds()
                    * 1000
                ),
            )
        except ValueError:
            elapsed_ms = 0
    status_kind = _ui_status_kind(task)

    # Major-step timeline fields (v0.8 redesign). The new structured rows are
    # the canonical data; the legacy ``currentMajorStep`` / ``progressSteps``
    # fields are kept as derived views for backward compatibility.
    major_step_rows = _enrich_office_major_step_rows(
        task.id,
        metadata,
        metadata.get("major_step_rows") or {},
    )
    major_step_skeleton = metadata.get("major_step_skeleton") or []
    active_key = metadata.get("active_step_instance_key", "")
    failed_key = metadata.get("failed_step_instance_key", "")
    terminal_key = metadata.get("terminal_step_instance_key", "")
    last_key = metadata.get("last_step_instance_key", "")

    # Derive ``currentMajorStep`` from the active row's title (per design doc
    # §13 B8). Falls back to the stored legacy value if the active row is
    # missing (e.g. a pre-v0.8 task that never went through the new API).
    legacy_current = metadata.get("current_major_step", "")
    if active_key and active_key in major_step_rows:
        current_major_step_title = str(major_step_rows[active_key].get("title", ""))
    elif terminal_key and terminal_key in major_step_rows:
        current_major_step_title = str(major_step_rows[terminal_key].get("title", ""))
    elif failed_key and failed_key in major_step_rows:
        current_major_step_title = str(major_step_rows[failed_key].get("title", ""))
    else:
        current_major_step_title = legacy_current

    return {
        "task_id": task.id,
        "id": task.id,
        "orchestratorTaskId": metadata.get("orchestratorTaskId", task.id),
        "status": status_kind,
        "statusKind": status_kind,
        "statusState": status_value,
        "summary": _task_summary(task),
        "user_request": _task_user_request(task),
        "userRequest": _task_user_request(task),
        "createdAt": created_at,
        "updatedAt": updated_at,
        "created_at": created_at,
        "started_at": created_at,
        "updated_at": updated_at,
        "completed_at": completed_at,
        "elapsed_ms": elapsed_ms,
        "agent": metadata.get("agentId", ""),
        "taskType": metadata.get("task_type", metadata.get("taskType", "general")),
        "task_type": metadata.get("task_type", metadata.get("taskType", "general")),
        "chatHistory": list(metadata.get("chat_history") or []),
        # Legacy fields (kept for clients still reading them).
        "currentMajorStep": current_major_step_title,
        "current_major_step": current_major_step_title,
        "progressSteps": list(metadata.get("progress_steps") or []),
        "progress_steps": list(metadata.get("progress_steps") or []),
        # New structured fields (v0.8 timeline redesign).
        "majorStepRows": major_step_rows,
        "majorStepEvents": list(metadata.get("major_step_events") or []),
        "majorSteps": major_step_rows,  # camelCase alias
        "majorStepsSkeleton": major_step_skeleton,
        "stepStates": dict(metadata.get("step_states") or {}),
        "stepSummaries": dict(metadata.get("step_summaries") or {}),
        "activeStepInstanceKey": active_key,
        "lastStepInstanceKey": last_key,
        "failedStepInstanceKey": failed_key,
        "terminalStepInstanceKey": terminal_key,
    }


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def handle_ui_request(
    method: str,
    path: str,
    task_store=None,
    log_store_url: str | None = None,
) -> dict:
    if method != "GET":
        return {"status": 405, "body": "Method not allowed"}

    if path == "/ui" or path == "":
        return serve_ui(task_store)

    if path == "/api/tasks":
        return list_tasks_json(task_store)
    if path.startswith("/api/tasks/"):
        task_id = path[len("/api/tasks/"):]
        return get_task_detail(task_id, task_store)

    if path == "/ui/events":
        return sse_task_events(task_store)

    # Legacy
    if path == "/tasks":
        return list_tasks_json(task_store)
    if path.startswith("/tasks/"):
        task_id = path[len("/tasks/"):]
        return get_task_detail(task_id, task_store)

    if path == "/poll":
        return poll_task_status(task_store)

    if path.startswith("/logs/stream/"):
        task_id = path[len("/logs/stream/"):]
        return proxy_log_stream(task_id, log_store_url)
    if path.startswith("/logs/"):
        task_id = path[len("/logs/"):]
        return proxy_to_log_store(task_id, log_store_url)

    return {"status": 404, "body": "Not found"}


# ---------------------------------------------------------------------------
# UI page
# ---------------------------------------------------------------------------

def serve_ui(task_store=None) -> dict:
    tasks = []
    if task_store is not None:
        tasks = [_serialize_ui_task(t) for t in task_store.list_tasks()]
    html = render_compass_ui(messages=[], tasks=tasks)
    return {
        "status": 200,
        "headers": {"Content-Type": "text/html; charset=utf-8"},
        "body": html,
    }


# ---------------------------------------------------------------------------
# JSON endpoints
# ---------------------------------------------------------------------------

def list_tasks_json(task_store) -> dict:
    if task_store is None:
        return {
            "status": 200,
            "headers": {"Content-Type": "application/json"},
            "body": {"tasks": []},
        }
    tasks = task_store.list_tasks()
    return {
        "status": 200,
        "headers": {"Content-Type": "application/json"},
        "body": {"tasks": [_serialize_ui_task(t) for t in tasks]},
    }


# Backwards compatibility for tests that still import the old name.
list_tasks = list_tasks_json


def get_task_detail(task_id: str, task_store) -> dict:
    if task_store is None:
        return {"status": 404, "body": "Task store not available"}
    task = task_store.get_task(task_id)
    if task is None:
        return {"status": 404, "body": "Task not found"}
    payload = _serialize_ui_task(task)
    payload["artifacts"] = [
        {
            "name": a.name,
            "type": a.artifact_type,
            "parts": a.parts,
            "metadata": getattr(a, "metadata", {}) or {},
        }
        for a in getattr(task, "artifacts", [])
    ]
    payload["statusMessage"] = (
        task.status.message.text() if getattr(task.status, "message", None) else ""
    )
    payload["metadata"] = getattr(task, "metadata", {}) or {}
    return {
        "status": 200,
        "headers": {"Content-Type": "application/json"},
        "body": {"task": payload, **payload},
    }


def poll_task_status(task_store, since: str | None = None) -> dict:
    tasks = task_store.list_tasks() if task_store else []
    return {
        "status": 200,
        "headers": {"Content-Type": "application/json"},
        "body": {
            "tasks": [_serialize_ui_task(t) for t in tasks],
            "messages": [],
            "timestamp": _now_iso(),
        },
    }


# ---------------------------------------------------------------------------
# SSE: /ui/events
# ---------------------------------------------------------------------------

def _snapshot_signature(task) -> tuple:
    """Return a tuple that changes whenever a task is updated."""
    return (
        task.id,
        str(getattr(getattr(task, "status", None), "state", "")),
        getattr(task, "updated_at", "") or "",
        len((getattr(task, "metadata", {}) or {}).get("chat_history") or []),
    )


def _sse_format(event: str, data: dict) -> str:
    payload = json.dumps(data, ensure_ascii=False)
    return f"event: {event}\ndata: {payload}\n\n"


def _artifact_root() -> str:
    return os.environ.get("ARTIFACT_ROOT", "artifacts/")


def _aggregate_local_logs(task_id: str) -> list[dict]:
    aggregator = LogAggregator(_artifact_root())
    logs = aggregator.aggregate_task(task_id)
    enriched: list[dict] = []
    for index, entry in enumerate(logs, start=1):
        enriched.append({**entry, "task_id": task_id, "sequence": index})
    return enriched


def _ui_events_generator(task_store, *, poll_interval: float = 1.0,
                          max_iterations: int | None = None) -> Iterable[str]:
    """Yield SSE chunks reflecting task lifecycle changes.

    The generator polls the task store at ``poll_interval`` seconds and emits
    events whenever a task is created or its signature changes. ``max_iterations``
    caps the loop (used by unit tests).
    """
    last_signatures: dict[str, tuple] = {}

    # Initial snapshot
    if task_store is not None:
        snapshot = [_serialize_ui_task(t) for t in task_store.list_tasks()]
        for t in task_store.list_tasks():
            last_signatures[t.id] = _snapshot_signature(t)
    else:
        snapshot = []
    yield _sse_format("task.snapshot", {"tasks": snapshot, "ts": _now_iso()})
    # Heartbeat comment so clients flush quickly
    yield ": connected\n\n"

    iterations = 0
    while True:
        if max_iterations is not None and iterations >= max_iterations:
            return
        iterations += 1
        time.sleep(poll_interval)
        if task_store is None:
            yield ": heartbeat\n\n"
            continue
        try:
            tasks = list(task_store.list_tasks())
        except Exception:
            yield ": heartbeat\n\n"
            continue
        seen = set()
        for task in tasks:
            seen.add(task.id)
            sig = _snapshot_signature(task)
            prev = last_signatures.get(task.id)
            if prev is None:
                last_signatures[task.id] = sig
                yield _sse_format("task.created", _serialize_ui_task(task))
                continue
            if prev != sig:
                last_signatures[task.id] = sig
                kind = _ui_status_kind(task)
                previous_state = str(prev[1])
                current_state = str(getattr(getattr(task, "status", None), "state", ""))
                if previous_state == "TASK_STATE_INPUT_REQUIRED" and current_state == "TASK_STATE_WORKING":
                    event_name = "task.resumed"
                else:
                    event_name = {
                        "completed": "task.completed",
                        "failed": "task.failed",
                        "waiting": "task.input_required",
                    }.get(kind, "task.updated")
                yield _sse_format(event_name, _serialize_ui_task(task))
        # No deletion events for now.
        # Heartbeat keeps the connection alive through proxies.
        yield ": heartbeat\n\n"


def sse_task_events(task_store, *, poll_interval: float = 1.0,
                    max_iterations: int | None = None) -> dict:
    return {
        "status": 200,
        "headers": {"Content-Type": "text/event-stream; charset=utf-8"},
        "body": _ui_events_generator(task_store, poll_interval=poll_interval,
                                     max_iterations=max_iterations),
    }


# ---------------------------------------------------------------------------
# Log store proxies
# ---------------------------------------------------------------------------

def proxy_to_log_store(task_id: str, log_store_url: str | None) -> dict:
    if not log_store_url:
        return {
            "status": 200,
            "headers": {"Content-Type": "application/json"},
            "body": {"task_id": task_id, "logs": _aggregate_local_logs(task_id)},
        }
    import urllib.request
    try:
        with urllib.request.urlopen(f"{log_store_url}/logs/{task_id}", timeout=5) as resp:
            return {
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "body": resp.read(),
            }
    except Exception as exc:
        return {
            "status": 200,
            "headers": {"Content-Type": "application/json"},
            "body": {"task_id": task_id, "logs": [], "error": str(exc)},
        }


def proxy_log_stream(task_id: str, log_store_url: str | None) -> dict:
    """Proxy SSE stream of logs from the log store.

    Returns a generator that streams chunks from the upstream connection so the
    client receives ``log.appended`` events in near-real time.
    """
    if not log_store_url:
        def _local_stream():
            previous = _aggregate_local_logs(task_id)
            for entry in previous:
                yield _sse_format("log.appended", entry)
            yield ": local-log-fallback\n\n"
            while True:
                time.sleep(1)
                current = _aggregate_local_logs(task_id)
                if len(current) > len(previous):
                    for entry in current[len(previous):]:
                        yield _sse_format("log.appended", entry)
                previous = current
                yield ": heartbeat\n\n"
        return {
            "status": 200,
            "headers": {"Content-Type": "text/event-stream; charset=utf-8"},
            "body": _local_stream(),
        }

    import urllib.request

    def _stream():
        url = f"{log_store_url}/logs/stream/{task_id}"
        try:
            req = urllib.request.Request(url, headers={"Accept": "text/event-stream"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                while True:
                    chunk = resp.read(1024)
                    if not chunk:
                        break
                    yield chunk
        except Exception as exc:
            yield f"event: log.error\ndata: {json.dumps({'error': str(exc)})}\n\n".encode("utf-8")

    return {
        "status": 200,
        "headers": {"Content-Type": "text/event-stream; charset=utf-8"},
        "body": _stream(),
    }
