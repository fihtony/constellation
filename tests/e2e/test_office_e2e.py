"""Office task E2E tests: Compass → Office Agent.

Tests 3 office tasks × 2 output modes (workspace / inplace) = 6 E2E runs.

Each test:
  1. Compass receives a user request → classifies as "office"
  2. Compass dispatches via dispatch_office_task (in-process) → Office agent HTTP server
  3. Office agent receives task → executes workflow (analyze/summarize/organize)
  4. Office agent writes output to expected location
  5. Compass receives completion callback → verifies delivery

Output modes:
  - workspace: source is read-only; output goes to OFFICE_WORKSPACE_ROOT / artifacts folder
  - inplace:   source folder is R/W for Office Agent; output goes to source folder

Test data:
  - tests/data/csv/sales_data.csv        → "analyze" capability (best sales rep, totals)
  - tests/data/stlouis/                    → "summarize" capability (summarize all school documents in folder)
  - tests/data/2026/                      → "organize" capability (group student essays by date)

Run locally (no containers):
    source .venv/bin/activate
    pytest tests/e2e/test_office_e2e.py -v -s

Run a single test:
    pytest tests/e2e/test_office_e2e.py -v -s -k "csv_workspace"
"""
from __future__ import annotations

import json
import os
import shutil
import time
import unicodedata
from http.server import HTTPServer
from pathlib import Path
from threading import Thread
from typing import Generator

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
TESTS_DATA = PROJECT_ROOT / "tests" / "data"
ARTIFACTS_ROOT = PROJECT_ROOT / "artifacts" / "office-e2e"
TOOLS_DATA_CSV = TESTS_DATA / "csv"
TOOLS_DATA_STLOUIS = TESTS_DATA / "stlouis"
TOOLS_DATA_2026 = TESTS_DATA / "2026"

# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def _load_env() -> dict[str, str]:
    env_file = PROJECT_ROOT / "config" / ".env"
    env = {}
    if env_file.exists():
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                env[key.strip()] = val.strip()
    return env


_TEST_ENV = _load_env()


def _env(key: str, default: str = "") -> str:
    return _TEST_ENV.get(key, os.environ.get(key, default))


# ---------------------------------------------------------------------------
# Service factory
# ---------------------------------------------------------------------------

def _make_services(task_store=None):
    from framework.agent import AgentServices
    from framework.checkpoint import InMemoryCheckpointer
    from framework.event_store import InMemoryEventStore
    from framework.memory import InMemoryMemoryService
    from framework.plugin import PluginManager
    from framework.runtime.adapter import get_runtime
    from framework.session import InMemorySessionService
    from framework.skills import SkillsRegistry
    from framework.task_store import InMemoryTaskStore

    skills_registry = SkillsRegistry()
    skills_registry.load_all()

    model = _env("ANTHROPIC_MODEL", "MiniMax-M2.7")
    effective_runtime = get_runtime(
        "claude-code",
        model=model,
    )
    return AgentServices(
        session_service=InMemorySessionService(),
        event_store=InMemoryEventStore(),
        memory_service=InMemoryMemoryService(),
        skills_registry=skills_registry,
        plugin_manager=PluginManager(),
        checkpoint_service=InMemoryCheckpointer(),
        runtime=effective_runtime,
        registry_client=None,
        task_store=task_store or InMemoryTaskStore(),
    )


# ---------------------------------------------------------------------------
# Office agent HTTP server in a thread
# ---------------------------------------------------------------------------

