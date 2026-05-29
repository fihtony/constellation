"""Live Compass UI contract tests against the running container stack.

These assertions focus on the finalized three-column workspace contract:
- the UI shell exposes the finalized task workspace structure
- creating a task yields a waiting task in newest-first order
- resuming reuses the same task id and completes the task
- logs expose filterable agent/level/task metadata for the right panel
"""
from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

import pytest


pytestmark = pytest.mark.live


PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
TESTS_DATA = PROJECT_ROOT / "tests" / "data"
COMPASS_BASE_URL = os.environ.get("TEST_COMPASS_BASE_URL", "http://localhost:8000").rstrip("/")
TOOLS_DATA_CSV = TESTS_DATA / "csv" / "sales_data.csv"


def _http_get_json(url: str, timeout: int = 30) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_text(url: str, timeout: int = 30) -> str:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def _http_post(url: str, payload: dict, timeout: int = 30) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _task_state(task_dict: dict) -> str:
    return task_dict.get("task", task_dict).get("status", {}).get("state", "")


def _task_id(task_dict: dict) -> str:
    return task_dict.get("task", task_dict).get("id", "")


def _poll_task(task_id: str, timeout_seconds: int = 900) -> dict:
    deadline = time.time() + timeout_seconds
    terminal = {
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELLED",
        "TASK_STATE_INPUT_REQUIRED",
    }
    while time.time() < deadline:
        task = _http_get_json(f"{COMPASS_BASE_URL}/tasks/{task_id}")
        if _task_state(task) in terminal:
            return task
        time.sleep(2)
    raise TimeoutError(f"Task {task_id} did not reach a terminal state within {timeout_seconds}s")


def _require_live_stack() -> None:
    try:
        with urllib.request.urlopen(f"{COMPASS_BASE_URL}/health", timeout=10) as resp:
            if resp.status != 200:
                pytest.skip(f"Compass is not healthy at {COMPASS_BASE_URL}")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Compass live stack is not reachable at {COMPASS_BASE_URL}: {exc}")


@pytest.fixture(scope="module", autouse=True)
def require_live_stack() -> None:
    _require_live_stack()


class TestCompassUILive:
    def test_ui_shell_exposes_finalized_workspace(self) -> None:
        body = _http_get_text(f"{COMPASS_BASE_URL}/ui")

        assert 'id="task-list-panel"' in body
        assert 'id="task-chat-panel"' in body
        assert 'id="task-info-panel"' in body
        assert "New Request" in body
        assert "Task List" in body
        assert "Task Chat" in body
        assert "Task Info" in body
        assert "filter-agent" in body
        assert "filter-level" in body

    def test_office_waiting_task_resumes_same_task_and_exposes_logs(self) -> None:
        initial_response = _http_post(
            f"{COMPASS_BASE_URL}/message:send",
            {
                "message": {
                    "messageId": "compass-ui-live-office-waiting",
                    "role": "ROLE_USER",
                    "parts": [
                        {
                            "text": "Please analyze the authorized spreadsheet in the shared folder and write the result to the task workspace."
                        }
                    ],
                    "metadata": {
                        "capability": "analyze",
                        "source_paths": [str(TOOLS_DATA_CSV)],
                    },
                },
                "configuration": {"returnImmediately": True},
            },
            timeout=60,
        )

        compass_task_id = _task_id(initial_response)
        assert compass_task_id
        assert _task_state(initial_response) == "TASK_STATE_INPUT_REQUIRED"

        tasks_payload = _http_get_json(f"{COMPASS_BASE_URL}/api/tasks")
        assert tasks_payload["tasks"], "expected newly created task to be listed"
        assert tasks_payload["tasks"][0]["task_id"] == compass_task_id
        assert tasks_payload["tasks"][0]["statusKind"] == "waiting"

        detail_waiting = _http_get_json(f"{COMPASS_BASE_URL}/api/tasks/{compass_task_id}")
        task_detail = detail_waiting.get("task", detail_waiting)
        assert task_detail["task_id"] == compass_task_id
        assert task_detail["statusKind"] == "waiting"
        assert len(task_detail["chatHistory"]) >= 2
        assert task_detail["chatHistory"][0]["role"] == "USER"
        assert task_detail["chatHistory"][1]["role"] == "COMPASS"
        assert task_detail["current_major_step"]
        assert task_detail["progress_steps"]

        resumed_response = _http_post(
            f"{COMPASS_BASE_URL}/tasks/{compass_task_id}/resume",
            {"input": "workspace"},
            timeout=900,
        )
        assert _task_id(resumed_response) == compass_task_id

        final_task = _poll_task(compass_task_id)
        assert _task_state(final_task) == "TASK_STATE_COMPLETED", final_task

        detail_completed = _http_get_json(f"{COMPASS_BASE_URL}/api/tasks/{compass_task_id}")
        completed_task = detail_completed.get("task", detail_completed)
        assert completed_task["task_id"] == compass_task_id
        assert completed_task["statusKind"] == "completed"
        assert completed_task["completed_at"]
        assert completed_task["elapsed_ms"] >= 0
        assert completed_task["chatHistory"][-2]["role"] == "USER"
        assert completed_task["chatHistory"][-1]["role"] == "COMPASS"

        logs_payload = _http_get_json(f"{COMPASS_BASE_URL}/logs/{compass_task_id}")
        assert logs_payload["logs"], "expected merged logs for the completed task"
        first_log = logs_payload["logs"][0]
        assert first_log["task_id"] == compass_task_id
        assert "sequence" in first_log
        assert "agent" in first_log
        assert "level" in first_log
        agents = {entry.get("agent", "") for entry in logs_payload["logs"]}
        assert "compass" in agents