"""Adaptive Card builders for Teams Gateway bot messages."""

from __future__ import annotations


def _card_envelope(body: list, schema: str = "http://adaptivecards.io/schemas/adaptive-card.json") -> dict:
    """Wrap body elements in an Adaptive Card attachment."""
    card = {
        "type": "AdaptiveCard",
        "$schema": schema,
        "version": "1.3",
        "body": body,
    }
    return {
        "contentType": "application/vnd.microsoft.card.adaptive",
        "content": card,
    }


def welcome_card(compass_ui_url: str = "") -> dict:
    body = [
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
    if compass_ui_url:
        body.append({"type": "TextBlock", "text": f"[Open Compass UI]({compass_ui_url})", "wrap": True})
    return _card_envelope(body)


def help_card(compass_ui_url: str = "") -> dict:
    return welcome_card(compass_ui_url)


def task_created_card(task_id: str, summary: str = "") -> dict:
    body = [
        {"type": "TextBlock", "text": "Task Created", "weight": "bolder", "size": "medium", "color": "accent"},
        {"type": "FactSet", "facts": [
            {"title": "Task ID", "value": task_id},
            {"title": "Status", "value": "WORKING"},
        ]},
    ]
    if summary:
        body.append({"type": "TextBlock", "text": summary[:200], "wrap": True})
    body.append({"type": "TextBlock", "text": f"Use `/task {task_id}` to check status.", "wrap": True, "isSubtle": True})
    return _card_envelope(body)


def task_detail_card(task: dict) -> dict:
    state = task.get("status", {}).get("state", "UNKNOWN")
    status_msg = ""
    status_data = task.get("status", {})
    msg_data = status_data.get("message", {})
    if isinstance(msg_data, dict):
        parts = msg_data.get("parts", [])
        if parts and isinstance(parts[0], dict):
            status_msg = parts[0].get("text", "")
    task_id = task.get("id", "")
    color = _state_color(state)
    body = [
        {"type": "TextBlock", "text": f"{_state_emoji(state)} Task {task_id}", "weight": "bolder", "size": "medium"},
        {"type": "FactSet", "facts": [
            {"title": "Status", "value": state},
        ]},
    ]
    if status_msg:
        body.append({"type": "TextBlock", "text": status_msg[:2000], "wrap": True})
    return _card_envelope(body)


def task_list_card(tasks: list[dict]) -> dict:
    if not tasks:
        body = [
            {"type": "TextBlock", "text": "No running tasks.", "wrap": True},
            {"type": "TextBlock", "text": "Send a message to create a new task.", "wrap": True, "isSubtle": True},
        ]
        return _card_envelope(body)

    body = [
        {"type": "TextBlock", "text": "Your Tasks", "weight": "bolder", "size": "medium"},
    ]
    shown = tasks[:10]
    facts = []
    for t in shown:
        tid = t.get("id") or t.get("task_id", "")
        state = t.get("state") or (t.get("status", {}).get("state", ""))
        summary = t.get("summary", "")[:50]
        emoji = _state_emoji(state)
        facts.append({"title": f"{emoji} {tid}", "value": f"{state} — {summary}" if summary else state})
    body.append({"type": "FactSet", "facts": facts})
    if len(tasks) > 10:
        body.append({"type": "TextBlock", "text": "Showing latest 10. View all in Compass UI.", "wrap": True, "isSubtle": True})
    return _card_envelope(body)


def input_required_card(task_id: str, question: str) -> dict:
    body = [
        {"type": "TextBlock", "text": "Input Required", "weight": "bolder", "size": "medium", "color": "attention"},
        {"type": "FactSet", "facts": [{"title": "Task", "value": task_id}]},
        {"type": "TextBlock", "text": question[:2000], "wrap": True},
        {"type": "TextBlock", "text": f"Reply directly or use `/resume {task_id} <your answer>`", "wrap": True, "isSubtle": True},
    ]
    return _card_envelope(body)


def completed_card(task_id: str, summary: str, links: list[dict] | None = None) -> dict:
    body = [
        {"type": "TextBlock", "text": "Task Completed", "weight": "bolder", "size": "medium", "color": "good"},
        {"type": "FactSet", "facts": [{"title": "Task", "value": task_id}]},
        {"type": "TextBlock", "text": summary[:2000], "wrap": True},
    ]
    if links:
        for link in links[:5]:
            body.append({"type": "TextBlock", "text": f"[{link.get('title', 'Link')}]({link['url']})", "wrap": True})
    return _card_envelope(body)


def failed_card(task_id: str, error_summary: str) -> dict:
    body = [
        {"type": "TextBlock", "text": "Task Failed", "weight": "bolder", "size": "medium", "color": "attention"},
        {"type": "FactSet", "facts": [{"title": "Task", "value": task_id}]},
        {"type": "TextBlock", "text": error_summary[:2000], "wrap": True},
    ]
    return _card_envelope(body)


def error_card(message: str) -> dict:
    body = [
        {"type": "TextBlock", "text": "Error", "weight": "bolder", "color": "attention"},
        {"type": "TextBlock", "text": message[:2000], "wrap": True},
    ]
    return _card_envelope(body)


def _state_emoji(state: str) -> str:
    mapping = {
        "TASK_STATE_COMPLETED": "\u2705",
        "COMPLETED": "\u2705",
        "TASK_STATE_FAILED": "\u274c",
        "FAILED": "\u274c",
        "TASK_STATE_INPUT_REQUIRED": "\u2753",
        "TASK_STATE_WORKING": "\ud83d\udd04",
        "WORKING": "\ud83d\udd04",
        "ROUTING": "\ud83d\udd04",
        "SUBMITTED": "\ud83d\udd04",
    }
    return mapping.get(state, "\ud83d\udd04")


def _state_color(state: str) -> str:
    if state in ("TASK_STATE_COMPLETED", "COMPLETED"):
        return "good"
    if state in ("TASK_STATE_FAILED", "FAILED", "TASK_STATE_INPUT_REQUIRED"):
        return "attention"
    return "accent"
