"""HTTP client for Compass API calls from IM Gateway."""

from __future__ import annotations

import json
import os
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

COMPASS_URL = os.environ.get("COMPASS_URL", "http://compass:8080")
COMPASS_API_KEY = os.environ.get("COMPASS_API_KEY", "").strip()


def _headers() -> dict:
    h = {"Content-Type": "application/json; charset=utf-8"}
    if COMPASS_API_KEY:
        h["Authorization"] = f"Bearer {COMPASS_API_KEY}"
    return h


def send_message(message: dict) -> dict:
    """POST /message:send to Compass and return the response body."""
    payload = json.dumps({"message": message}, ensure_ascii=False).encode("utf-8")
    req = Request(
        f"{COMPASS_URL}/message:send",
        data=payload,
        headers=_headers(),
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def resume_task(context_id: str, message: dict) -> dict:
    """POST /message:send with contextId to resume an INPUT_REQUIRED task."""
    payload = json.dumps({
        "contextId": context_id,
        "message": message,
    }, ensure_ascii=False).encode("utf-8")
    req = Request(
        f"{COMPASS_URL}/message:send",
        data=payload,
        headers=_headers(),
        method="POST",
    )
    with urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_task(task_id: str) -> dict:
    """GET /tasks/{task_id} from Compass."""
    req = Request(
        f"{COMPASS_URL}/tasks/{task_id}",
        headers=_headers(),
        method="GET",
    )
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_tasks() -> list[dict]:
    """GET /api/tasks from Compass, return the tasks array."""
    req = Request(
        f"{COMPASS_URL}/api/tasks",
        headers=_headers(),
        method="GET",
    )
    with urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("tasks", [])


def register_notification_target(callback_url: str) -> dict:
    """POST /api/notification-targets to register Gateway's webhook URL."""
    payload = json.dumps({"url": callback_url}, ensure_ascii=False).encode("utf-8")
    req = Request(
        f"{COMPASS_URL}/api/notification-targets",
        data=payload,
        headers=_headers(),
        method="POST",
    )
    with urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))
