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
from common.runtime.adapter import resolve_backend_name

COMPASS_URL = "http://localhost:8080"
REGISTRY_URL = "http://localhost:9000"
CONTAINER_ARTIFACT_ROOT = "/app/artifacts"
TASK_TIMEOUT = 900
POLL_INTERVAL = 3
TERMINAL_STATES = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED"}


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Office Agent end-to-end tests through Compass.")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--reuse-images",
        action="store_true",
        help="Reuse existing Office/Compass images instead of rebuilding them before the test run.",
    )
    parser.add_argument(
        "--test",
        type=str,
        default="",
        help="Run only the specified test function(s), comma-separated (e.g. 'test_csv_analysis,test_pdf_summary').",
    )
    return parser.parse_args()


def _host_artifact_root() -> Path:
    return (PROJECT_ROOT / "artifacts").resolve()


def _container_to_host(path: str) -> Path:
    if not path:
        return Path()
    artifact_root = _host_artifact_root()
    if path.startswith(CONTAINER_ARTIFACT_ROOT):
        suffix = path[len(CONTAINER_ARTIFACT_ROOT):].lstrip("/")
        return (artifact_root / suffix).resolve()
    return Path(path).resolve()


def _resolve_runtime_inputs() -> tuple[str, str, str]:
    common_env = load_env_file("common/.env")
    tests_env = load_env_file("tests/.env")
    token = ""
    requested_runtime = str(common_env.get("AGENT_RUNTIME") or "connect-agent").strip()
    _, effective_runtime = resolve_backend_name(requested_runtime)
    for mapping in (common_env, tests_env):
        token = str(mapping.get("COPILOT_GITHUB_TOKEN") or "").strip()
        if token:
            break
    if not token and os.environ.get("CONSTELLATION_TRUSTED_ENV", "").strip().lower() in {"1", "true", "yes", "on"}:
        token = str(os.environ.get("COPILOT_GITHUB_TOKEN") or "").strip()
    return requested_runtime, effective_runtime, token


def _expected_runtime() -> tuple[str, str]:
    requested_runtime, effective_runtime, _ = _resolve_runtime_inputs()
    return requested_runtime, effective_runtime


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _prepare_rw_fixture_dir(source_name: str) -> Path:
    source_dir = (PROJECT_ROOT / "tests" / "data" / source_name).resolve()
    target_dir = (PROJECT_ROOT / "tests" / "data" / f"{source_name}_rw").resolve()
    if target_dir.exists():
        shutil.rmtree(target_dir)
    shutil.copytree(source_dir, target_dir)
    return target_dir


def _classify_office_question(question: str) -> str:
    """Classify an office clarification question type for auto-reply.

    Returns one of: 'authorize_path', 'path', 'output_mode', 'write_confirm', 'unknown'.
    """
    lowered = question.lower()
    # Authorization / target-files questions (Compass asks user to authorize a path)
    if any(
        phrase in lowered
        for phrase in [
            "authorize",
            "explicit authorization",
            "target files",
            "target directories",
        ]
    ):
        return "authorize_path"
    if "absolute path" in lowered:
        return "path"
    # Write/access permission / confirmation (check before output_mode to avoid false matches)
    if any(
        phrase in lowered
        for phrase in [
            "approve write",
            "write access",
            "write permission",
            "grant write",
            "confirm write",
            "write directly",
            "modify the original",
            "modify files directly",
            "in-place write",
            "inplace write",
            "allow_inplace",
            "allow write",
            "permission to write",
            "permission to modify",
            "do you allow",
            "overwrite",
            "save to the same",
            # Broader permission/confirm patterns
            "grant permission",
            "do you confirm",
            "may i modify",
            "can i modify",
            "modify.*in.place",
            "confirm.*modify",
            "confirm.*in.place",
        ]
    ) or (
        "confirm" in lowered
        and ("modify" in lowered or "in-place" in lowered or "in place" in lowered or "write" in lowered)
    ) or (
        "permission" in lowered
        and ("access" in lowered or "read" in lowered or "write" in lowered)
    ):
        return "write_confirm"
    if any(
        phrase in lowered
        for phrase in [
            "choose where",
            "workspace only",
            "write its output",
            "choose workspace or in-place",
            "in-place output",
            "output mode",
            "where should the output",
            "where would you like",
            "where do you want the output",
            "where to write",
            "output destination",
            "output location",
            "save the output",
            "write the output",
            "write output",
        ]
    ) or (
        "workspace" in lowered
        and ("in-place" in lowered or "in place" in lowered or "inplace" in lowered)
    ):
        return "output_mode"
    return "unknown"


