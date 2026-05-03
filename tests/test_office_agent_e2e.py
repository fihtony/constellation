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


def _prepare_rw_fixture_dir(source_name: str) -> Path:
    source_dir = (PROJECT_ROOT / "tests" / "data" / source_name).resolve()
    target_dir = (PROJECT_ROOT / "tests" / "data" / f"{source_name}_rw").resolve()
    if target_dir.exists():
        shutil.rmtree(target_dir)
    shutil.copytree(source_dir, target_dir)
    return target_dir


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
    if runtime != "connect-agent":
        reporter.fail("common/.env does not set AGENT_RUNTIME=connect-agent", f"current={runtime!r}")
        return False
    if runtime == "copilot-cli" and not token:
        reporter.fail("COPILOT_GITHUB_TOKEN is not configured in common/.env or tests/.env")
        return False
    reporter.ok("Runtime prerequisites are configured")

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
    lowered = question.lower()
    if "absolute path" in lowered:
        if not target_path:
            reporter.fail(f"{label} requested an absolute path but the test has no target path to provide")
            return None
        reply_text = str(target_path)
    elif (
        "choose where" in lowered
        or "workspace only" in lowered
        or "write its output" in lowered
        or "choose workspace or in-place output" in lowered
        or "in-place output" in lowered
    ):
        reply_text = "Modify the original folder directly." if output_mode == "inplace" else "Use workspace output."
    elif "approve write access" in lowered:
        reply_text = "Yes. Approve write access." if output_mode == "inplace" else "No. Use workspace output instead."
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
    status, body, _ = _send_compass_message(instruction, requested_capability=capability)
    if status != 200 or not isinstance(body, dict) or not isinstance(body.get("task"), dict):
        reporter.fail(f"{label} submission failed", f"status={status} body={body}")
        return None, None

    task = body["task"]
    task_id = str(task.get("id") or "")
    _assert_card_visible(task_id, capability, reporter, label)

    clarification_rounds = 0
    state = str((task.get("status") or {}).get("state") or "")
    while state == "TASK_STATE_INPUT_REQUIRED":
        clarification_rounds += 1
        if clarification_rounds == 1:
            reporter.ok(f"{label} entered the Compass clarification flow")
        question = str((((task.get("status") or {}).get("message") or {}).get("parts") or [{}])[0].get("text") or "")
        resumed = _reply_to_input_required(
            task_id,
            question,
            target_path,
            reporter,
            label,
            output_mode=output_mode,
        )
        if not resumed:
            return None, None
        task = resumed
        state = str((task.get("status") or {}).get("state") or "")

    final_task = _wait_for_task(task_id)
    if not final_task:
        reporter.fail(f"{label} timed out")
        return None, None
    final_state = str((final_task.get("status") or {}).get("state") or "")
    if final_state == "TASK_STATE_COMPLETED":
        reporter.ok(f"{label} completed through Compass")
    else:
        reporter.fail(f"{label} ended in {final_state}", json.dumps((final_task.get("status") or {}).get("message") or {}, ensure_ascii=False)[:400])
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
        reporter.fail(f"{label} workspace is missing on the host", f"container={workspace_container} host={workspace_host}")
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
    _assert_copilot_cli_runtime(workspace_host, reporter, "CSV analysis")


