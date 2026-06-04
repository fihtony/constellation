"""Live development E2E tests against the real containerized Compass stack.

This suite sends one generic development request to the running Compass HTTP
service, lets the real containerized agents drive the workflow autonomously,
and validates UI state, merged logs, and workspace artifacts under ``artifacts/``.

The task text comes only from the ``--task`` CLI argument so the test never
hardcodes any Jira ticket, repo URL, design URL, or other task-specific clue.
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from pathlib import Path

import pytest


pytestmark = pytest.mark.live


PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
ARTIFACTS_ROOT = PROJECT_ROOT / "artifacts"
COMPASS_BASE_URL = os.environ.get("TEST_COMPASS_BASE_URL", "http://localhost:8000").rstrip("/")

SENSITIVE_PATTERNS = [
    re.compile(r"Authorization:\s*(?:Bearer|Basic)\s+\S+", re.IGNORECASE),
    re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{20,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bATATT[A-Za-z0-9_\-]{10,}\b"),
    re.compile(r"\bglpat-[A-Za-z0-9\-]{10,}\b"),
]


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


def _extract_jira_key(task_text: str) -> str:
    match = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", task_text)
    return match.group(1) if match else ""


def _poll_task(task_id: str, timeout_seconds: int = 5400) -> dict:
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
        time.sleep(5)
    raise TimeoutError(f"Task {task_id} did not complete within {timeout_seconds}s")


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _check_json_artifact(workspace_path: Path, rel_path: str, required_keys: list[str] | None = None) -> dict:
    full_path = workspace_path / rel_path
    assert full_path.is_file(), f"missing artifact: {full_path}"
    payload = _read_json(full_path)
    data = payload.get("data", {}) if isinstance(payload, dict) else {}
    for key in required_keys or []:
        assert data.get(key), f"artifact {rel_path} missing data.{key}: {json.dumps(data, ensure_ascii=False)[:400]}"
    return payload


def _assert_no_sensitive_tokens(text: str, *, label: str) -> None:
    for pattern in SENSITIVE_PATTERNS:
        assert not pattern.search(text), f"{label} leaked sensitive token pattern: {pattern.pattern}"


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


def test_development_task_live_container(request) -> None:
    task_text = request.config.getoption("--task", default="")
    if not task_text:
        pytest.skip(
            "No --task argument provided. "
            "Usage: pytest tests/e2e/test_development_container_e2e.py -m live -v -s "
            "--task \"implement jira ticket: https://jira.example.com/browse/PROJ-123\""
        )

    jira_key = _extract_jira_key(task_text)

    ui_html = _http_get_text(f"{COMPASS_BASE_URL}/ui")
    assert 'id="task-list-panel"' in ui_html
    assert 'id="task-chat-panel"' in ui_html
    assert 'id="task-info-panel"' in ui_html
    assert "Task Info" in ui_html
    assert "Task Logs" in ui_html

    initial_response = _http_post(
        f"{COMPASS_BASE_URL}/message:send",
        {
            "message": {
                "messageId": f"development-container-e2e-{int(time.time())}",
                "role": "ROLE_USER",
                "parts": [{"text": task_text}],
                "metadata": {},
            },
            "configuration": {"returnImmediately": True},
        },
        timeout=60,
    )

    compass_task_id = _task_id(initial_response)
    assert compass_task_id, "expected Compass task id"
    assert _task_state(initial_response) in {
        "TASK_STATE_WORKING",
        "TASK_STATE_SUBMITTED",
        "TASK_STATE_ROUTING",
    }, initial_response

    tasks_payload = _http_get_json(f"{COMPASS_BASE_URL}/api/tasks")
    assert tasks_payload.get("tasks"), "expected task list payload"
    assert tasks_payload["tasks"][0]["task_id"] == compass_task_id

    running_detail_payload = _http_get_json(f"{COMPASS_BASE_URL}/api/tasks/{compass_task_id}")
    running_task = running_detail_payload.get("task", running_detail_payload)
    assert running_task["task_id"] == compass_task_id
    assert running_task["orchestratorTaskId"] == compass_task_id
    assert running_task["userRequest"] == task_text
    assert running_task["statusKind"] in {"active", "waiting", "completed"}
    assert running_task["chatHistory"], "expected chat history for the submitted task"
    assert running_task["chatHistory"][0]["role"] == "USER"
    assert running_task["current_major_step"]
    assert running_task["progress_steps"]

    final_task_payload = _poll_task(compass_task_id)
    final_state = _task_state(final_task_payload)
    assert final_state == "TASK_STATE_COMPLETED", final_task_payload

    completed_detail_payload = _http_get_json(f"{COMPASS_BASE_URL}/api/tasks/{compass_task_id}")
    completed_task = completed_detail_payload.get("task", completed_detail_payload)
    assert completed_task["task_id"] == compass_task_id
    assert completed_task["orchestratorTaskId"] == compass_task_id
    assert completed_task["statusKind"] == "completed"
    assert completed_task["completed_at"]
    assert completed_task["elapsed_ms"] >= 0
    assert completed_task["current_major_step"]
    assert completed_task["progress_steps"]

    metadata = completed_task.get("metadata", {})
    assert metadata.get("teamLeadTaskId"), "expected Team Lead task id in Compass metadata"

    # v0.8 timeline redesign: the new structured fields are populated and the
    # closing row is ``compass.task_completed#0``. We don't assert the exact
    # row count here (loops vary by task) but require both the Compass and
    # downstream (tl/wd/cr) prefix step keys to appear in the timeline.
    rows = completed_task.get("majorStepRows") or {}
    assert rows, "expected majorStepRows on completed development task"
    step_keys = set(rows.keys())
    assert any(k.startswith("compass.") for k in step_keys), (
        f"no compass.* step keys in majorStepRows: {sorted(step_keys)}"
    )
    assert any(
        k.startswith("tl.") or k.startswith("wd.") or k.startswith("cr.")
        for k in step_keys
    ), f"no tl/wd/cr step keys in majorStepRows: {sorted(step_keys)}"
    assert completed_task.get("terminalStepInstanceKey") == "compass.task_completed#0", (
        f"expected terminalStepInstanceKey=compass.task_completed#0, got "
        f"{completed_task.get('terminalStepInstanceKey')!r}"
    )

    merged_artifact_metadata: dict[str, object] = {}
    for artifact in completed_task.get("artifacts", []) or []:
        merged_artifact_metadata.update(artifact.get("metadata") or {})
    assert merged_artifact_metadata.get("prUrl"), "expected PR URL in final Compass artifact metadata"
    assert merged_artifact_metadata.get("branch"), "expected branch in final Compass artifact metadata"

    logs_payload = _http_get_json(f"{COMPASS_BASE_URL}/logs/{compass_task_id}")
    logs = logs_payload.get("logs") or []
    assert logs, "expected merged logs for the completed development task"
    agents = {str(entry.get("agent") or "") for entry in logs}
    assert "compass" in agents
    assert "team-lead" in agents

    workspace_path = ARTIFACTS_ROOT / compass_task_id
    assert workspace_path.is_dir(), f"missing workspace: {workspace_path}"

    jira_ticket = _check_json_artifact(workspace_path, "team-lead/jira-ticket.json")
    if jira_key:
        assert jira_ticket.get("data", {}).get("key") == jira_key

    context_manifest = _check_json_artifact(workspace_path, "team-lead/context-manifest.json")
    assert context_manifest.get("data", {}).get("repo_cloned") is True
    assert context_manifest.get("data", {}).get("repo_path")

    analysis = _check_json_artifact(workspace_path, "team-lead/analysis.json", ["task_type"])
    delivery_plan = _check_json_artifact(workspace_path, "team-lead/delivery-plan.json", ["agent_type"])
    final_report = _check_json_artifact(workspace_path, "team-lead/final-report.json")
    assert final_report.get("data", {}).get("pr_url") or final_report.get("data", {}).get("prUrl")

    git_setup = _check_json_artifact(workspace_path, "web-dev/git-setup-log.json")
    assert git_setup.get("data", {}).get("repo_exists") is True
    branch_name = str(git_setup.get("data", {}).get("branch_name") or "")
    assert branch_name
    if jira_key:
        assert jira_key.lower() in branch_name.lower(), f"branch does not reference Jira key: {branch_name}"

    _check_json_artifact(workspace_path, "web-dev/implementation-plan.json")
    jira_update = _check_json_artifact(workspace_path, "web-dev/jira-update-log.json", ["jira_key", "pr_url"])
    pr_evidence = _check_json_artifact(workspace_path, "web-dev/pr-evidence.json", ["pr_url", "branch"])
    assert pr_evidence.get("data", {}).get("branch") == branch_name

    definition_of_done = delivery_plan.get("data", {}).get("definition_of_done", {})
    screenshot_required = bool(definition_of_done.get("screenshot_required"))
    screenshots_dir = workspace_path / "web-dev" / "screenshots"
    png_screenshots = sorted(path for path in screenshots_dir.glob("*.png") if path.is_file()) if screenshots_dir.is_dir() else []
    if screenshot_required:
        assert pr_evidence.get("data", {}).get("screenshot_included") is True
        assert pr_evidence.get("data", {}).get("screenshot_uploaded") is True
        assert png_screenshots, "expected PNG screenshots for a screenshot-required task"

    review_report = _check_json_artifact(workspace_path, "code-review/review-report.json", ["verdict"])
    assert review_report.get("data", {}).get("verdict") == "approved"

    optional_design_md = workspace_path / "ui-design" / "stitch" / "DESIGN.md"
    optional_design_html = workspace_path / "ui-design" / "stitch" / "code.html"
    if optional_design_md.exists() or optional_design_html.exists():
        assert optional_design_md.exists() or optional_design_html.exists()

    compass_log = workspace_path / "compass" / "agent.log"
    team_lead_log = workspace_path / "team-lead" / "agent.log"
    web_dev_log = workspace_path / "web-dev" / "agent.log"
    code_review_log = workspace_path / "code-review" / "agent.log"

    for log_path in (compass_log, team_lead_log, web_dev_log, code_review_log):
        assert log_path.exists(), f"missing agent log: {log_path}"
        _assert_no_sensitive_tokens(_read_text(log_path), label=str(log_path.relative_to(workspace_path)))

    compass_log_text = _read_text(compass_log)
    team_lead_log_text = _read_text(team_lead_log)
    web_dev_log_text = _read_text(web_dev_log)
    code_review_log_text = _read_text(code_review_log)

    assert "Request received by Compass" in compass_log_text
    assert "[A2A] → team-lead" in compass_log_text
    assert "development task complete" in compass_log_text or "Development task completed" in compass_log_text

    assert "dispatch_web_dev" in team_lead_log_text
    assert "dispatch_code_review" in team_lead_log_text
    assert "final-report.json" in team_lead_log_text or "report_success" in team_lead_log_text

    assert "[NODE] create_pr" in web_dev_log_text or "create_pr" in web_dev_log_text
    assert "jira-update-log.json" in web_dev_log_text or "jira transitioned to in review" in web_dev_log_text or "adding jira completion comment" in web_dev_log_text
    assert "PR created" in web_dev_log_text or "create_pr done" in web_dev_log_text

    assert "review report generated" in code_review_log_text or "Verdict: approved" in code_review_log_text

    command_logs = [
        workspace_path / "team-lead" / "command-log.txt",
        workspace_path / "web-dev" / "command-log.txt",
        workspace_path / "code-review" / "command-log.txt",
    ]
    stage_summaries = [
        workspace_path / "team-lead" / "stage-summary.json",
        workspace_path / "web-dev" / "stage-summary.json",
        workspace_path / "code-review" / "stage-summary.json",
    ]
    assert any(path.exists() for path in command_logs), "expected at least one command-log.txt in the task workspace"
    assert any(path.exists() for path in stage_summaries), "expected at least one stage-summary.json in the task workspace"

    _assert_no_sensitive_tokens(json.dumps(logs, ensure_ascii=False), label="merged-logs")
    _assert_no_sensitive_tokens(json.dumps(completed_task, ensure_ascii=False), label="task-detail")
    _assert_no_sensitive_tokens(json.dumps(jira_update, ensure_ascii=False), label="jira-update-log")
    _assert_no_sensitive_tokens(json.dumps(pr_evidence, ensure_ascii=False), label="pr-evidence")
    _assert_no_sensitive_tokens(json.dumps(review_report, ensure_ascii=False), label="review-report")

    print(f"[development-container-e2e] task_id={compass_task_id}")
    print(f"[development-container-e2e] workspace={workspace_path}")
    print(f"[development-container-e2e] branch={branch_name}")
    print(f"[development-container-e2e] pr={pr_evidence.get('data', {}).get('pr_url', '')}")