def _run_checked(args: list[str], reporter: Reporter, *, label: str, timeout: int = 1200) -> bool:
    code, stdout, stderr = run_command(args, cwd=str(PROJECT_ROOT), timeout=timeout)
    if code == 0:
        reporter.ok(label)
        return True
    reporter.fail(label, (stderr or stdout)[-1200:])
    return False


def _docker_image_exists(image_name: str) -> bool:
    code, _, _ = run_command(
        ["docker", "image", "inspect", image_name],
        cwd=str(PROJECT_ROOT),
        timeout=120,
    )
    return code == 0


def _wait_for_health(url: str, timeout: int = 90) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body, _ = http_request(f"{url}/health", timeout=5)
        if status == 200 and body.get("status") == "ok":
            return True
        time.sleep(1)
    return False


def _ensure_stack(reporter: Reporter, *, reuse_images: bool = False) -> bool:
    reporter.section("T0 — Prepare Compass Stack")

    if shutil.which("docker") is None:
        reporter.fail("Docker CLI is not available")
        return False

    requested_runtime, effective_runtime, token = _resolve_runtime_inputs()
    if effective_runtime not in {"connect-agent", "copilot-cli", "claude-code"}:
        reporter.fail(
            "common/.env does not configure a supported agentic runtime",
            f"requested={requested_runtime!r} effective={effective_runtime!r}",
        )
        return False
    if effective_runtime == "copilot-cli" and not token:
        reporter.fail("COPILOT_GITHUB_TOKEN is not configured in common/.env or tests/.env")
        return False
    reporter.ok(f"Runtime prerequisites are configured ({effective_runtime})")

    if reuse_images and _docker_image_exists("constellation-office-agent:latest"):
        reporter.ok("Reusing existing Office Agent image")
    else:
        reporter.step("Build Office Agent image")
        if not _run_checked([str(PROJECT_ROOT / "build-agents.sh"), "office"], reporter, label="Office Agent image built"):
            return False

    if reuse_images and _docker_image_exists("constellation-compass-agent:latest") and _docker_image_exists("constellation-init-register:latest"):
        reporter.ok("Reusing existing Compass and init-register images")
    else:
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


