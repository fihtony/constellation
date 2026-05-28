"""Live Office task E2E tests against the real containerized Compass stack.

This suite sends generic office requests to the running Compass service,
waits for Compass to launch a per-task Office container, verifies the original
user path is bind-mounted directly, and asserts all outputs land under the
workspace in ``artifacts/``.

The natural-language request stays generic so Office methodology does not learn
fixture-specific clues. Authorized source paths and capability hints are passed
only through A2A metadata.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import time
import unicodedata
import urllib.request
from pathlib import Path

import pytest


pytestmark = pytest.mark.live


PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
TESTS_DATA = PROJECT_ROOT / "tests" / "data"
ARTIFACTS_ROOT = PROJECT_ROOT / "artifacts"

COMPASS_BASE_URL = os.environ.get("TEST_COMPASS_BASE_URL", "http://localhost:8000").rstrip("/")
RESUME_HTTP_TIMEOUT_SECONDS = 900
RESUME_RESULT_TIMEOUT_SECONDS = 1200

TOOLS_DATA_CSV = TESTS_DATA / "csv" / "sales_data.csv"
TOOLS_DATA_STLOUIS = TESTS_DATA / "stlouis"
TOOLS_DATA_2026 = TESTS_DATA / "2026"


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


def _poll_task(base_url: str, task_id: str, timeout_seconds: int = 900) -> dict:
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


def _source_root_for(source_path: Path) -> Path:
    return source_path if source_path.is_dir() else source_path.parent


def _snapshot_tree(path: Path) -> dict[str, tuple[int, int]]:
    root = path if path.is_dir() else path.parent
    snapshot: dict[str, tuple[int, int]] = {}
    if path.is_file():
        stat_result = path.stat()
        snapshot[path.name] = (stat_result.st_size, int(stat_result.st_mtime_ns))
        return snapshot
    for item in sorted(root.rglob("*")):
        rel = str(item.relative_to(root))
        stat_result = item.stat()
        snapshot[rel] = (stat_result.st_size, int(stat_result.st_mtime_ns))
    return snapshot


def _expected_output_path(output_filename: str, office_workspace: Path) -> Path:
    return office_workspace / output_filename


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value)


def _summarize_source_files(source_path: Path) -> list[Path]:
    if source_path.is_dir():
        return sorted(
            item for item in source_path.rglob("*")
            if item.is_file() and item.suffix.lower() in {".pdf", ".docx", ".txt", ".md", ".pptx"}
        )
    return [source_path]


def _docker_names() -> list[str]:
    proc = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"docker ps failed: {proc.stderr.strip()}")
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def _docker_inspect(container_name: str) -> dict:
    proc = subprocess.run(
        ["docker", "inspect", container_name],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"docker inspect failed for {container_name}: {proc.stderr.strip()}")
    payload = json.loads(proc.stdout or "[]")
    if not payload:
        raise RuntimeError(f"docker inspect returned no data for {container_name}")
    return payload[0]


def _wait_for_office_container(task_id: str, timeout_seconds: int = 180) -> tuple[str, dict]:
    deadline = time.time() + timeout_seconds
    prefix = f"office-{task_id.lower()}-"
    while time.time() < deadline:
        for name in _docker_names():
            if name.startswith(prefix):
                return name, _docker_inspect(name)
        time.sleep(0.5)
    raise TimeoutError(f"Office per-task container was not observed for {task_id}")


def _env_map_from_inspect(container_details: dict) -> dict[str, str]:
    env: dict[str, str] = {}
    for item in container_details.get("Config", {}).get("Env", []) or []:
        if "=" not in str(item):
            continue
        key, _, value = str(item).partition("=")
        env[key] = value
    return env


def _assert_source_mount(
    container_details: dict,
    source_root: Path,
    requested_source: Path,
    *,
    read_only: bool,
) -> None:
    source_root_real = os.path.realpath(str(source_root))
    requested_source_real = os.path.realpath(str(requested_source))

    relevant_mounts = []
    for mount in container_details.get("Mounts", []) or []:
        source = os.path.realpath(str(mount.get("Source") or ""))
        destination = str(mount.get("Destination") or "")
        if source != source_root_real:
            continue
        if not destination.startswith("/app/userdata/input-"):
            continue
        relevant_mounts.append(mount)

    assert relevant_mounts, (
        f"missing Office source bind mount for {source_root_real}; mounts={container_details.get('Mounts', [])}"
    )
    assert all(bool(mount.get("RW", False)) is (not read_only) for mount in relevant_mounts), (
        f"Office source mount read_only expectation failed: {relevant_mounts}"
    )

    env_map = _env_map_from_inspect(container_details)
    assert env_map.get("OFFICE_SOURCE_ROOT") == "/app/userdata"
    assert env_map.get("OFFICE_ALLOW_INPLACE_WRITES") == ("false" if read_only else "true")

    mount = relevant_mounts[0]
    relative = os.path.relpath(requested_source_real, source_root_real)
    expected_allowed = mount["Destination"] if relative in {".", ""} else os.path.join(mount["Destination"], relative)
    assert env_map.get("OFFICE_ALLOWED_BASE_PATHS") == expected_allowed


def _assert_no_unknown_dirs(root: Path) -> None:
    unknown_dirs = [path for path in root.rglob("*") if path.is_dir() and path.name == "unknown"]
    assert not unknown_dirs, f"unexpected unknown organize directories: {unknown_dirs}"


def _assert_live_stack_ready() -> None:
    try:
        with urllib.request.urlopen(f"{COMPASS_BASE_URL}/health", timeout=10) as resp:
            if resp.status != 200:
                pytest.skip(f"Compass is not healthy at {COMPASS_BASE_URL}")
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"Compass live stack is not reachable at {COMPASS_BASE_URL}: {exc}")

    proc = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}"],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    if proc.returncode != 0:
        pytest.skip(f"docker CLI is unavailable: {proc.stderr.strip()}")


@pytest.fixture(scope="session", autouse=True)
def require_live_stack() -> None:
    _assert_live_stack_ready()


OFFICE_TASKS = [
    (
        "csv_workspace_container",
        "Please analyze the authorized spreadsheet in the shared folder and write the result to the task workspace.",
        TOOLS_DATA_CSV,
        "analyze",
        "sales_data.csv.analysis.md",
    ),
    (
        "stlouis_workspace_container",
        "Please summarize the authorized documents in the shared folder and write the summaries to the task workspace.",
        TOOLS_DATA_STLOUIS,
        "summarize",
        "combined-summary.md",
    ),
    (
        "organize_workspace_container",
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
def test_office_task_workspace_live_container(
    test_id: str,
    description: str,
    source_path: Path,
    capability: str,
    output_filename: str,
) -> None:
    source_root = _source_root_for(source_path)
    source_snapshot_before = _snapshot_tree(source_root)

    ui_html = _http_get_text(f"{COMPASS_BASE_URL}/ui")
    assert "Compass Agent" in ui_html

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
        timeout=60,
    )

    compass_task_id = _task_id(initial_response)
    assert compass_task_id, f"[{test_id}] empty Compass task id"
    assert _task_state(initial_response) == "TASK_STATE_INPUT_REQUIRED", (
        f"[{test_id}] expected output-mode clarification first: {initial_response}"
    )

    with ThreadPoolExecutor(max_workers=1) as executor:
        resume_future = executor.submit(
            _http_post,
            f"{COMPASS_BASE_URL}/tasks/{compass_task_id}/resume",
            {"input": "workspace"},
            RESUME_HTTP_TIMEOUT_SECONDS,
        )

        container_name, container_details = _wait_for_office_container(compass_task_id)
        assert container_name.startswith(f"office-{compass_task_id.lower()}-")
        _assert_source_mount(container_details, source_root, source_path, read_only=True)

        resumed_response = resume_future.result(timeout=RESUME_RESULT_TIMEOUT_SECONDS)

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
            assert _normalize_text(doc_path.name) in _normalize_text(combined_text), (
                f"[{test_id}] combined summary missing {doc_path.name}"
            )

    if capability == "organize":
        organized_root = office_workspace / "organized-output" / "files"
        assert organized_root.is_dir(), f"[{test_id}] missing organized output root"
        organized_files = [path for path in organized_root.rglob("*") if path.is_file()]
        assert organized_files, f"[{test_id}] no organized files materialized"
        assert (organized_root / "Yan" / "2026-01" / "0103-1.txt").exists(), f"[{test_id}] missing Yan canonical file"
        assert (organized_root / "Ethan" / "2026-01" / "0103-4.txt").exists(), f"[{test_id}] missing Ethan January file"
        assert (organized_root / "Ethan" / "2026-02" / "0221-1.txt").exists(), f"[{test_id}] missing Ethan February file"
        _assert_no_unknown_dirs(organized_root)

    source_snapshot_after = _snapshot_tree(source_root)
    assert source_snapshot_after == source_snapshot_before, f"[{test_id}] source tree changed in workspace mode"
    assert not any(path.name == output_filename for path in source_root.rglob("*")), (
        f"[{test_id}] output escaped into original source tree"
    )

    office_log = ARTIFACTS_ROOT / compass_task_id / "office" / "agent.log"
    office_log_text = _read_text(office_log)

    assert office_log.exists(), f"[{test_id}] missing office log"
    assert "[NODE] handle_message" in office_log_text, f"[{test_id}] missing office handle_message log"
    assert "office agent started" in office_log_text, f"[{test_id}] missing office startup log"
    assert "office workspace prepared" in office_log_text, f"[{test_id}] missing office workspace log"
    assert "[A2A] ← compass" in office_log_text, f"[{test_id}] missing office receive-from-compass log"

    compass_log = ARTIFACTS_ROOT / compass_task_id / "compass" / "agent.log"
    if compass_log.exists():
        compass_log_text = _read_text(compass_log)
        assert "task_type='office'" in compass_log_text, f"[{test_id}] compass did not classify as office"
        assert "office task awaiting output mode" in compass_log_text, f"[{test_id}] missing output-mode inquiry log"
        assert "[A2A] → office" in compass_log_text, f"[{test_id}] missing office dispatch log"