def test_pdf_summary(reporter: Reporter) -> None:
    reporter.section("T2 — PDF Summary Through Compass")
    pdf_dir = (PROJECT_ROOT / "tests" / "data" / "stlouis").resolve()
    instruction = f"Summarize the PDF files in {pdf_dir} and extract a short timeline of the months or events they mention."
    _, workspace_host = _run_office_task(
        instruction,
        "office.folder.summarize",
        reporter,
        "PDF summary",
        target_path=pdf_dir,
        output_mode="workspace",
    )
    if workspace_host is None:
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

    output_root = workspace_host / "office-agent" / "organized-output"
    files_root = output_root / "files"
    manifest_path = output_root / ".office-agent-manifest.json"
    if output_root.is_dir():
        reporter.ok("Essay organize output exists under artifacts/workspaces")
    else:
        reporter.fail("Essay organize output folder is missing", str(output_root))
        return
    if files_root.is_dir():
        reporter.ok("Essay organize used the canonical files schema root")
    else:
        reporter.fail("Essay organize is missing the canonical files schema root", str(files_root))
        return
    if not (output_root / "originals").exists():
        reporter.ok("Essay organize did not duplicate the original tree into workspace")
    else:
        reporter.fail("Essay organize still duplicated the original tree into workspace", str(output_root / "originals"))

    generated_files = [
        path for path in sorted(files_root.rglob("*.txt"))
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
        re.search(r"^files/(?:.+/)?(?:19|20)\d{2}/\d{4}/", rel)
        or re.search(r"(?:19|20)\d{2}-\d{2}-\d{2}", rel)
        or re.search(r"^files/.+/\d{4}\.txt$", rel)
        or re.search(r"^files/.+/\d{4}/[^/]+\.txt$", rel)
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
    if not readme_files:
        reporter.ok("Essay organize produced no README files in this valid layout")
    elif all("\\n" not in path.read_text(encoding="utf-8") for path in readme_files):
        reporter.ok("Essay organize README files use real line breaks")
    else:
        reporter.fail("Essay organize README files still contain literal \\n sequences")

    expected_fragments = _extract_expected_txt_fragments(essays_dir)
    fragment_paths = _fragment_output_paths(manifest)
    generated_fragment_texts = {
        path.read_text(encoding="utf-8", errors="replace").strip()
        for path in generated_files
        if path.resolve() in fragment_paths
        and path.read_text(encoding="utf-8", errors="replace").strip()
    }
    unexpected = sorted(text[:120] for text in generated_fragment_texts if text not in expected_fragments)
    if not unexpected:
        reporter.ok("Essay organize output content matches source essay fragments")
    else:
        reporter.fail("Essay organize output contains content that does not match the source fragments", "\n---\n".join(unexpected[:5]))

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
    _assert_copilot_cli_runtime(workspace_host, reporter, "CSV analysis in-place")


def test_pdf_summary_inplace(reporter: Reporter) -> None:
    reporter.section("T5 — PDF Summary In-Place Through Compass")
    target_dir = _prepare_rw_fixture_dir("stlouis")
    instruction = f"Summarize the PDF files in {target_dir} and write the final summary back into that folder."
    _, workspace_host = _run_office_task(
        instruction,
        "office.folder.summarize",
        reporter,
        "PDF summary in-place",
        target_path=target_dir,
        output_mode="inplace",
    )
    if workspace_host is None:
        return

    summary_path = target_dir / "summary.md"
    if summary_path.is_file():
        reporter.ok("PDF in-place wrote summary.md into tests/data/stlouis_rw")
    else:
        reporter.fail("PDF in-place did not write summary.md into the user folder", str(summary_path))
        return
    summary_text = summary_path.read_text(encoding="utf-8")
    markers = ["janvier", "january", "fevrier", "february", "octobre", "october", "decembre", "december"]
    if any(marker in summary_text.lower() for marker in markers):
        reporter.ok("PDF in-place summary reflects month/event context from the fixture data")
    else:
        reporter.fail("PDF in-place summary did not include expected notice context", summary_text[:300])
    if not (workspace_host / "office-agent" / "summary.md").exists():
        reporter.ok("PDF in-place kept the final summary out of the workspace")
    else:
        reporter.fail("PDF in-place still wrote the final summary into the workspace")
    if (workspace_host / "office-agent" / "command-log.txt").is_file() and (workspace_host / "office-agent" / "stage-summary.json").is_file():
        reporter.ok("PDF in-place kept audit files in the workspace")
    else:
        reporter.fail("PDF in-place is missing workspace audit files")
    if not (target_dir / "command-log.txt").exists() and not (target_dir / "stage-summary.json").exists():
        reporter.ok("PDF in-place did not leak agent audit files into the user folder")
    else:
        reporter.fail("PDF in-place leaked audit files into the user folder")
    _assert_copilot_cli_runtime(workspace_host, reporter, "PDF summary in-place")


def test_essay_organize_inplace(reporter: Reporter) -> None:
    reporter.section("T6 — Essay Organize In-Place Through Compass")
    target_dir = _prepare_rw_fixture_dir("2026")
    instruction = (
        f"Read {target_dir}, organize the essays by student and date, preserve the originals, "
        "and write the final organized result back into the same folder."
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

    output_root = target_dir / "organized-output"
    files_root = output_root / "files"
    manifest_path = output_root / ".office-agent-manifest.json"
    if output_root.is_dir() and files_root.is_dir():
        reporter.ok("Essay organize in-place wrote the canonical organized-output tree into tests/data/2026_rw")
    else:
        reporter.fail("Essay organize in-place did not write the canonical organized-output tree into the user folder", str(output_root))
        return
    if not (workspace_host / "office-agent" / "organized-output").exists():
        reporter.ok("Essay organize in-place kept final organized files out of the workspace")
    else:
        reporter.fail("Essay organize in-place still wrote final organized files into the workspace")
    if manifest_path.is_file():
        reporter.ok("Essay organize in-place wrote an execution manifest into the user folder")
    else:
        reporter.fail("Essay organize in-place is missing its manifest", str(manifest_path))

    generated_files = [path for path in sorted(files_root.rglob("*.txt"))]
    if len(generated_files) >= 3:
        reporter.ok("Essay organize in-place produced grouped output files")
    else:
        reporter.fail("Essay organize in-place produced too few grouped files", f"count={len(generated_files)}")
    if not (output_root / "originals").exists():
        reporter.ok("Essay organize in-place did not duplicate the original tree")
    else:
        reporter.fail("Essay organize in-place still duplicated the original tree", str(output_root / "originals"))

    readme_files = [path for path in sorted(output_root.rglob("README.*")) if path.is_file()]
    if not readme_files:
        reporter.ok("Essay organize in-place produced no README files in this valid layout")
    elif all("\\n" not in path.read_text(encoding="utf-8") for path in readme_files):
        reporter.ok("Essay organize in-place README files use real line breaks")
    else:
        reporter.fail("Essay organize in-place README files still contain literal \\n sequences")

    manifest = _read_json(manifest_path)
    expected_fragments = _extract_expected_txt_fragments(target_dir)
    fragment_paths = _fragment_output_paths(manifest)
    generated_fragment_texts = {
        path.read_text(encoding="utf-8", errors="replace").strip()
        for path in generated_files
        if path.resolve() in fragment_paths
        and path.read_text(encoding="utf-8", errors="replace").strip()
    }
    unexpected = sorted(text[:120] for text in generated_fragment_texts if text not in expected_fragments)
    if not unexpected:
        reporter.ok("Essay organize in-place output content matches source essay fragments")
    else:
        reporter.fail("Essay organize in-place output contains content that does not match the source fragments", "\n---\n".join(unexpected[:5]))

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
        reporter.ok("Essay organize in-place preserved distinct essay content across Ethan's dated files")
    else:
        reporter.fail(
            "Essay organize in-place still repeats content across Ethan's grouped files",
            "\n".join(str(path.relative_to(output_root)) for path in ethan_files[:10]),
        )
    if (workspace_host / "office-agent" / "command-log.txt").is_file() and (workspace_host / "office-agent" / "stage-summary.json").is_file():
        reporter.ok("Essay organize in-place kept audit files in the workspace")
    else:
        reporter.fail("Essay organize in-place is missing workspace audit files")
    if not (target_dir / "command-log.txt").exists() and not (target_dir / "stage-summary.json").exists():
        reporter.ok("Essay organize in-place did not leak agent audit files into the user folder")
    else:
        reporter.fail("Essay organize in-place leaked audit files into the user folder")
    _assert_copilot_cli_runtime(workspace_host, reporter, "Essay organize in-place")


def main() -> int:
    args = _parse_args()
    reporter = Reporter(verbose=args.verbose)

    if not _ensure_stack(reporter):
        return summary_exit_code(reporter)

    test_csv_analysis(reporter)
    test_pdf_summary(reporter)
    test_essay_organize(reporter)
    test_csv_analysis_inplace(reporter)
    test_pdf_summary_inplace(reporter)
    test_essay_organize_inplace(reporter)
    return summary_exit_code(reporter)


if __name__ == "__main__":
    raise SystemExit(main())