def _assert_expected_runtime(workspace_host: Path, reporter: Reporter, label: str) -> None:
    stage_summary_path = workspace_host / "office-agent" / "stage-summary.json"
    try:
        stage_summary = json.loads(stage_summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        reporter.fail(f"{label} stage-summary.json is unreadable", str(exc))
        return
    runtime_config = stage_summary.get("runtimeConfig") if isinstance(stage_summary.get("runtimeConfig"), dict) else {}
    # runtimeConfig block stores the runtime summary under the "runtime" key (see build_office_agent_runtime_config)
    runtime = runtime_config.get("runtime") if isinstance(runtime_config.get("runtime"), dict) else {}
    requested = str(runtime.get("requestedBackend") or "")
    effective = str(runtime.get("effectiveBackend") or "")
    expected_requested, expected_effective = _expected_runtime()
    if requested == expected_requested and effective == expected_effective:
        reporter.ok(f"{label} used the configured runtime ({expected_effective})")
    else:
        reporter.fail(
            f"{label} did not stay on the configured runtime",
            (
                f"expected_requested={expected_requested!r}, expected_effective={expected_effective!r}, "
                f"requested={requested!r}, effective={effective!r}"
            ),
        )


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


def _extract_expected_txt_fragments(root: Path) -> set[str]:
    fragments: set[str] = set()
    for path in sorted(root.rglob("*.txt")):
        if "organized-output" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        current_lines: list[str] = []
        seen_marker = False
        for line in text.splitlines():
            if line.strip().startswith(">>>"):
                if seen_marker:
                    body = "\n".join(current_lines).strip()
                    if body:
                        fragments.add(body)
                current_lines = []
                seen_marker = True
                continue
            if seen_marker:
                current_lines.append(line)
        body = "\n".join(current_lines).strip()
        if seen_marker and body:
            fragments.add(body)
    return fragments


def _fragment_output_paths(manifest: dict) -> set[Path]:
    executed_actions = manifest.get("executedActions") if isinstance(manifest.get("executedActions"), list) else []
    paths: set[Path] = set()
    for action in executed_actions:
        if str(action.get("action") or "") != "write_fragment":
            continue
        destination = str(action.get("destination") or "").strip()
        if destination:
            paths.add(Path(destination).resolve())
    return paths


def _reply_to_input_required(
    task_id: str,
    question: str,
    target_path: Path | None,
    reporter: Reporter,
    label: str,
    *,
    output_mode: str,
) -> dict | None:
    """Auto-reply to an office task clarification question from Compass.

    Handles:
    - 'authorize_path'— user is asked to authorize file/folder access
    - 'path'         — user is asked for an absolute file/folder path
    - 'output_mode'  — user is asked to choose workspace vs in-place output
    - 'write_confirm'— user is asked to approve write access for in-place mode
    - 'unknown'      — logs as unexpected and returns None (test fails)
    """
    q_type = _classify_office_question(question)
    if q_type == "authorize_path":
        if not target_path:
            reporter.fail(f"{label} asked to authorize a path but test has no target path", question[:400])
            return None
        reply_text = f"Authorize {target_path} as a Target Files/Directories entry."
    elif q_type == "path":
        if not target_path:
            reporter.fail(f"{label} asked for an absolute path but test has no target path", question[:400])
            return None
        reply_text = str(target_path)
    elif q_type == "output_mode":
        reply_text = "Modify the original folder directly." if output_mode == "inplace" else "Use workspace output."
    elif q_type == "write_confirm":
        if output_mode == "inplace":
            reply_text = "Yes. Approve write access."
        else:
            # workspace mode: confirm read-only access to copy files into workspace
            reply_text = "Yes. Proceed with read-only workspace copy."
    else:
        reporter.fail(f"{label} asked an unexpected clarification question", question[:400])
        return None

    reply_status, reply_body, _ = _send_compass_message(reply_text, context_id=task_id)
    if reply_status != 200 or not isinstance(reply_body, dict) or not isinstance(reply_body.get("task"), dict):
        reporter.fail(f"{label} resume request failed", f"status={reply_status} body={reply_body}")
        return None
    return reply_body["task"]


def _run_office_task(
    instruction: str,
    capability: str,
    reporter: Reporter,
    label: str,
    *,
    target_path: Path | None = None,
    output_mode: str = "workspace",
) -> tuple[dict | None, Path | None]:
    """Submit an office task to Compass and poll until completion.

    Handles INPUT_REQUIRED clarification rounds asynchronously — the Compass
    agentic workflow may transition to INPUT_REQUIRED at any point after the
    initial WORKING state, so we poll continuously rather than checking only
    the initial submission response.
    """
    status, body, _ = _send_compass_message(instruction, requested_capability=capability)
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("task"), dict):
        reporter.fail(f"{label} submission failed", f"status={status} body={body}")
        return None, None

    task = body["task"]
    task_id = str(task.get("id") or "")
    _assert_card_visible(task_id, capability, reporter, label)

    clarification_rounds = 0
    final_task: dict | None = None
    deadline = time.time() + TASK_TIMEOUT

    while time.time() < deadline:
        poll_status, poll_body, _ = http_request(f"{COMPASS_URL}/tasks/{task_id}", timeout=10)
        if poll_status != 200 or not isinstance(poll_body, dict):
            time.sleep(POLL_INTERVAL)
            continue
        current_task = poll_body.get("task") if isinstance(poll_body.get("task"), dict) else None
        if not current_task:
            time.sleep(POLL_INTERVAL)
            continue

        state = str((current_task.get("status") or {}).get("state") or "")
        if state in TERMINAL_STATES:
            final_task = current_task
            break
        if state == "TASK_STATE_INPUT_REQUIRED":
            clarification_rounds += 1
            if clarification_rounds == 1:
                reporter.ok(f"{label} entered the Compass clarification flow")
            question = str(
                (((current_task.get("status") or {}).get("message") or {}).get("parts") or [{}])[0].get("text") or ""
            )
            resumed = _reply_to_input_required(
                task_id, question, target_path, reporter, label, output_mode=output_mode
            )
            if not resumed:
                return None, None
            time.sleep(2)
            continue
        time.sleep(POLL_INTERVAL)

    if not final_task:
        reporter.fail(f"{label} timed out after {TASK_TIMEOUT}s")
        return None, None

    final_state = str((final_task.get("status") or {}).get("state") or "")
    if final_state == "TASK_STATE_COMPLETED":
        reporter.ok(f"{label} completed through Compass")
    else:
        reporter.fail(
            f"{label} ended in {final_state}",
            json.dumps((final_task.get("status") or {}).get("message") or {}, ensure_ascii=False)[:400],
        )
        return None, None

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
        reporter.fail(f"{label} workspace missing on host", f"container={workspace_container} host={workspace_host}")
        return None, None
    return final_task, workspace_host