class _OfficeServer:
    """In-process OfficeAgent wrapper for local E2E tests (no socket binding)."""

    def __init__(self, office_agent, port: int = 0):
        self._agent = office_agent
        self._port = port
        self._server: HTTPServer | None = None
        self._thread: Thread | None = None

    @property
    def agent(self):
        """The OfficeAgent instance (for accessing its task store)."""
        return self._agent

    @property
    def url(self) -> str:
        return "inprocess://office"

    def start(self) -> "_OfficeServer":
        from framework.a2a.server import A2ARequestHandler

        class Handler(A2ARequestHandler):
            agent = self._agent
            advertised_url = self.url
            agent_card_path = ""

        self._server = HTTPServer(("127.0.0.1", self._port), Handler)
        self._port = self._server.server_address[1]
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        # Wait until server is ready
        for _ in range(50):
            time.sleep(0.05)
            try:
                import urllib.request
                urllib.request.urlopen(f"{self.url}/health", timeout=1)
                break
            except Exception:
                pass
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def office_server() -> Generator[_OfficeServer, None, None]:
    """Start Office Agent for the test session (in-process, no HTTP binding)."""
    from agents.office.agent import OfficeAgent, office_definition

    services = _make_services()
    office_agent = OfficeAgent(office_definition, services)

    import asyncio
    async def _start():
        await office_agent.start()
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(_start())
    loop.close()

    server = _OfficeServer(office_agent)
    print(f"\n[office-server] started at {server.url}")
    yield server
    print(f"\n[office-server] stopped")


@pytest.fixture
def clean_artifacts():
    """Clean test_* folders from previous runs before each test.

    Note: Office agent output goes to {ARTIFACTS_ROOT}/{compass_task_id}/office/artifacts/
    not to this folder. This fixture just provides isolation.
    """
    ARTIFACTS_ROOT.mkdir(parents=True, exist_ok=True)
    # Clean all test_* folders from previous runs
    for item in ARTIFACTS_ROOT.iterdir():
        if item.is_dir() and item.name.startswith("test_"):
            shutil.rmtree(item, ignore_errors=True)
    yield  # Tests run here
    # Don't clean up — preserve for post-test inspection


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

# Task definitions: (test_id, description, source_path, capability, output_filename)
OFFICE_TASKS = [
    (
        "csv_analyze_workspace",
        f"Analyze the CSV file at {TOOLS_DATA_CSV}/sales_data.csv and find the best sales person and total sales amount. Output should go to workspace.",
        str(TOOLS_DATA_CSV / "sales_data.csv"),
        "analyze",
        "sales_data.csv.analysis.md",
    ),
    (
        "csv_analyze_inplace",
        f"Analyze the CSV file at {TOOLS_DATA_CSV}/sales_data.csv and find the best sales person and total sales amount. Output should be inplace.",
        str(TOOLS_DATA_CSV / "sales_data.csv"),
        "analyze",
        "sales_data.csv.analysis.md",
    ),
    (
        "stlouis_summarize_workspace",
        f"Summarize all supported documents under folder {TOOLS_DATA_STLOUIS}. Create one summary per document and a combined summary report. Output should go to workspace.",
        str(TOOLS_DATA_STLOUIS),
        "summarize",
        "combined-summary.md",
    ),
    (
        "stlouis_summarize_inplace",
        f"Summarize all supported documents under folder {TOOLS_DATA_STLOUIS}. Create one summary per document and a combined summary report. Output should be inplace.",
        str(TOOLS_DATA_STLOUIS),
        "summarize",
        "combined-summary.md",
    ),
    (
        "2026_organize_workspace",
        f"Organize the student essays in {TOOLS_DATA_2026} folder by grouping same student's essays by date. Output should go to workspace.",
        str(TOOLS_DATA_2026),
        "organize",
        "organization-plan.md",
    ),
    (
        "2026_organize_inplace",
        f"Organize the student essays in {TOOLS_DATA_2026} folder by grouping same student's essays by date. Output should be inplace.",
        str(TOOLS_DATA_2026),
        "organize",
        "organization-plan.md",
    ),
]


def _output_mode(test_id: str) -> str:
    return "inplace" if "inplace" in test_id else "workspace"


def _source_root_for(source_path: str) -> str:
    p = Path(source_path)
    if p.is_dir():
        return str(p.resolve())
    return str(p.parent.resolve())


