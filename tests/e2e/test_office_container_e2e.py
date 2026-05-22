"""Container-backed Office E2E tests against the real Compass service."""

from __future__ import annotations

import json
import os
import time
import urllib.request
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
TESTS_DATA = PROJECT_ROOT / "tests" / "data"
ARTIFACTS_ROOT = PROJECT_ROOT / "artifacts"

TOOLS_DATA_CSV = TESTS_DATA / "csv" / "sales_data.csv"
TOOLS_DATA_STLOUIS = TESTS_DATA / "stlouis"
TOOLS_DATA_2026 = TESTS_DATA / "2026"


def _default_compass_url() -> str:
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return "http://compass:8000"
    return "http://127.0.0.1:8000"


COMPASS_BASE_URL = os.environ.get("COMPASS_BASE_URL", _default_compass_url()).rstrip("/")


def _http_get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _http_get_text(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:
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


def _poll_task(base_url: str, task_id: str, timeout_seconds: int = 600) -> dict:
    deadline = time.time() + timeout_seconds
    terminal = {
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELLED",
        "TASK_STATE_INPUT_REQUIRED",
    }
    while time.time() < deadline:
        result = _http_get(f"{base_url}/tasks/{task_id}")
        if _task_state(result) in terminal:
            return result
        time.sleep(2)
    raise TimeoutError(f"Task {task_id} did not complete within {timeout_seconds}s")


def _expected_output_path(output_filename: str, office_workspace: Path) -> Path:
    return office_workspace / output_filename


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _source_root_for(source_path: Path) -> Path:
    return source_path if source_path.is_dir() else source_path.parent


def _snapshot_tree(root: Path) -> dict[str, tuple[int, int]]:
    snapshot: dict[str, tuple[int, int]] = {}
    for item in sorted(root.rglob("*")):
        if item.is_file():
            rel = item.relative_to(root).as_posix()
            stat = item.stat()
            snapshot[rel] = (stat.st_size, int(stat.st_mtime_ns))
    return snapshot


def _summarize_source_files(source_path: Path) -> list[Path]:
    if source_path.is_dir():
        return sorted(
            item for item in source_path.rglob("*")
            if item.is_file() and item.suffix.lower() in {".pdf", ".docx", ".txt", ".md", ".pptx"}
        )
    return [source_path]


OFFICE_TASKS = [
    (
        "csv_workspace",
        "Please analyze the authorized spreadsheet in the shared folder and write the result to the task workspace.",
        TOOLS_DATA_CSV,
        "analyze",
        "sales_data.csv.analysis.md",
    ),
    (
        "stlouis_workspace",
        "Please summarize the authorized documents in the shared folder and write the summaries to the task workspace.",
        TOOLS_DATA_STLOUIS,
        "summarize",
        "combined-summary.md",
    ),
    (
        "2026_workspace",
        "Please organize the authorized files in the shared folder into the task workspace.",
        TOOLS_DATA_2026,
        "organize",
        "organization-plan.md",
    ),
]


@pytest.mark.parametrize(
    "test_id,description,source_path,capability,output_filename",
    OFFICE_TASKS,
    ids=[case[0] for case in OFFICE_TASKS],
)
def test_office_task_workspace_container(
    test_id: str,
    description: str,
    source_path: Path,
    capability: str,
    output_filename: str,
):
    ui_html = _http_get_text(f"{COMPASS_BASE_URL}/ui")
    assert "Compass Agent" in ui_html

    source_root = _source_root_for(source_path)
    source_snapshot_before = _snapshot_tree(source_root)

    initial_response = _http_post(
        f"{COMPASS_BASE_URL}/message:send",
        {
            "message": {
                "messageId": f"office-container-e2e-{test_id}",
                "role": "ROLE_USER",
                "parts": [{"text": description}],
                "metadata": {
                    "capability": capability,
                    "source_paths": [str(source_path)],
                },
            },
            "configuration": {"returnImmediately": True},
        },
    )

    compass_task_id = _task_id(initial_response)
    assert compass_task_id, f"[{test_id}] empty Compass task id"
    assert _task_state(initial_response) == "TASK_STATE_INPUT_REQUIRED", (
        f"[{test_id}] expected output-mode clarification first: {initial_response}"
    )

    resumed_response = _http_post(
        f"{COMPASS_BASE_URL}/tasks/{compass_task_id}/resume",
        {"input": "workspace"},
        timeout=600,
    )
    assert _task_id(resumed_response) == compass_task_id

    compass_final = _poll_task(COMPASS_BASE_URL, compass_task_id)
    assert _task_state(compass_final) == "TASK_STATE_COMPLETED", f"[{test_id}] compass failed: {compass_final}"

    office_base = ARTIFACTS_ROOT / compass_task_id / "office"
    office_workspace = office_base / "artifacts"
    task_report_path = office_base / "task-report.json"
    expected_output_path = _expected_output_path(output_filename, office_workspace)

    assert office_workspace.is_dir(), f"[{test_id}] missing office workspace: {office_workspace}"
    assert task_report_path.exists(), f"[{test_id}] missing task-report.json"
    assert expected_output_path.exists(), f"[{test_id}] missing expected output: {expected_output_path}"

    report = json.loads(task_report_path.read_text(encoding="utf-8"))
    assert report.get("data", {}).get("output_mode") == "workspace"
    assert report.get("data", {}).get("warnings_count") == 0
    assert all(path.startswith("/app/userdata/") for path in report.get("data", {}).get("source_paths", []))

    if capability == "summarize":
        combined_text = _read_text(expected_output_path)
        for doc_path in _summarize_source_files(source_path):
            per_doc_output = office_workspace / f"{doc_path.name}.summary.md"
            assert per_doc_output.exists(), f"[{test_id}] missing per-document summary: {per_doc_output}"
            assert doc_path.name in combined_text, f"[{test_id}] combined summary missing {doc_path.name}"

    if capability == "organize":
        organized_root = office_workspace / "organized-output" / "files"
        assert organized_root.is_dir(), f"[{test_id}] missing organized output root"
        organized_files = [path for path in organized_root.rglob("*") if path.is_file()]
        assert organized_files, f"[{test_id}] no organized files materialized"

    source_snapshot_after = _snapshot_tree(source_root)
    assert source_snapshot_after == source_snapshot_before, f"[{test_id}] source tree changed in workspace mode"
    assert not any(path.name == output_filename for path in source_root.rglob("*")), (
        f"[{test_id}] output escaped into source tree"
    )