def test_csv_analysis(reporter: Reporter) -> None:
    reporter.section("T1 — CSV Analysis Through Compass")
    csv_path = (PROJECT_ROOT / "tests" / "data" / "csv" / "sales_data.csv").resolve()
    expected_top_rep = _top_sales_rep(csv_path)
    instruction = f"Analyze {csv_path} and find the sales rep with the highest total sales."
    _, workspace_host = _run_office_task(
        instruction,
        "office.data.analyze",
        reporter,
        "CSV analysis",
        target_path=csv_path,
        output_mode="workspace",
    )
    if workspace_host is None:
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
    _assert_expected_runtime(workspace_host, reporter, "CSV analysis")


def test_pdf_summary(reporter: Reporter) -> None:
    reporter.section("T2 — Document Summary Through Compass (mixed file types)")
    data_dir = (PROJECT_ROOT / "tests" / "data" / "stlouis").resolve()
    instruction = (
        f"Summarize all documents in {data_dir} — including PDF, DOCX, and text files — "
        "and generate a comprehensive report covering the key topics, dates, and events mentioned."
    )
    _, workspace_host = _run_office_task(
        instruction,
        "office.document.summarize",
        reporter,
        "Document summary",
        target_path=data_dir,
        output_mode="workspace",
    )
    if workspace_host is None:
        return

    # Accept either summary.md or analysis.md — the LLM chooses the name
    summary_path = workspace_host / "office-agent" / "summary.md"
    if not summary_path.is_file():
        summary_path = workspace_host / "office-agent" / "analysis.md"
    try:
        summary_text = summary_path.read_text(encoding="utf-8")
    except OSError as exc:
        reporter.fail("Document summary report was not written to artifacts/workspaces", str(exc))
        return
    reporter.ok("Document summary report exists under artifacts/workspaces")
    # Markers expected from the actual PDF files in tests/data/stlouis
    # (filenames include decembre-2025, fevrier-2026, janvier-2026, octobre-2025)
    markers = [
        "janvier", "january",
        "fevrier", "february",
        "octobre", "october",
        "decembre", "december",
        "2025", "2026",
        "parents",
    ]
    normalized = summary_text.lower()
    matched = [m for m in markers if m in normalized]
    if matched:
        reporter.ok(f"Document summary reflects month/event context from fixture files ({', '.join(matched[:4])})")
    else:
        reporter.fail("Document summary did not include expected notice context", summary_text[:300])
    _assert_expected_runtime(workspace_host, reporter, "Document summary")