def _expected_output_path(
    output_mode: str,
    capability: str,
    source_path: str,
    output_filename: str,
    office_workspace: str,
) -> str:
    if output_mode == "workspace":
        return os.path.join(office_workspace, output_filename)
    if capability in {"analyze", "summarize"}:
        base_dir = source_path if os.path.isdir(source_path) else os.path.dirname(source_path)
        return os.path.join(base_dir, output_filename)
    return os.path.join(source_path, output_filename)


def _summarize_source_files(source_path: str) -> list[str]:
    path = Path(source_path)
    if path.is_dir():
        return sorted(
            str(item.resolve())
            for item in path.rglob("*")
            if item.is_file() and item.suffix.lower() in {".pdf", ".docx", ".txt", ".md", ".pptx"}
        )
    return [str(path.resolve())]


def _read_text(path: str) -> str:
    if not os.path.exists(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return -1.0


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value)


# ---------------------------------------------------------------------------
# Individual tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "test_id,description,source_path,capability,output_filename",
    OFFICE_TASKS,
    ids=[case[0] for case in OFFICE_TASKS],
)
async def test_office_task(
    office_server,
    clean_artifacts,
    monkeypatch,
    test_id,
    description,
    source_path,
    capability,
    output_filename,
):
    """Run a single office task E2E test.

    Validates:
      1. Compass classifies the request as "office"
      2. Compass dispatches to Office Agent (in-process dispatch_office_task → HTTP)
      3. Office Agent processes the task
      4. Output appears in the expected location (workspace or inplace)
      5. A2A callback is sent from Office Agent to Compass
    """
    from agents.compass.agent import CompassAgent, compass_definition
    from framework.task_store import InMemoryTaskStore
    from framework.tools.base import BaseTool, ToolResult
    from framework.tools.registry import get_registry

    output_mode = _output_mode(test_id)
    source_root = _source_root_for(source_path)
    # clean_artifacts fixture just cleans up old test folders, actual output goes to compass_task_id folder

    print(f"\n{'='*70}")
    print(f"[{test_id}] START")
    print(f"  source   : {source_path}")
    print(f"  sourceRoot: {source_root}")
    print(f"  output   : {output_mode}")
    print(f"  office   : {office_server.url}")
    print(f"{'='*70}")

    # ---- Set environment for this test ----
    os.environ["OFFICE_SOURCE_ROOT"] = source_root
    os.environ["OFFICE_ALLOWED_BASE_PATHS"] = source_root
    os.environ["OFFICE_ALLOW_INPLACE_WRITES"] = "true" if output_mode == "inplace" else "false"
    os.environ["ARTIFACT_ROOT"] = str(ARTIFACTS_ROOT)
    os.environ["AGENT_RUNTIME"] = "claude-code"
    os.environ["ANTHROPIC_AUTH_TOKEN"] = _env("ANTHROPIC_AUTH_TOKEN", "")
    os.environ["ANTHROPIC_BASE_URL"] = _env("ANTHROPIC_BASE_URL", "https://api.minimaxi.com/anthropic")
    os.environ["ANTHROPIC_MODEL"] = _env("ANTHROPIC_MODEL", "MiniMax-M2.7")
    os.environ["OFFICE_AGENTIC_TIMEOUT_SECONDS"] = "180"
    os.environ["OFFICE_AGENTIC_MAX_TURNS"] = "12"

    # Ensure compass registry lookup can discover office capability in local E2E.
    from framework.registry_client import RegistryClient
    monkeypatch.setattr(
        RegistryClient,
        "discover",
        lambda self, capability_name: office_server.url if capability_name in {"office.document.summarize", "office.agent"} else "",
    )

    # ---- Register compass tools ----
    from agents.compass.tools import register_compass_tools
    register_compass_tools()

    # ---- Create Compass Agent ----
    compass_store = InMemoryTaskStore()
    compass_services = _make_services(task_store=compass_store)
    compass_agent = CompassAgent(compass_definition, compass_services)

    await compass_agent.start()

    # Override dispatch_office_task to call the office server directly
    registry = get_registry()

    class InProcessDispatchOfficeOverride(BaseTool):
        name = "dispatch_office_task"
        description = "Override dispatch_office_task to call office server directly"
        parameters_schema = {
            "type": "object",
            "properties": {
                "task_description": {"type": "string"},
                "source_paths": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["task_description"],
        }

        def execute_sync(self, task_description: str = "", source_paths: list = None) -> ToolResult:
            import asyncio
            import threading
            import uuid

            source_paths = source_paths or []

            # Get the compass task ID at execution time (task is already created by compass.handle_message)
            compass_tasks = compass_store.list_tasks(agent_id="compass")
            current_compass_task_id = compass_tasks[-1].id if compass_tasks else ""

            envelope = {
                "message": {
                    "messageId": str(uuid.uuid4()),
                    "role": "ROLE_USER",
                    "parts": [{"text": task_description}],
                    "metadata": {
                        "output_mode": output_mode,
                        "source_paths": source_paths,
                        "compassTaskId": current_compass_task_id,
                        "allowed_tools": ["read_pdf", "read_docx", "read_txt", "read_csv",
                                         "list_directory", "write_workspace", "write_file",
                                         "organize_folder", "organize_move_file"],
                    },
                },
                "configuration": {"returnImmediately": True},
            }
            try:
                response_holder: dict[str, dict] = {}
                error_holder: dict[str, str] = {}

                def _invoke_office():
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        response_holder["response"] = loop.run_until_complete(
                            office_server.agent.handle_message(envelope)
                        )
                    except Exception as exc:
                        error_holder["error"] = str(exc)
                    finally:
                        loop.close()

                t = threading.Thread(target=_invoke_office, daemon=True)
                t.start()
                t.join(timeout=60)
                if t.is_alive():
                    return ToolResult(output="", error=json.dumps({"status": "error", "message": "office handle_message timeout"}))
                if error_holder.get("error"):
                    return ToolResult(output="", error=json.dumps({"status": "error", "message": error_holder["error"]}))

                response = response_holder.get("response", {})
                task_data = response.get("task", response)
                task_id = task_data.get("id", "")
                if not task_id:
                    return ToolResult(output=json.dumps({"status": "dispatched", "taskId": ""}))

                # Poll until done
                deadline = time.time() + 600
                terminal = {
                    "TASK_STATE_COMPLETED", "TASK_STATE_FAILED",
                    "TASK_STATE_CANCELLED", "TASK_STATE_INPUT_REQUIRED",
                }
                while time.time() < deadline:
                    try:
                        result = office_server.agent.services.task_store.get_task_dict(task_id)
                        task_obj = result.get("task", result)
                        state = task_obj.get("status", {}).get("state", "")
                        if state in terminal:
                            return ToolResult(output=json.dumps({"status": "completed", "taskId": task_id}))
                    except Exception:
                        pass
                    time.sleep(2)
                return ToolResult(output=json.dumps({"status": "timeout", "taskId": task_id}))
            except Exception as exc:
                return ToolResult(output="", error=json.dumps({"status": "error", "message": str(exc)}))

    try:
        registry.unregister("dispatch_office_task")
    except KeyError:
        pass
    registry.register(InProcessDispatchOfficeOverride())

    # ---- Send task to Compass ----
    print(f"\n[{test_id}] Sending to Compass...")

    compass_result = await compass_agent.handle_message({
        "message": {
            "messageId": f"e2e-{test_id}",
            "role": "ROLE_USER",
            "parts": [{"text": description}],
            "metadata": {},
        }
    })

    compass_task_id = compass_result.get("task", {}).get("id", "")
    compass_state = compass_result.get("task", {}).get("status", {}).get("state", "")
    print(f"[{test_id}] Compass task state: {compass_state}")
    print(f"[{test_id}] Compass task ID  : {compass_task_id}")
    assert compass_task_id, f"[{test_id}] Compass task ID is empty"
    assert compass_state in {"TASK_STATE_COMPLETED", "TASK_STATE_WORKING", "TASK_STATE_SUBMITTED"}, (
        f"[{test_id}] Unexpected compass task state: {compass_state}"
    )

    # Track source output timestamp to ensure mode-specific delivery checks are real.
    source_expected_output = _expected_output_path(
        output_mode="inplace",
        capability=capability,
        source_path=source_path,
        output_filename=output_filename,
        office_workspace="",
    )
    source_output_mtime_before = _safe_mtime(source_expected_output)

    # Poll for office task completion using the same task store as the office agent
    print(f"\n[{test_id}] Waiting for Office Agent to complete...")

    office_task_store = office_server.agent.services.task_store
    deadline = time.monotonic() + 600
    last_heartbeat = time.monotonic()
    office_final = None

    while time.monotonic() < deadline:
        tasks = office_task_store.list_tasks(agent_id="office")
        for task in tasks:
            task_meta = getattr(task, "metadata", {}) or {}
            if task_meta.get("compass_task_id") != compass_task_id:
                continue
            state_val = task.status.state.value
            if state_val in {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED"}:
                office_final = office_task_store.get_task_dict(task.id)
                break
        if office_final:
            break
        now = time.monotonic()
        if now - last_heartbeat >= 30:
            last_heartbeat = now
            elapsed = int(now - (deadline - 600))
            print(f"[{test_id}] Office Agent still running... ({elapsed}s elapsed)")
        time.sleep(2)

    assert office_final is not None, f"[{test_id}] Office Agent did not complete within 600s"
    office_task_obj = office_final.get("task", office_final)
    office_state = office_task_obj.get("status", {}).get("state", "")
    print(f"[{test_id}] Office task state: {office_state}")
    assert office_state == "TASK_STATE_COMPLETED", f"[{test_id}] Office task failed: {office_final}"

    # ---- Verify output location ----
    print(f"\n[{test_id}] Verifying output...")

    # The office agent writes to: {ARTIFACT_ROOT}/{compass_task_id}/office/
    # task-report.json is in {compass_task_id}/office/
    # output file is in {compass_task_id}/office/artifacts/{filename}
    office_base = os.path.join(ARTIFACTS_ROOT, compass_task_id, "office")
    office_workspace = os.path.join(office_base, "artifacts")
    task_report_path = os.path.join(office_base, "task-report.json")

    expected_output_path = _expected_output_path(
        output_mode=output_mode,
        capability=capability,
        source_path=source_path,
        output_filename=output_filename,
        office_workspace=office_workspace,
    )
    summarize_sources = _summarize_source_files(source_path) if capability == "summarize" else []

    print(f"[{test_id}] Expected output: {expected_output_path}")
    print(f"[{test_id}] Office workspace: {office_workspace}")
    print(f"[{test_id}] Workspace contents: {list(Path(office_workspace).rglob('*')) if os.path.exists(office_workspace) else 'not found'}")

    if output_mode == "workspace":
        # List workspace contents for debugging
        workspace_contents = list(Path(office_workspace).rglob("*")) if os.path.exists(office_workspace) else []
        print(f"[{test_id}] Workspace contents: {workspace_contents}")

    # Check task-report.json exists in the office working folder
    assert os.path.exists(task_report_path), f"[{test_id}] task-report.json not found in office working folder"
    with open(task_report_path, encoding="utf-8") as f:
        report = json.load(f)
    print(f"[{test_id}] task-report.json: {json.dumps(report, indent=2)}")
    assert report.get("data", {}).get("output_mode") == output_mode, (
        f"[{test_id}] task-report output_mode mismatch"
    )
    assert report.get("data", {}).get("warnings_count") == 0, (
        f"[{test_id}] task-report warnings_count should be 0: {report}"
    )
    assert not os.path.exists(os.path.join(office_base, "warnings.md")), (
        f"[{test_id}] warnings.md should not exist for successful agentic execution"
    )

    # Check output delivery in the expected location
    assert os.path.exists(expected_output_path), (
        f"[{test_id}] Expected output missing: {expected_output_path}"
    )

    if capability == "organize":
        if output_mode == "workspace":
            organized_root = os.path.join(office_workspace, "organized-output", "files")
        else:
            organized_root = os.path.join(source_path, "organized-output", "files")
        assert os.path.isdir(organized_root), (
            f"[{test_id}] Missing organized output directory: {organized_root}"
        )
        organized_files = [
            path for path in Path(organized_root).rglob("*")
            if path.is_file()
        ]
        assert organized_files, f"[{test_id}] No files materialized under organized output"

    if capability == "summarize" and len(summarize_sources) > 1:
        for doc_path in summarize_sources:
            per_doc_output = _expected_output_path(
                output_mode=output_mode,
                capability=capability,
                source_path=doc_path,
                output_filename=f"{Path(doc_path).name}.summary.md",
                office_workspace=office_workspace,
            )
            assert os.path.exists(per_doc_output), (
                f"[{test_id}] Missing per-document summary: {per_doc_output}"
            )
        combined_text = _read_text(expected_output_path)
        for doc_path in summarize_sources:
            assert _normalize_text(Path(doc_path).name) in _normalize_text(combined_text), (
                f"[{test_id}] combined summary missing document section for {Path(doc_path).name}"
            )

    source_output_mtime_after = _safe_mtime(source_expected_output)
    if output_mode == "workspace":
        if source_output_mtime_before >= 0:
            assert source_output_mtime_after == source_output_mtime_before, (
                f"[{test_id}] Workspace mode should not modify source output path: {source_expected_output}"
            )
        print(f"[{test_id}] PASS: workspace output verified")
    else:
        assert source_output_mtime_after >= source_output_mtime_before, (
            f"[{test_id}] Inplace output was not updated in source folder: {source_expected_output}"
        )
        print(f"[{test_id}] PASS: inplace output verified")

    # Step-level log checks: compass + office logs under artifacts/{task_id}/...
    from framework.devlog import get_agent_log_path

    compass_log = get_agent_log_path(compass_task_id, "compass")
    office_log = get_agent_log_path(compass_task_id, "office")
    compass_log_text = _read_text(compass_log)
    office_log_text = _read_text(office_log)
    print(f"[{test_id}] Compass log: {compass_log}")
    print(f"[{test_id}] Office log : {office_log}")

    assert "[NODE] handle_message" in compass_log_text, f"[{test_id}] missing compass handle_message log"
    assert "task_type='office'" in compass_log_text, f"[{test_id}] compass did not classify task as office"
    assert "[A2A] → registry" in compass_log_text, f"[{test_id}] missing A2A registry query log"
    assert "[A2A] ← registry" in compass_log_text, f"[{test_id}] missing A2A registry response log"
    assert "[A2A] → office" in compass_log_text, f"[{test_id}] missing A2A dispatch-to-office log"
    assert "office dispatch complete" in compass_log_text, f"[{test_id}] missing office dispatch completion log"

    assert "[NODE] handle_message" in office_log_text, f"[{test_id}] missing office handle_message log"
    assert "[A2A] ← compass" in office_log_text, f"[{test_id}] missing office receive-from-compass log"
    assert "office agent started" in office_log_text, f"[{test_id}] missing office startup log"

    print(f"\n[{test_id}] COMPLETED SUCCESSFULLY")
    print(f"{'='*70}")

    # Clean up registry
    try:
        registry.unregister("dispatch_office_task")
    except KeyError:
        pass
