"""Compass UI route handlers."""
from __future__ import annotations
import json
from datetime import datetime, timezone
from agents.compass.ui.templates import render_compass_ui


def _now_iso() -> str:
    """Return current UTC time in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def _ui_status(task) -> str:
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
        text = message.text().strip()
        if text:
            return text
    return ""


def _serialize_ui_task(task) -> dict:
    return {
        "task_id": task.id,
        "status": _ui_status(task),
        "summary": _task_summary(task),
        "raw_status": getattr(getattr(task, "status", None), "state", None).value,
        "agent": (getattr(task, "metadata", {}) or {}).get("agentId", ""),
    }


def handle_ui_request(method: str, path: str, task_store=None, log_store_url=None) -> dict:
    """Handle UI-related HTTP requests."""
    if method == "GET" and path == "/ui":
        return serve_ui(task_store)
    if method == "GET" and path == "/tasks":
        return list_tasks(task_store)
    if method == "GET" and path.startswith("/tasks/"):
        task_id = path.split("/")[-1]
        return get_task_detail(task_id, task_store)
    if method == "GET" and path == "/poll":
        since = None  # TODO: parse from query params
        return poll_task_status(task_store, since)
    if method == "GET" and path.startswith("/logs/"):
        task_id = path.split("/")[-1]
        return proxy_to_log_store(task_id, log_store_url)

    return {"status": 404, "body": "Not found"}


def serve_ui(task_store=None) -> dict:
    """Serve the main UI page."""
    # Get current state
    messages = []  # TODO: Get from session
    tasks = []
    if task_store is not None:
        tasks = [_serialize_ui_task(task) for task in task_store.list_tasks()]

    html = render_compass_ui(messages, tasks)
    return {
        "status": 200,
        "headers": {"Content-Type": "text/html"},
        "body": html,
    }


def list_tasks(task_store) -> dict:
    """List all tasks."""
    if task_store is None:
        return {"status": 200, "headers": {"Content-Type": "application/json"}, "body": {"tasks": []}}

    tasks = task_store.list_tasks()
    return {
        "status": 200,
        "headers": {"Content-Type": "application/json"},
        "body": {
            "tasks": [_serialize_ui_task(t) for t in tasks]
        },
    }


def get_task_detail(task_id: str, task_store) -> dict:
    """Get detailed info for a task."""
    if task_store is None:
        return {"status": 404, "body": "Task store not available"}

    task = task_store.get_task(task_id)
    if task is None:
        return {"status": 404, "body": "Task not found"}

    return {
        "status": 200,
        "headers": {"Content-Type": "application/json"},
        "body": {
            "task_id": task.id,
            "status": task.status.state.value,
            "message": task.status.message.text() if task.status.message else "",
            "metadata": task.metadata,
            "artifacts": [
                {"name": a.name, "type": a.artifact_type, "parts": a.parts}
                for a in task.artifacts
            ],
        },
    }


def proxy_to_log_store(task_id: str, log_store_url: str) -> dict:
    """Proxy log requests to LogStore."""
    if not log_store_url:
        return {"status": 200, "headers": {"Content-Type": "application/json"}, "body": {"task_id": task_id, "logs": []}}

    import urllib.request
    try:
        resp = urllib.request.urlopen(f"{log_store_url}/logs/{task_id}")
        return {"status": 200, "headers": {"Content-Type": "application/json"}, "body": resp.read()}
    except Exception as e:
        return {"status": 200, "headers": {"Content-Type": "application/json"}, "body": {"task_id": task_id, "logs": [], "error": str(e)}}


def poll_task_status(task_store, since: str | None = None) -> dict:
    """Poll for task status updates.

    Returns all tasks with their current state and any new messages.
    """
    tasks = task_store.list_tasks() if task_store else []

    # Get messages since timestamp
    messages = []  # TODO: Implement message history

    return {
        "tasks": [
            _serialize_ui_task(t)
            for t in tasks
        ],
        "messages": messages,
        "timestamp": _now_iso(),
    }