def test_essay_organize(reporter: Reporter) -> None:
    reporter.section("T3 — Essay Organize by Student Name Through Compass")
    essays_dir = (PROJECT_ROOT / "tests" / "data" / "2026").resolve()
    instruction = (
        f"Organize the essays in {essays_dir} by student name. "
        "Group each student's essays into a separate folder. "
        "Save the organized output to workspace (do not modify the source folder)."
    )
    _, workspace_host = _run_office_task(
        instruction,
        "office.folder.organize",
        reporter,
        "Essay organize",
        target_path=essays_dir,
        output_mode="workspace",
    )
    if workspace_host is None:
        return

    agent_dir = workspace_host / "office-agent"
    # The LLM-driven agent writes organized files to organized/ and a report
    organized_root = agent_dir / "organized"
    report_path = agent_dir / "organization-report.md"
    plan_path = agent_dir / "organization-plan.json"

    if organized_root.is_dir():
        reporter.ok("Essay organize created organized/ output under artifacts/workspaces")
    else:
        reporter.fail("Essay organize missing organized/ directory in workspace", str(organized_root))
        return

    # Check for student-name subdirectories in the organized output
    known_students = {"Ethan", "Yan", "Alice", "Charlie", "Student_Ethan", "Student_Yan", "Student_Alice", "Student_Charlie"}
    subdirs = {p.name for p in organized_root.iterdir() if p.is_dir()}
    student_dirs = subdirs & known_students
    if student_dirs:
        reporter.ok(f"Essay organize created per-student directories: {', '.join(sorted(student_dirs))}")
    else:
        # Tolerate other naming: check if any files exist with student names in paths
        all_files = list(organized_root.rglob("*"))
        has_student_files = any(
            re.search(r"\b(Ethan|Yan|Alice|Charlie)\b", str(f.relative_to(organized_root)), re.IGNORECASE)
            for f in all_files if f.is_file()
        )
        if has_student_files:
            reporter.ok("Essay organize produced student-named output paths")
        else:
            reporter.fail(
                "Essay organize did not produce per-student directories",
                f"subdirs found: {sorted(subdirs)[:10]}",
            )

    # Verify the organization plan and report were written
    if plan_path.is_file():
        reporter.ok("Essay organize wrote organization-plan.json")
        plan = _read_json(plan_path)
        groups = plan.get("groups") if isinstance(plan.get("groups"), list) else []
        if groups:
            reporter.ok(f"Organization plan has {len(groups)} group(s)")
        else:
            reporter.fail("Organization plan has no groups", str(plan_path))
    else:
        reporter.fail("Essay organize missing organization-plan.json", str(plan_path))

    if report_path.is_file():
        reporter.ok("Essay organize wrote organization-report.md")
    else:
        reporter.fail("Essay organize missing organization-report.md", str(report_path))

    # Source directory must NOT be modified (workspace/read-only mode)
    if not (essays_dir / "organized").exists():
        reporter.ok("Essay organize did not modify the source directory (workspace mode)")
    else:
        reporter.fail("Essay organize modified the source directory in workspace mode")

    _assert_expected_runtime(workspace_host, reporter, "Essay organize")


