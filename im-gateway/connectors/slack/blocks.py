"""Block Kit builder helpers for the Slack Connector.

Provides functions to construct Slack Block Kit payloads for common message types.
Separated from connector.py per the design in docs/compass-slack-integration-zh.md §3.1.
"""

from __future__ import annotations

MAX_BLOCK_TEXT_LEN = 3000
MAX_BLOCKS = 50


def truncate(text: str, limit: int) -> str:
    """Truncate text to *limit* chars, appending a note when shortened."""
    if len(text) <= limit:
        return text
    return text[: limit - 30] + "\n\n_...truncated. See Compass UI for full content._"


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


def task_created(task_id: str, summary: str) -> dict:
    """Render a 'Task Created' Block Kit message."""
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "\u2705 Task Created"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Task ID:*\n{task_id}"},
            {"type": "mrkdwn", "text": "*Status:*\nWORKING"},
        ]},
    ]
    if summary:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": truncate(summary, 200)}})
    blocks.append({"type": "context", "elements": [
        {"type": "mrkdwn", "text": f"Use `/compass task {task_id}` to check status."}
    ]})
    return {"blocks": blocks}


def task_list(tasks: list[dict]) -> dict:
    """Render a task list Block Kit message."""
    if not tasks:
        return {"blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text": "No running tasks. Send a message to create one."}},
        ]}

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Your Tasks"}},
    ]
    for t in tasks[:10]:
        tid = t.get("id") or t.get("task_id", "")
        state = t.get("state") or t.get("status", {}).get("state", "")
        summary = t.get("summary", "")[:50]
        emoji = state_emoji(state)
        text = f"{emoji} *{tid}* — {state}"
        if summary:
            text += f"\n{summary}"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": text}})
    if len(tasks) > 10:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": "Showing latest 10. View all in Compass UI."}
        ]})
    return {"blocks": blocks[:MAX_BLOCKS]}


def task_detail(task: dict) -> dict:
    """Render a task detail Block Kit message."""
    state = task.get("status", {}).get("state", "UNKNOWN")
    status_msg = ""
    msg_data = task.get("status", {}).get("message", {})
    if isinstance(msg_data, dict):
        parts = msg_data.get("parts", [])
        if parts and isinstance(parts[0], dict):
            status_msg = parts[0].get("text", "")
    task_id = task.get("id", "")
    emoji = state_emoji(state)
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": f"{emoji} Task {task_id}"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Status:*\n{state}"},
        ]},
    ]
    if status_msg:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": truncate(status_msg, MAX_BLOCK_TEXT_LEN)}})
    return {"blocks": blocks}


def input_required(question: str, task_id: str) -> dict:
    """Render an 'Input Required' Block Kit message."""
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "\u2753 Input Required"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Task:*\n{task_id}"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": truncate(question, MAX_BLOCK_TEXT_LEN)}},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"Reply in this thread or use `/compass resume {task_id} <your answer>`"}
        ]},
    ]
    return {"blocks": blocks}


def task_completed(task_id: str, summary: str, links: list[dict] | None = None) -> dict:
    """Render a 'Task Completed' Block Kit message."""
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "\u2705 Task Completed"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Task:*\n{task_id}"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": truncate(summary, MAX_BLOCK_TEXT_LEN)}},
    ]
    if links:
        link_texts = [f"<{l['url']}|{l.get('title', 'Link')}>" for l in links[:5]]
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": " | ".join(link_texts)}})
    return {"blocks": blocks}


def task_failed(task_id: str, error_summary: str) -> dict:
    """Render a 'Task Failed' Block Kit message."""
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "\u274c Task Failed"}},
        {"type": "section", "fields": [
            {"type": "mrkdwn", "text": f"*Task:*\n{task_id}"},
        ]},
        {"type": "section", "text": {"type": "mrkdwn", "text": truncate(error_summary, MAX_BLOCK_TEXT_LEN)}},
    ]
    return {"blocks": blocks}


def help_message() -> dict:
    """Render the help Block Kit message."""
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": "Compass Bot"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "I can help you create and track development and office tasks."}},
        {"type": "section", "text": {"type": "mrkdwn", "text": (
            "*Available commands:*\n"
            "\u2022 `/compass tasks` — List your running tasks\n"
            "\u2022 `/compass task <id>` — View task details\n"
            "\u2022 `/compass resume <id> <text>` — Reply to a task waiting for input\n"
            "\u2022 `/compass help` — Show this help\n"
            "\n_Or just send a message to create a new task._"
        )}},
    ]
    return {"blocks": blocks}


def error_message(message: str) -> dict:
    """Render an error Block Kit message."""
    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": f":warning: {truncate(message, MAX_BLOCK_TEXT_LEN)}"}},
    ]
    return {"blocks": blocks}
