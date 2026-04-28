#!/usr/bin/env python3
"""End-to-end validation for Office workflows through Compass.

This script exercises the real Compass -> Office Agent flow with the Copilot CLI
runtime and verifies two user-visible outcomes:

1. Office task outputs land under artifacts/workspaces on the host.
2. Office tasks appear in the Compass task-board API that drives the UI.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_test_support import Reporter, http_request, load_env_file, run_command, summary_exit_code

COMPASS_URL = "http://localhost:8080"
REGISTRY_URL = "http://localhost:9000"
CONTAINER_ARTIFACT_ROOT = "/app/artifacts"
TASK_TIMEOUT = 900
POLL_INTERVAL = 3
TERMINAL_STATES = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Office Agent end-to-end tests through Compass.")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args()


def _artifact_root_host() -> Path:
    compass_env = load_env_file("compass/.env")
    configured = (
        os.environ.get("ARTIFACT_ROOT_HOST", "").strip()
        or compass_env.get("ARTIFACT_ROOT_HOST", "").strip()
        or str(PROJECT_ROOT / "artifacts")
    )
    return Path(configured).resolve()


def _container_to_host(path: str) -> Path:
    if not path:
        return Path()
    artifact_root = _artifact_root_host()
    if path.startswith(CONTAINER_ARTIFACT_ROOT):
        suffix = path[len(CONTAINER_ARTIFACT_ROOT):].lstrip("/")
        return (artifact_root / suffix).resolve()
    return Path(path).resolve()


def _resolve_runtime_inputs() -> tuple[str, str]:
    common_env = load_env_file("common/.env")
    tests_env = load_env_file("tests/.env")
    token = ""
    runtime = str(common_env.get("AGENT_RUNTIME") or "").strip()
    for mapping in (common_env, tests_env):
        token = str(mapping.get("COPILOT_GITHUB_TOKEN") or "").strip()
        if token:
            break
    if not token and os.environ.get("CONSTELLATION_TRUSTED_ENV", "").strip().lower() in {"1", "true", "yes", "on"}:
        token = str(os.environ.get("COPILOT_GITHUB_TOKEN") or "").strip()
    return runtime, token


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _run_checked(args: list[str], reporter: Reporter, *, label: str, timeout: int = 1200) -> bool:
    code, stdout, stderr = run_command(args, cwd=str(PROJECT_ROOT), timeout=timeout)
    if code == 0:
        reporter.ok(label)
        return True
    reporter.fail(label, (stderr or stdout)[-1200:])
    return False


def _wait_for_health(url: str, timeout: int = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body, _ = http_request(f"{url}/health", timeout=5)
        if status == 200 and body.get("status") == "ok":
            return True
        time.sleep(1)
    return False


def _ensure_stack(reporter: Reporter) -> bool:
    reporter.section("T0 — Prepare Compass Stack")

    if shutil.which("docker") is None:
        reporter.fail("Docker CLI is not available")
        return False

    runtime, token = _resolve_runtime_inputs()
    if runtime != "copilot-cli":
        reporter.fail("common/.env does not set AGENT_RUNTIME=copilot-cli", f"current={runtime!r}")
        return False
    if not token:
        reporter.fail("COPILOT_GITHUB_TOKEN is not configured in common/.env or tests/.env")
        return False
    reporter.ok("Copilot CLI runtime prerequisites are configured")

    reporter.step("Build Office Agent image")
    if not _run_checked([str(PROJECT_ROOT / "build-agents.sh"), "office"], reporter, label="Office Agent image built"):
        return False

    reporter.step("Build Compass and init-register images")
    if not _run_checked(["docker", "compose", "build", "compass", "init-register"], reporter, label="Compass and init-register images built"):
        return False

    reporter.step("Start Compass stack")
    if not _run_checked(
        ["docker", "compose", "up", "-d", "--force-recreate", "registry", "jira", "scm", "ui-design", "compass"],
        reporter,
        label="Compass stack started",
        timeout=1800,
    ):
        return False

    reporter.step("Refresh registry definitions")
    if not _run_checked(["docker", "compose", "run", "--rm", "init-register"], reporter, label="Registry bootstrap completed"):
        return False

    if not _wait_for_health(COMPASS_URL):
        reporter.fail("Compass did not become healthy")
        return False
    reporter.ok("Compass health check passed")

    status, body, _ = http_request(f"{REGISTRY_URL}/agents", timeout=20)
    if status != 200 or not isinstance(body, list):
        reporter.fail("Could not query registry agent definitions", f"status={status} body={body}")
        return False
    if any(item.get("agent_id") == "office-agent" for item in body):
        reporter.ok("Office Agent definition is registered in the Registry")
    else:
        reporter.fail("Office Agent definition is missing from the Registry")
        return False
    return True


def _send_compass_message(text: str, *, requested_capability: str = "", context_id: str = ""):
    payload = {
        "message": {
            "messageId": f"office-e2e-{int(time.time() * 1000)}",
            "role": "ROLE_USER",
            "parts": [{"text": text}],
        }
    }
    if requested_capability:
        payload["requestedCapability"] = requested_capability
    if context_id:
        payload["contextId"] = context_id
    return http_request(f"{COMPASS_URL}/message:send", method="POST", payload=payload, timeout=60)


def _wait_for_task(task_id: str, timeout: int = TASK_TIMEOUT) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body, _ = http_request(f"{COMPASS_URL}/tasks/{task_id}", timeout=10)
        if status == 200 and isinstance(body, dict):
            task = body.get("task") if isinstance(body.get("task"), dict) else None
            if task:
                state = str((task.get("status") or {}).get("state") or "")
                if state in TERMINAL_STATES:
                    return task
        time.sleep(POLL_INTERVAL)
    return None


def _task_card(task_id: str) -> dict:
    status, body, _ = http_request(f"{COMPASS_URL}/api/tasks/{task_id}/card", timeout=15)
    if status == 200 and isinstance(body, dict) and isinstance(body.get("task"), dict):
        return body["task"]
    return {}


def _assert_card_visible(task_id: str, capability: str, reporter: Reporter, label: str) -> None:
    status, body, _ = http_request(f"{COMPASS_URL}/api/tasks", timeout=15)
    if status != 200 or not isinstance(body, dict):
        reporter.fail(f"{label} task board query failed", f"status={status} body={body}")
        return
    cards = body.get("tasks") if isinstance(body.get("tasks"), list) else []
    card = next((item for item in cards if item.get("id") == task_id), None)
    if not card:
        reporter.fail(f"{label} task is missing from the Compass task board", task_id)
        return
    if capability in (card.get("workflow") or []):
        reporter.ok(f"{label} task is visible in the Compass task board")
    else:
        reporter.fail(f"{label} task card is visible but workflow is unexpected", json.dumps(card, ensure_ascii=False)[:500])


def _assert_copilot_cli_runtime(workspace_host: Path, reporter: Reporter, label: str) -> None:
    stage_summary_path = workspace_host / "office-agent" / "stage-summary.json"
    try:
        stage_summary = json.loads(stage_summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        reporter.fail(f"{label} stage-summary.json is unreadable", str(exc))
        return
    runtime_config = stage_summary.get("runtimeConfig") if isinstance(stage_summary.get("runtimeConfig"), dict) else {}
    runtime = runtime_config.get("runtimeConfig") if isinstance(runtime_config.get("runtimeConfig"), dict) else {}
    requested = str(runtime.get("requestedBackend") or "")
    effective = str(runtime.get("effectiveBackend") or "")
    if requested == "copilot-cli" and effective == "copilot-cli":
        reporter.ok(f"{label} used Copilot CLI runtime")
    else:
        reporter.fail(f"{label} did not stay on Copilot CLI runtime", f"requested={requested!r}, effective={effective!r}")


def _top_sales_rep(csv_path: Path) -> str:
    totals: dict[str, float] = {}
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            rep = str(row.get("Sales_Rep") or "").strip()
            if not rep:
                continue
            totals[rep] = totals.get(rep, 0.0) + float(str(row.get("Sales_Amount") or "0"))
    return max(totals.items(), key=lambda item: item[1])[0]


def _run_office_task(instruction: str, capability: str, reporter: Reporter, label: str) -> tuple[dict | None, Path]:
    status, body, _ = _send_compass_message(instruction, requested_capability=capability)
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("task"), dict):
        reporter.fail(f"{label} submission failed", f"status={status} body={body}")
        return None, Path()

    task = body["task"]
    task_id = str(task.get("id") or "")
    state = str((task.get("status") or {}).get("state") or "")
    if state == "TASK_STATE_INPUT_REQUIRED":
        reporter.ok(f"{label} entered the Compass output-mode prompt")
    else:
        reporter.fail(f"{label} did not enter the expected output-mode prompt", f"state={state}")
        return None, Path()

    _assert_card_visible(task_id, capability, reporter, label)

    reply_status, reply_body, _ = _send_compass_message("Use workspace output.", context_id=task_id)
    if reply_status != 200 or not isinstance(reply_body, dict) or not isinstance(reply_body.get("task"), dict):
        reporter.fail(f"{label} resume request failed", f"status={reply_status} body={reply_body}")
        return None, Path()

    final_task = _wait_for_task(task_id)
    if not final_task:
        reporter.fail(f"{label} timed out")
        return None, Path()
    final_state = str((final_task.get("status") or {}).get("state") or "")
    if final_state == "TASK_STATE_COMPLETED":
        reporter.ok(f"{label} completed through Compass")
    else:
        reporter.fail(f"{label} ended in {final_state}", json.dumps((final_task.get("status") or {}).get("message") or {}, ensure_ascii=False)[:400])
        return None, Path()

    _assert_card_visible(task_id, capability, reporter, label)
    card = _task_card(task_id)
    if card.get("statusKind") == "completed":
        reporter.ok(f"{label} completion is reflected in the Compass task card")
    else:
        reporter.fail(f"{label} task card did not reach completed status", json.dumps(card, ensure_ascii=False)[:500])

    workspace_container = str(final_task.get("workspacePath") or "")
    workspace_host = _container_to_host(workspace_container)
    if workspace_host.is_dir():
        reporter.ok(f"{label} workspace exists on the host")
    else:
        reporter.fail(f"{label} workspace is missing on the host", f"container={workspace_container} host={workspace_host}")
        return None, Path()
    return final_task, workspace_host


def test_csv_analysis(reporter: Reporter) -> None:
    reporter.section("T1 — CSV Analysis Through Compass")
    csv_path = (PROJECT_ROOT / "tests" / "data" / "csv" / "sales_data.csv").resolve()
    expected_top_rep = _top_sales_rep(csv_path)
    instruction = f"Analyze {csv_path} and find the sales rep with the highest total sales."
    _, workspace_host = _run_office_task(instruction, "office.data.analyze", reporter, "CSV analysis")
    if not workspace_host:
        return

    report_path = workspace_host / "office-agent" / "analysis.md"
    try:
        report_text = report_path.read_text(encoding="utf-8")
    except OSError as exc:
        reporter.fail("CSV analysis report was not written to artifacts/workspaces", str(exc))
        return
    reporter.ok("CSV analysis report exists under artifacts/workspaces")
    if expected_top_rep.lower() in report_text.lower():
        reporter.ok(f"CSV analysis names the top sales rep ({expected_top_rep})")
    else:
        reporter.fail("CSV analysis did not mention the expected top rep", report_text[:300])
    _assert_copilot_cli_runtime(workspace_host, reporter, "CSV analysis")


def test_pdf_summary(reporter: Reporter) -> None:
    reporter.section("T2 — PDF Summary Through Compass")
    pdf_dir = (PROJECT_ROOT / "tests" / "data" / "stlouis").resolve()
    instruction = f"Summarize the PDF files in {pdf_dir} and extract a short timeline of the months or events they mention."
    _, workspace_host = _run_office_task(instruction, "office.folder.summarize", reporter, "PDF summary")
    if not workspace_host:
        return

    summary_path = workspace_host / "office-agent" / "summary.md"
    try:
        summary_text = summary_path.read_text(encoding="utf-8")
    except OSError as exc:
        reporter.fail("PDF summary report was not written to artifacts/workspaces", str(exc))
        return
    reporter.ok("PDF summary report exists under artifacts/workspaces")
    markers = ["janvier", "january", "fevrier", "february", "octobre", "october", "decembre", "december"]
    normalized = summary_text.lower()
    if any(marker in normalized for marker in markers):
        reporter.ok("PDF summary reflects month/event context from the fixture data")
    else:
        reporter.fail("PDF summary did not include expected notice context", summary_text[:300])
    _assert_copilot_cli_runtime(workspace_host, reporter, "PDF summary")


def test_essay_organize(reporter: Reporter) -> None:
    reporter.section("T3 — Essay Organize Through Compass")
    essays_dir = (PROJECT_ROOT / "tests" / "data" / "2026").resolve()
    instruction = (
        f"Read {essays_dir}, group each student's essays by date into the workspace, preserve the originals, "
        "and create grouped text files for the extracted essays."
    )
    _, workspace_host = _run_office_task(instruction, "office.folder.organize", reporter, "Essay organize")
    if not workspace_host:
        return

    output_root = workspace_host / "office-agent" / "organized-output"
    manifest_path = output_root / ".office-agent-manifest.json"
    if output_root.is_dir():
        reporter.ok("Essay organize output exists under artifacts/workspaces")
    else:
        reporter.fail("Essay organize output folder is missing", str(output_root))
        return

    generated_files = [
        path for path in sorted(output_root.rglob("*.txt"))
        if "originals" not in path.relative_to(output_root).parts
    ]
    if len(generated_files) >= 3:
        reporter.ok("Essay organize produced grouped output files")
    else:
        reporter.fail("Essay organize produced too few grouped files", f"count={len(generated_files)}")

    manifest = _read_json(manifest_path)
    executed_actions = manifest.get("executedActions") if isinstance(manifest.get("executedActions"), list) else []
    if executed_actions:
        reporter.ok("Essay organize wrote an execution manifest")
    else:
        reporter.fail("Essay organize manifest has no executed actions", str(manifest_path))

    generated_rel_paths = [str(path.relative_to(output_root)) for path in generated_files[:40]]
    has_dated_structure = any(
        re.search(r"/(?:19|20)\d{2}/\d{4}/", f"/{rel}")
        or re.search(r"(?:19|20)\d{2}-\d{2}-\d{2}", rel)
        or re.search(r"/(?:19|20)\d{2}/(?:grouped|by-student)/[^/]+/\d{4}\.txt$", f"/{rel}")
        or re.search(r"/(?:19|20)\d{2}/(?:grouped|by-student)/[^/]+/\d{4}/[^/]+\.txt$", f"/{rel}")
        for rel in generated_rel_paths
    )
    has_known_student = any(
        re.search(r"\b(Ethan|Yan|Alice|Charlie|Student_Ethan|Student_Yan|Student_Alice|Student_Charlie)\b", rel)
        for rel in generated_rel_paths
    )
    if has_dated_structure and has_known_student:
        reporter.ok("Essay organize created dated per-student output paths")
    else:
        reporter.fail("Essay organize output paths do not show the expected student/date grouping", "\n".join(generated_rel_paths[:20]))

    readme_files = [path for path in sorted(output_root.rglob("README.*")) if path.is_file()]
    if readme_files and all("\\n" not in path.read_text(encoding="utf-8") for path in readme_files):
        reporter.ok("Essay organize README files use real line breaks")
    else:
        reporter.fail("Essay organize README files still contain literal \\n sequences")

    ethan_files = [
        path for path in generated_files
        if path.name != "README.txt" and re.search(r"\b(Ethan|Student_Ethan)\b", str(path.relative_to(output_root)))
    ]
    ethan_unique_contents = {
        path.read_text(encoding="utf-8", errors="replace").strip()
        for path in ethan_files
        if path.read_text(encoding="utf-8", errors="replace").strip()
    }
    if ethan_files and len(ethan_unique_contents) >= min(3, len(ethan_files)):
        reporter.ok("Essay organize preserved distinct essay content across Ethan's dated files")
    else:
        reporter.fail(
            "Essay organize still repeats content across Ethan's grouped files",
            "\n".join(str(path.relative_to(output_root)) for path in ethan_files[:10]),
        )
    _assert_copilot_cli_runtime(workspace_host, reporter, "Essay organize")


def main() -> int:
    args = _parse_args()
    reporter = Reporter(verbose=args.verbose)

    if not _ensure_stack(reporter):
        return summary_exit_code(reporter)

    test_csv_analysis(reporter)
    test_pdf_summary(reporter)
    test_essay_organize(reporter)
    return summary_exit_code(reporter)


if __name__ == "__main__":
    raise SystemExit(main())