def test_csv_analysis_inplace(reporter: Reporter) -> None:
    reporter.section("T4 — CSV Analysis In-Place Through Compass")
    target_dir = _prepare_rw_fixture_dir("csv")
    csv_path = target_dir / "sales_data.csv"
    expected_top_rep = _top_sales_rep(csv_path)
    instruction = f"Analyze {csv_path} and write the final report back into the same folder."
    _, workspace_host = _run_office_task(
        instruction,
        "office.data.analyze",
        reporter,
        "CSV analysis in-place",
        target_path=csv_path,
        output_mode="inplace",
    )
    if workspace_host is None:
        return

    report_path = target_dir / "analysis.md"
    if report_path.is_file():
        reporter.ok("CSV in-place wrote analysis.md into tests/data/csv_rw")
    else:
        reporter.fail("CSV in-place did not write analysis.md into the user folder", str(report_path))
        return
    report_text = report_path.read_text(encoding="utf-8")
    if expected_top_rep.lower() in report_text.lower():
        reporter.ok(f"CSV in-place analysis names the top sales rep ({expected_top_rep})")
    else:
        reporter.fail("CSV in-place analysis did not mention the expected top rep", report_text[:300])
    if not (workspace_host / "office-agent" / "analysis.md").exists():
        reporter.ok("CSV in-place kept the final report out of the workspace")
    else:
        reporter.fail("CSV in-place still wrote the final report into the workspace")
    if (workspace_host / "office-agent" / "command-log.txt").is_file() and (workspace_host / "office-agent" / "stage-summary.json").is_file():
        reporter.ok("CSV in-place kept audit files in the workspace")
    else:
        reporter.fail("CSV in-place is missing workspace audit files")
    if not (target_dir / "command-log.txt").exists() and not (target_dir / "stage-summary.json").exists():
        reporter.ok("CSV in-place did not leak agent audit files into the user folder")
    else:
        reporter.fail("CSV in-place leaked audit files into the user folder")
    _assert_expected_runtime(workspace_host, reporter, "CSV analysis in-place")


def test_pdf_summary_inplace(reporter: Reporter) -> None:
    reporter.section("T5 — Document Summary In-Place Through Compass (mixed file types)")
    target_dir = _prepare_rw_fixture_dir("stlouis")
    instruction = (
        f"Summarize all documents in {target_dir} (PDF, DOCX, and text files) "
        "and write the final summary report directly back into that folder."
    )
    _, workspace_host = _run_office_task(
        instruction,
        "office.document.summarize",
        reporter,
        "Document summary in-place",
        target_path=target_dir,
        output_mode="inplace",
    )
    if workspace_host is None:
        return

    # Accept summary.md or analysis.md in the user folder
    summary_path = target_dir / "summary.md"
    if not summary_path.is_file():
        summary_path = target_dir / "analysis.md"
    if summary_path.is_file():
        reporter.ok(f"Document in-place wrote {summary_path.name} into tests/data/stlouis_rw")
    else:
        reporter.fail("Document in-place did not write summary/analysis file into the user folder", str(target_dir))
        return
    summary_text = summary_path.read_text(encoding="utf-8")
    markers = [
        "janvier", "january",
        "fevrier", "february",
        "octobre", "october",
        "decembre", "december",
        "2025", "2026",
        "parents",
    ]
    matched = [m for m in markers if m in summary_text.lower()]
    if matched:
        reporter.ok(f"Document in-place summary reflects month/event context ({', '.join(matched[:4])})")
    else:
        reporter.fail("Document in-place summary did not include expected notice context", summary_text[:300])
    # Audit files must stay in workspace, not leak into user folder
    if (workspace_host / "office-agent" / "command-log.txt").is_file() and \
            (workspace_host / "office-agent" / "stage-summary.json").is_file():
        reporter.ok("Document in-place kept audit files in the workspace")
    else:
        reporter.fail("Document in-place is missing workspace audit files")
    if not (target_dir / "command-log.txt").exists() and not (target_dir / "stage-summary.json").exists():
        reporter.ok("Document in-place did not leak agent audit files into the user folder")
    else:
        reporter.fail("Document in-place leaked audit files into the user folder")
    _assert_expected_runtime(workspace_host, reporter, "Document summary in-place")


