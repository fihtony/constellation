"""Adaptive Card builder helpers for the Teams Connector.

Provides functions to construct Microsoft Adaptive Card payloads for common message types.
Separated from connector.py per the design in docs/compass-slack-integration-zh.md §3.1.
"""

from __future__ import annotations


def state_emoji(state: str) -> str:
    """Return a status emoji for the given task state."""
    mapping = {
        "TASK_STATE_COMPLETED": "\u2705",
        "COMPLETED": "\u2705",
        "TASK_STATE_FAILED": "\u274c",
        "FAILED": "\u274c",
        "TASK_STATE_INPUT_REQUIRED": "\u2753",
        "TASK_STATE_WORKING": "\U0001f504",
        "WORKING": "\U0001f504",
        "ROUTING": "\U0001f504",
        "SUBMITTED": "\U0001f504",
    }
    return mapping.get(state, "\U0001f504")


def card_envelope(body: list) -> dict:
    """Wrap an Adaptive Card body list in the standard envelope."""
    card = {
        "type": "AdaptiveCard",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "version": "1.3",
        "body": body,
    }
    return {
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": card,
    }


def task_created(task_id: str, summary: str) -> dict:
    """Render a 'Task Created' Adaptive Card."""
    body: list[dict] = [
        {"type": "TextBlock", "text": "Task Created", "weight": "bolder", "size": "medium", "color": "accent"},
        {"type": "FactSet", "facts": [
            {"title": "Task ID", "value": task_id},
            {"title": "Status", "value": "WORKING"},
        ]},
    ]
    if summary:
        body.append({"type": "TextBlock", "text": summary[:200], "wrap": True})
    body.append({"type": "TextBlock", "text": f"Use `/task {task_id}` to check status.", "wrap": True, "isSubtle": True})
    return card_envelope(body)


def task_list(tasks: list[dict]) -> dict:
    """Render a task list Adaptive Card."""
    if not tasks:
        body: list[dict] = [
            {"type": "TextBlock", "text": "No running tasks.", "wrap": True},
            {"type": "TextBlock", "text": "Send a message to create a new task.", "wrap": True, "isSubtle": True},
        ]
        return card_envelope(body)

    body = [{"type": "TextBlock", "text": "Your Tasks", "weight": "bolder", "size": "medium"}]
    shown = tasks[:10]
    facts = []
    for t in shown:
        tid = t.get("id") or t.get("task_id", "")
        state = t.get("state") or (t.get("status", {}).get("state", ""))
        summary = t.get("summary", "")[:50]
        emoji = state_emoji(state)
        facts.append({"title": f"{emoji} {tid}", "value": f"{state} — {summary}" if summary else state})
    body.append({"type": "FactSet", "facts": facts})
    if len(tasks) > 10:
        body.append({"type": "TextBlock", "text": "Showing latest 10. View all in Compass UI.", "wrap": True, "isSubtle": True})
    return card_envelope(body)


def task_detail(task: dict) -> dict:
    """Render a task detail Adaptive Card."""
    state = task.get("status", {}).get("state", "UNKNOWN")
    status_msg = ""
    msg_data = task.get("status", {}).get("message", {})
    if isinstance(msg_data, dict):
        parts = msg_data.get("parts", [])
        if parts and isinstance(parts[0], dict):
            status_msg = parts[0].get("text", "")
    task_id = task.get("id", "")
    body: list[dict] = [
        {"type": "TextBlock", "text": f"{state_emoji(state)} Task {task_id}", "weight": "bolder", "size": "medium"},
        {"type": "FactSet", "facts": [{"title": "Status", "value": state}]},
    ]
    if status_msg:
        body.append({"type": "TextBlock", "text": status_msg[:2000], "wrap": True})
    return card_envelope(body)


def input_required(question: str, task_id: str) -> dict:
    """Render an 'Input Required' Adaptive Card."""
    body: list[dict] = [
        {"type": "TextBlock", "text": "Input Required", "weight": "bolder", "size": "medium", "color": "attention"},
        {"type": "FactSet", "facts": [{"title": "Task", "value": task_id}]},
        {"type": "TextBlock", "text": question[:2000], "wrap": True},
        {"type": "TextBlock", "text": f"Reply directly or use `/resume {task_id} <your answer>`", "wrap": True, "isSubtle": True},
    ]
    return card_envelope(body)


def task_completed(task_id: str, summary: str, links: list[dict] | None = None) -> dict:
    """Render a 'Task Completed' Adaptive Card."""
    body: list[dict] = [
        {"type": "TextBlock", "text": "Task Completed", "weight": "bolder", "size": "medium", "color": "good"},
        {"type": "FactSet", "facts": [{"title": "Task", "value": task_id}]},
        {"type": "TextBlock", "text": summary[:2000], "wrap": True},
    ]
    if links:
        for link in links[:5]:
            body.append({"type": "TextBlock", "text": f"[{link.get('title', 'Link')}]({link['url']})", "wrap": True})
    return card_envelope(body)


def task_failed(task_id: str, error_summary: str) -> dict:
    """Render a 'Task Failed' Adaptive Card."""
    body: list[dict] = [
        {"type": "TextBlock", "text": "Task Failed", "weight": "bolder", "size": "medium", "color": "attention"},
        {"type": "FactSet", "facts": [{"title": "Task", "value": task_id}]},
        {"type": "TextBlock", "text": error_summary[:2000], "wrap": True},
    ]
    return card_envelope(body)


def help_message() -> dict:
    """Render the help Adaptive Card."""
    body: list[dict] = [
        {"type": "TextBlock", "text": "Compass Bot", "weight": "bolder", "size": "large"},
        {"type": "TextBlock", "text": "Welcome! I can help you create and track development and office tasks.", "wrap": True},
        {"type": "TextBlock", "text": "**Available commands:**", "wrap": True},
        {"type": "FactSet", "facts": [
            {"title": "/tasks", "value": "List your running tasks"},
            {"title": "/task <id>", "value": "View task details"},
            {"title": "/resume <id> <text>", "value": "Reply to a task waiting for input"},
            {"title": "/help", "value": "Show this help message"},
        ]},
        {"type": "TextBlock", "text": "Or just send a message to create a new task.", "wrap": True, "isSubtle": True},
    ]
    return card_envelope(body)


def error_message(message: str) -> dict:
    """Render an error Adaptive Card."""
    body: list[dict] = [
        {"type": "TextBlock", "text": "Error", "weight": "bolder", "color": "attention"},
        {"type": "TextBlock", "text": message[:2000], "wrap": True},
    ]
    return card_envelope(body)
