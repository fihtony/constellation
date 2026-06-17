"""Local Office task E2E tests: Compass UI/A2A -> Registry -> Office Agent.

This suite runs three workspace-mode office tasks against real local HTTP
servers. The natural-language request stays generic so agents do not depend on
test-only paths or dataset-specific clues in prompt text. Authorized source
paths and capability hints are passed via message metadata instead.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import time
import urllib.parse
import urllib.request
import unicodedata
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Generator

import pytest


PROJECT_ROOT = Path(__file__).parent.parent.parent.resolve()
TESTS_DATA = PROJECT_ROOT / "tests" / "data"
ARTIFACTS_ROOT = PROJECT_ROOT / "artifacts" / "office-e2e"
READONLY_INPUTS_ROOT = PROJECT_ROOT / "artifacts" / "office-e2e-inputs"

TOOLS_DATA_CSV = TESTS_DATA / "csv" / "sales_data.csv"
TOOLS_DATA_STLOUIS = TESTS_DATA / "stlouis"
TOOLS_DATA_2026 = TESTS_DATA / "2026"


def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for env_file in (PROJECT_ROOT / "tests" / ".env", PROJECT_ROOT / "config" / ".env"):
        if not env_file.exists():
            continue
        with open(env_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                env.setdefault(key.strip(), value.strip())
    return env


_TEST_ENV = _load_env()


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, _TEST_ENV.get(key, default))


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

    runtime = get_runtime("claude-code", model=_env("ANTHROPIC_MODEL", "MiniMax-M2.7"))
    return AgentServices(
        session_service=InMemorySessionService(),
        event_store=InMemoryEventStore(),
        memory_service=InMemoryMemoryService(),
        skills_registry=skills_registry,
        plugin_manager=PluginManager(),
        checkpoint_service=InMemoryCheckpointer(),
        runtime=runtime,
        registry_client=None,
        task_store=task_store or InMemoryTaskStore(),
    )


class _AgentServer:
    def __init__(self, agent, card_path: str = "", port: int = 0):
        self.agent = agent
        self.card_path = card_path
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "_AgentServer":
        from framework.a2a.server import A2ARequestHandler

        parent = self

        class Handler(A2ARequestHandler):
            agent = parent.agent
            advertised_url = ""
            agent_card_path = parent.card_path

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.port = self._server.server_address[1]
        Handler.advertised_url = self.url
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self._wait_for_ready()
        return self

    def _wait_for_ready(self) -> None:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{self.url}/health", timeout=1):
                    return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError(f"Server did not become ready: {self.url}")

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)


class _RegistryServer:
    def __init__(self, capability_map: dict[str, str], port: int = 0):
        self.capability_map = capability_map
        self.port = port
        self.queries: list[str] = []
        self._server: ThreadingHTTPServer | None = None
        self._thread: Thread | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "_RegistryServer":
        parent = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path == "/query":
                    capability = urllib.parse.parse_qs(parsed.query).get("capability", [""])[0]
                    parent.queries.append(capability)
                    service_url = parent.capability_map.get(capability, "")
                    body = json.dumps([{"serviceUrl": service_url}] if service_url else []).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if parsed.path.startswith("/agents/"):
                    agent_id = parsed.path.split("/")[-1]
                    service_url = parent.capability_map.get(agent_id, "")
                    payload = json.dumps({"serviceUrl": service_url}).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                self.send_response(404)
                self.end_headers()

            def log_message(self, format: str, *args) -> None:  # noqa: A003
                print(f"[registry] {format % args}")

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.port = self._server.server_address[1]
        self._thread = Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        if self._thread:
            self._thread.join(timeout=5)


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
    if task_dict.get("ui_update", {}).get("task_status"):
        return task_dict["ui_update"]["task_status"]
    return task_dict.get("task", task_dict).get("status", {}).get("state", "")


def _task_id(task_dict: dict) -> str:
    if task_dict.get("task_id"):
        return task_dict["task_id"]
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


def _set_readonly(path: Path) -> None:
    if path.is_file():
        path.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        return

    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_dir():
            child.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)
        else:
            child.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    path.chmod(stat.S_IRUSR | stat.S_IXUSR | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)


def _set_writable(path: Path) -> None:
    if not path.exists():
        return
    if path.is_file():
        path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
        return

    for child in sorted(path.rglob("*")):
        if child.is_dir():
            child.chmod(
                stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
                | stat.S_IRGRP | stat.S_IXGRP
                | stat.S_IROTH | stat.S_IXOTH
            )
        else:
            child.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    path.chmod(
        stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
        | stat.S_IRGRP | stat.S_IXGRP
        | stat.S_IROTH | stat.S_IXOTH
    )


def _stage_readonly_source(test_id: str, source_path: Path) -> Path:
    stage_root = READONLY_INPUTS_ROOT / test_id
    if stage_root.exists():
        _set_writable(stage_root)
        shutil.rmtree(stage_root)
    if source_path.is_dir():
        target = stage_root / source_path.name
        shutil.copytree(source_path, target)
    else:
        target_dir = stage_root / "source"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / source_path.name
        shutil.copy2(source_path, target)
    _set_readonly(stage_root)
    return target


def _source_root_for(source_path: Path) -> Path:
    return source_path if source_path.is_dir() else source_path.parent


def _snapshot_tree(path: Path) -> dict[str, tuple[int, int]]:
    root = path if path.is_dir() else path.parent
    snapshot: dict[str, tuple[int, int]] = {}
    if path.is_file():
        stat_result = path.stat()
        snapshot[path.name] = (stat_result.st_size, int(stat_result.st_mtime))
        return snapshot
    for item in sorted(root.rglob("*")):
        rel = str(item.relative_to(root))
        stat_result = item.stat()
        snapshot[rel] = (stat_result.st_size, int(stat_result.st_mtime))
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


@pytest.fixture(scope="session", autouse=True)
def configure_env() -> Generator[None, None, None]:
    ARTIFACTS_ROOT.mkdir(parents=True, exist_ok=True)
    READONLY_INPUTS_ROOT.mkdir(parents=True, exist_ok=True)

    os.environ["ARTIFACT_ROOT"] = str(ARTIFACTS_ROOT)
    os.environ["AGENT_RUNTIME"] = "claude-code"
    os.environ["ANTHROPIC_AUTH_TOKEN"] = _env("ANTHROPIC_AUTH_TOKEN", "")
    os.environ["ANTHROPIC_BASE_URL"] = _env("ANTHROPIC_BASE_URL", "https://api.minimaxi.com/anthropic")
    os.environ["ANTHROPIC_MODEL"] = _env("ANTHROPIC_MODEL", "MiniMax-M2.7")
    os.environ["OFFICE_AGENTIC_TIMEOUT_SECONDS"] = "300"
    os.environ["OFFICE_AGENTIC_MAX_TURNS"] = "16"
    os.environ["OFFICE_ALLOW_INPLACE_WRITES"] = "false"
    yield


@pytest.fixture(scope="session")
def office_server() -> Generator[_AgentServer, None, None]:
    from agents.office.agent import OfficeAgent, office_definition

    agent = OfficeAgent(office_definition, _make_services())
    asyncio.run(agent.start())
    server = _AgentServer(agent).start()
    print(f"\n[office-server] {server.url}")
    yield server
    server.stop()
    asyncio.run(agent.stop())


@pytest.fixture(scope="session")
def registry_server(office_server: _AgentServer) -> Generator[_RegistryServer, None, None]:
    capability_map = {
        "office.document.summarize": office_server.url,
        "office.data.analyze": office_server.url,
        "office.folder.organize": office_server.url,
        "office.agent": office_server.url,
        "office": office_server.url,
    }
    server = _RegistryServer(capability_map).start()
    print(f"\n[registry-server] {server.url}")
    yield server
    server.stop()


@pytest.fixture(scope="session")
def compass_server(registry_server: _RegistryServer) -> Generator[_AgentServer, None, None]:
    from agents.compass.agent import CompassAgent, compass_definition
    from framework.task_store import InMemoryTaskStore

    os.environ["REGISTRY_URL"] = registry_server.url

    agent = CompassAgent(compass_definition, _make_services(task_store=InMemoryTaskStore()))
    asyncio.run(agent.start())
    server = _AgentServer(agent).start()
    os.environ["COMPASS_BASE_URL"] = server.url
    print(f"\n[compass-server] {server.url}")
    print(f"[compass-ui] {server.url}/ui")
    yield server
    server.stop()
    asyncio.run(agent.stop())


@pytest.fixture(autouse=True)
def reset_registry_queries(registry_server: _RegistryServer) -> Generator[None, None, None]:
    registry_server.queries.clear()
    yield


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "test_id,description,source_path,capability,output_filename",
    OFFICE_TASKS,
    ids=[case[0] for case in OFFICE_TASKS],
)
async def test_office_task_workspace(
    compass_server: _AgentServer,
    office_server: _AgentServer,
    registry_server: _RegistryServer,
    test_id: str,
    description: str,
    source_path: Path,
    capability: str,
    output_filename: str,
):
    staged_source = _stage_readonly_source(test_id, source_path)
    source_root = _source_root_for(staged_source)
    source_snapshot_before = _snapshot_tree(source_root)

    os.environ["OFFICE_SOURCE_ROOT"] = str(source_root)
    os.environ["OFFICE_ALLOWED_BASE_PATHS"] = str(source_root)
    os.environ["OFFICE_ALLOW_INPLACE_WRITES"] = "false"

    compass_ui_url = f"{compass_server.url}/ui"
    ui_html = _http_get_text(compass_ui_url)
    assert "Compass Agent" in ui_html

    print(f"\n{'=' * 70}")
    print(f"[{test_id}] Compass UI: {compass_ui_url}")
    print(f"[{test_id}] Source root : {source_root}")
    print(f"[{test_id}] Source path : {staged_source}")
    print(f"{'=' * 70}")

    assert not os.access(source_root, os.W_OK), f"[{test_id}] source root should be read-only"

    initial_response = _http_post(
        f"{compass_server.url}/message:send",
        {
            "message": {
                "messageId": f"office-e2e-{test_id}",
                "role": "ROLE_USER",
                "parts": [{"text": description}],
                "metadata": {
                    "capability": capability,
                    "source_paths": [str(staged_source)],
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
        f"{compass_server.url}/tasks/{compass_task_id}/resume",
        {"input": "workspace"},
        timeout=600,
    )
    assert _task_id(resumed_response) == compass_task_id

    compass_final = _poll_task(compass_server.url, compass_task_id)
    assert _task_state(compass_final) == "TASK_STATE_COMPLETED", f"[{test_id}] compass failed: {compass_final}"

    office_task_store = office_server.agent.services.task_store
    office_final = None
    deadline = time.time() + 600
    while time.time() < deadline:
        tasks = office_task_store.list_tasks(agent_id="office")
        for task in tasks:
            task_meta = getattr(task, "metadata", {}) or {}
            if task_meta.get("compass_task_id") != compass_task_id:
                continue
            current = office_task_store.get_task_dict(task.id)
            state = _task_state(current)
            if state in {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED"}:
                office_final = current
                break
        if office_final is not None:
            break
        time.sleep(2)

    assert office_final is not None, f"[{test_id}] office task not found for compass task {compass_task_id}"
    assert _task_state(office_final) == "TASK_STATE_COMPLETED", f"[{test_id}] office failed: {office_final}"

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

    if capability == "summarize":
        combined_text = _read_text(expected_output_path)
        for doc_path in _summarize_source_files(staged_source):
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

    source_snapshot_after = _snapshot_tree(source_root)
    assert source_snapshot_after == source_snapshot_before, f"[{test_id}] source tree changed in workspace mode"
    assert not any(path.name == output_filename for path in source_root.rglob("*")), (
        f"[{test_id}] output escaped into read-only source root"
    )

    from framework.devlog import get_agent_log_path

    compass_log = Path(get_agent_log_path(compass_task_id, "compass"))
    office_log = Path(get_agent_log_path(compass_task_id, "office"))
    compass_log_text = _read_text(compass_log)
    office_log_text = _read_text(office_log)

    assert compass_log.exists(), f"[{test_id}] missing compass log"
    assert office_log.exists(), f"[{test_id}] missing office log"

    assert "task_type='office'" in compass_log_text, f"[{test_id}] compass did not classify as office"
    assert "office task awaiting output mode" in compass_log_text, f"[{test_id}] missing output-mode inquiry log"
    assert "[A2A] → registry" in compass_log_text, f"[{test_id}] missing registry send log"
    assert "[A2A] ← registry" in compass_log_text, f"[{test_id}] missing registry receive log"
    assert "[A2A] → office" in compass_log_text, f"[{test_id}] missing office dispatch log"
    assert "office delivery verified" in compass_log_text, f"[{test_id}] missing office delivery verification log"
    assert "[A2A] ← callback" in compass_log_text, f"[{test_id}] missing office callback log"

    assert "[NODE] handle_message" in office_log_text, f"[{test_id}] missing office handle_message log"
    assert "office agent started" in office_log_text, f"[{test_id}] missing office startup log"
    assert "[A2A] ← compass" in office_log_text, f"[{test_id}] missing office receive-from-compass log"

    assert any(query in registry_server.queries for query in {"office.document.summarize", "office.data.analyze", "office.folder.organize"}), (
        f"[{test_id}] registry was not queried for office capability"
    )

    print(f"[{test_id}] Compass log : {compass_log}")
    print(f"[{test_id}] Office log  : {office_log}")
    print(f"[{test_id}] Workspace   : {office_workspace}")
    print(f"[{test_id}] Output      : {expected_output_path}")