def test_essay_organize_inplace(reporter: Reporter) -> None:
    reporter.section("T6 — Essay Organize by Student Name In-Place Through Compass")
    target_dir = _prepare_rw_fixture_dir("2026")
    instruction = (
        f"Organize the essays in {target_dir} by student name. "
        "Group each student's essays into a separate folder within the source directory. "
        "Modify the source folder directly (in-place)."
    )
    _, workspace_host = _run_office_task(
        instruction,
        "office.folder.organize",
        reporter,
        "Essay organize in-place",
        target_path=target_dir,
        output_mode="inplace",
    )
    if workspace_host is None:
        return

    # The agent should create student-name subdirectories inside the source folder
    known_students = {"Ethan", "Yan", "Alice", "Charlie", "Student_Ethan", "Student_Yan", "Student_Alice", "Student_Charlie"}
    subdirs = {p.name for p in target_dir.iterdir() if p.is_dir()}
    student_dirs = subdirs & known_students
    if student_dirs:
        reporter.ok(f"Essay organize in-place created student directories in source: {', '.join(sorted(student_dirs))}")
    else:
        # Tolerate files organized into subdirs with student names in paths
        all_files = list(target_dir.rglob("*.txt"))
        has_student_dirs = any(
            re.search(r"\b(Ethan|Yan|Alice|Charlie)\b", str(f.relative_to(target_dir)), re.IGNORECASE)
            for f in all_files
        )
        if has_student_dirs:
            reporter.ok("Essay organize in-place produced student-named paths in source folder")
        else:
            reporter.fail(
                "Essay organize in-place did not create per-student directories in source folder",
                f"subdirs found: {sorted(subdirs)[:10]}",
            )

    # Report must be in the workspace (not leaked into the user folder)
    report_path = workspace_host / "office-agent" / "organization-report.md"
    if report_path.is_file():
        reporter.ok("Essay organize in-place wrote organization-report.md in workspace")
    else:
        reporter.fail("Essay organize in-place missing organization-report.md in workspace", str(report_path))

    # Audit files must stay in workspace
    if (workspace_host / "office-agent" / "command-log.txt").is_file() and \
            (workspace_host / "office-agent" / "stage-summary.json").is_file():
        reporter.ok("Essay organize in-place kept audit files in the workspace")
    else:
        reporter.fail("Essay organize in-place is missing workspace audit files")
    if not (target_dir / "command-log.txt").exists() and not (target_dir / "stage-summary.json").exists():
        reporter.ok("Essay organize in-place did not leak agent audit files into the user folder")
    else:
        reporter.fail("Essay organize in-place leaked audit files into the user folder")
    _assert_expected_runtime(workspace_host, reporter, "Essay organize in-place")


def main() -> int:
    args = _parse_args()
    reporter = Reporter(verbose=args.verbose)

    if not _ensure_stack(reporter, reuse_images=args.reuse_images):
        return summary_exit_code(reporter)

    all_tests = [
        test_csv_analysis,
        test_pdf_summary,
        test_essay_organize,
        test_csv_analysis_inplace,
        test_pdf_summary_inplace,
        test_essay_organize_inplace,
    ]
    if args.test:
        selected = {name.strip() for name in args.test.split(",")}
        all_tests = [t for t in all_tests if t.__name__ in selected]
        if not all_tests:
            print(f"No matching tests for: {args.test}")
            return 1

    for test_fn in all_tests:
        test_fn(reporter)
    return summary_exit_code(reporter)


if __name__ == "__main__":
    raise SystemExit(main())