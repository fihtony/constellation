"""Compass UI route handlers."""
from __future__ import annotations
import json
from agents.compass.ui.templates import render_compass_ui


def handle_ui_request(method: str, path: str, task_store=None) -> dict:
    """Handle UI-related HTTP requests."""
    if method == "GET" and path == "/ui":
        return serve_ui()
    if method == "GET" and path == "/tasks":
        return list_tasks(task_store)
    if method == "GET" and path.startswith("/tasks/"):
        task_id = path.split("/")[-1]
        return get_task_detail(task_id, task_store)
    return {"status": 404, "body": "Not found"}


def serve_ui() -> dict:
    """Serve the main UI page."""
    # Get current state
    messages = []  # TODO: Get from session
    tasks = []     # TODO: Get from task_store

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
            "tasks": [
                {
                    "task_id": t.id,
                    "status": t.status.state.value,
                    "summary": t.metadata.get("summary", ""),
                }
                for t in tasks
            ]
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