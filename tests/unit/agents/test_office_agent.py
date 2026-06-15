"""Unit tests for Office Agent."""

import pytest
import os
import tempfile
import json
from unittest.mock import MagicMock
from pathlib import Path

from framework.agent import AgentServices
from framework.task_store import InMemoryTaskStore


def _agent_services(runtime=None):
    return AgentServices(
        session_service=MagicMock(),
        event_store=MagicMock(),
        memory_service=MagicMock(),
        skills_registry=MagicMock(),
        plugin_manager=MagicMock(),
        checkpoint_service=MagicMock(),
        runtime=runtime or MagicMock(),
        registry_client=None,
        task_store=InMemoryTaskStore(),
    )


class _RuntimeStub:
    def __init__(self, *, single_shot_response: str = ""):
        self.single_shot_response = single_shot_response or "# Summary\n\nEnglish summary.\n"
        self.run_calls: list[dict] = []
        self.run_agentic_calls: list[dict] = []

    def run(self, prompt: str, **kwargs):
        self.run_calls.append({"prompt": prompt, **kwargs})
        return {
            "summary": self.single_shot_response[:500],
            "raw_response": self.single_shot_response,
            "warnings": [],
            "artifacts": [],
        }

    def run_agentic(self, *args, **kwargs):
        self.run_agentic_calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("run_agentic should not be called for bounded folder workflows")


def test_office_agent_class_exists():
    """Verify office agent can be imported and has correct structure."""
    from agents.office.agent import OfficeAgent, office_definition, office_workflow

    assert office_definition.agent_id == "office"
    assert office_definition.runtime_capabilities["run"] is True
    assert office_definition.runtime_capabilities["run_agentic"] is True
    assert office_definition.runtime_capabilities["agentic_tools"] is True
    assert office_workflow.name == "office"
    # Nodes are extracted from edges; edge format is (source, node_func, dest) or (source, node_func)
    # START/END are string sentinels, node_func is edge[1], dest (if callable) is edge[2]
    edge_nodes = []
    for edge in office_workflow.edges:
        edge_nodes.append(edge[1])  # node function
        if len(edge) == 3 and callable(edge[2]):
            edge_nodes.append(edge[2])  # destination node (not START/END)
    node_names = [n.__name__ for n in edge_nodes]
    assert "receive_task" in node_names
    assert "analyze_request" in node_names
    assert "execute_office_work" in node_names
    assert "report_result" in node_names


@pytest.mark.asyncio
async def test_office_agent_fails_closed_without_execution_contract():
    from agents.office.agent import OfficeAgent, office_definition

    agent = OfficeAgent(definition=office_definition, services=_agent_services())
    result = await agent.handle_message({"message": {"parts": [{"text": "summarize a file"}], "metadata": {}}})

    assert result["task"]["status"]["state"] == "TASK_STATE_FAILED"
    assert "Missing executionContract" in result["task"]["status"]["message"]["parts"][0]["text"]


def test_office_tools_register():
    """Verify office tools can be registered and called."""
    from agents.office.office_tools import register_office_tools
    from framework.tools.registry import get_registry

    register_office_tools()
    registry = get_registry()
    tool_names = registry.names()
    assert "read_pdf" in tool_names
    assert "read_docx" in tool_names
    assert "read_txt" in tool_names
    assert "read_csv" in tool_names
    assert "list_directory" in tool_names
    assert "write_workspace" in tool_names
    assert "write_file" in tool_names


def test_receive_task_parses_request():
    """Test receive_task extracts capability and paths."""
    from agents.office.nodes import receive_task

    state = {
        "_task_id": "task-test123",
        "user_request": "summarize tests/data/2026/0103/1.txt",
        "source_paths": ["/tmp/tests/data/2026/0103/1.txt"],
    }
    result = receive_task(state)
    assert result["capability"] == "summarize"
    assert result["output_mode"] == "workspace"
    assert result["source_paths"] == ["/tmp/tests/data/2026/0103/1.txt"]


def test_receive_task_parses_analyze():
    """Test receive_task detects analyze capability."""
    from agents.office.nodes import receive_task

    state = {
        "_task_id": "task-test123",
        "user_request": "analyze tests/data/csv/sales_data.csv",
    }
    result = receive_task(state)
    assert result["capability"] == "analyze"


def test_receive_task_parses_inplace():
    """Test receive_task detects inplace mode."""
    from agents.office.nodes import receive_task

    state = {
        "_task_id": "task-test123",
        "user_request": "summarize tests/data/2026/0103/1.txt inplace",
    }
    result = receive_task(state)
    assert result["output_mode"] == "inplace"


def test_analyze_request_validates_paths():
    """Test analyze_request validates paths against OFFICE_SOURCE_ROOT."""
    from agents.office.nodes import analyze_request
    from framework.devlog import AgentLogger

    with tempfile.TemporaryDirectory() as tmp:
        # Set OFFICE_SOURCE_ROOT to our temp dir
        os.environ["OFFICE_SOURCE_ROOT"] = tmp
        os.environ["ARTIFACT_ROOT"] = tmp

        # Create a test file
        test_file = os.path.join(tmp, "test.txt")
        with open(test_file, "w") as f:
            f.write("hello world")

        state = {
            "_task_id": "task-test123",
            "_compass_task_id": "compass-test",
            "_task_logger": AgentLogger(task_id="compass-test", agent_name="office"),
            "source_paths": [test_file],
            "capability": "summarize",
            "output_mode": "workspace",
        }
        result = analyze_request(state)
        assert "validated_paths" in result
        assert os.path.realpath(test_file) in [os.path.realpath(p) for p in result["validated_paths"]]
        assert "artifacts_dir" in result
        log_file = os.path.join(tmp, "compass-test", "office", "agent.log")
        assert os.path.exists(log_file)
        assert "validated office request" in open(log_file, encoding="utf-8").read()

        del os.environ["OFFICE_SOURCE_ROOT"]
        del os.environ["ARTIFACT_ROOT"]


def test_analyze_request_rejects_inplace_when_policy_disallows():
    """Unsupported inplace mode must fail closed instead of silently falling back."""
    from agents.office.nodes import analyze_request

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["OFFICE_SOURCE_ROOT"] = tmp
        os.environ["ARTIFACT_ROOT"] = tmp
        os.environ.pop("OFFICE_ALLOW_INPLACE_WRITES", None)

        test_file = os.path.join(tmp, "test.txt")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("hello world")

        state = {
            "_task_id": "task-test123",
            "_compass_task_id": "compass-test",
            "source_paths": [test_file],
            "capability": "summarize",
            "output_mode": "inplace",
        }
        result = analyze_request(state)
        assert "error" in result
        assert "workspace output" in result["error"].lower()

        del os.environ["OFFICE_SOURCE_ROOT"]
        del os.environ["ARTIFACT_ROOT"]


def test_report_result_writes_evidence():
    """Test report_result writes task-report.json."""
    from agents.office.nodes import report_result

    with tempfile.TemporaryDirectory() as tmp:
        state = {
            "_task_id": "task-test123",
            "_compass_task_id": "compass-test",
            "workspace_root": tmp,
            "capability": "summarize",
            "output_mode": "workspace",
            "summary": "Test summary content.",
            "validated_paths": ["/tmp/test.txt"],
            "success": True,
        }
        result = report_result(state)
        assert result["status"] == "completed"

        evidence_file = os.path.join(tmp, "task-report.json")
        assert os.path.exists(evidence_file), f"Evidence file not written at {evidence_file}"
        with open(evidence_file) as f:
            evidence = json.load(f)
        assert evidence["data"]["capability"] == "summarize"


def test_receive_task_parses_organize():
    """receive_task correctly identifies organize capability."""
    from agents.office.nodes import receive_task

    state = {
        "_task_id": "task-org-test",
        "_compass_task_id": "compass-org-test",
        "user_request": "organize /path/to/myfolder output_mode=workspace",
    }
    result = receive_task(state)
    assert result["capability"] == "organize", f"Expected organize, got {result['capability']}"


def test_analyze_request_allows_organize_without_files():
    """analyze_request allows organize capability without specific file paths."""
    from agents.office.nodes import receive_task, analyze_request

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["OFFICE_SOURCE_ROOT"] = tmp
        try:
            state = {
                "_task_id": "task-org-test2",
                "_compass_task_id": "compass-org-test2",
                "user_request": f"organize {tmp}/somedir",
                "output_mode": "workspace",
            }
            state = receive_task(state)
            result = analyze_request(state)
            # organize should proceed even with empty validated_paths (only folder path needed)
            # It should NOT return an error about "No valid paths"
            assert "No valid paths" not in result.get("error", ""), f"organize should not error on missing file paths: {result}"
        finally:
            del os.environ["OFFICE_SOURCE_ROOT"]


def test_execute_office_work_handles_organize_error_if_no_runtime():
    """execute_office_work returns error for organize if no runtime."""
    from agents.office.nodes import execute_office_work

    state = {
        "_runtime": None,
        "capability": "organize",
        "validated_paths": ["/tmp"],
        "artifacts_dir": "/tmp/artifacts",
        "output_mode": "workspace",
    }
    result = execute_office_work(state)
    # execute_office_work with no runtime should return an error
    assert "error" in result, f"execute_office_work with no runtime should return error, got: {result}"


def test_execute_office_work_summarize_folder_uses_bounded_single_shot_runtime(tmp_path):
    """Folder summarize should avoid long agentic loops and write bounded outputs."""
    from agents.office.nodes import execute_office_work

    source_root = tmp_path / "source"
    source_root.mkdir()
    first = source_root / "first.txt"
    second = source_root / "second.txt"
    first.write_text("First document content.", encoding="utf-8")
    second.write_text("Second document content.", encoding="utf-8")

    workspace_root = tmp_path / "workspace"
    artifacts_dir = workspace_root / "artifacts"
    artifacts_dir.mkdir(parents=True)

    runtime = _RuntimeStub(single_shot_response="# Summary\n\nEnglish-only summary.\n")
    old_source_root = os.environ.get("OFFICE_SOURCE_ROOT")
    try:
        os.environ["OFFICE_SOURCE_ROOT"] = str(source_root)
        state = {
            "_runtime": runtime,
            "capability": "summarize",
            "validated_paths": [str(first), str(second)],
            "artifacts_dir": str(artifacts_dir),
            "workspace_root": str(workspace_root),
            "output_mode": "workspace",
            "_plugin_manager": None,
        }
        result = execute_office_work(state)
    finally:
        if old_source_root is None:
            os.environ.pop("OFFICE_SOURCE_ROOT", None)
        else:
            os.environ["OFFICE_SOURCE_ROOT"] = old_source_root

    assert result["success"] is True
    assert len(runtime.run_calls) == 2
    assert runtime.run_agentic_calls == []
    assert (artifacts_dir / f"{first.name}.summary.md").exists()
    assert (artifacts_dir / f"{second.name}.summary.md").exists()
    combined = artifacts_dir / "combined-summary.md"
    assert combined.exists()
    combined_text = combined.read_text(encoding="utf-8")
    assert first.name in combined_text
    assert second.name in combined_text


def test_execute_office_work_organize_folder_uses_bounded_materialization_flow(tmp_path):
    """Folder organize should materialize outputs without an open-ended agentic loop."""
    from agents.office.nodes import execute_office_work

    source_root = tmp_path / "source"
    nested = source_root / "docs"
    nested.mkdir(parents=True)
    (nested / "alpha.txt").write_text("Alpha content", encoding="utf-8")
    (nested / "beta.txt").write_text("Beta content", encoding="utf-8")

    workspace_root = tmp_path / "workspace"
    artifacts_dir = workspace_root / "artifacts"
    artifacts_dir.mkdir(parents=True)

    runtime = _RuntimeStub()
    old_source_root = os.environ.get("OFFICE_SOURCE_ROOT")
    try:
        os.environ["OFFICE_SOURCE_ROOT"] = str(source_root)
        state = {
            "_runtime": runtime,
            "capability": "organize",
            "validated_paths": [str(source_root)],
            "artifacts_dir": str(artifacts_dir),
            "workspace_root": str(workspace_root),
            "output_mode": "workspace",
            "organize_dimension": "type",
            "_plugin_manager": None,
        }
        result = execute_office_work(state)
    finally:
        if old_source_root is None:
            os.environ.pop("OFFICE_SOURCE_ROOT", None)
        else:
            os.environ["OFFICE_SOURCE_ROOT"] = old_source_root

    organized_root = artifacts_dir / "organized-output" / "files"
    organized_files = sorted(path for path in organized_root.rglob("*") if path.is_file())

    assert result["success"] is True
    assert runtime.run_calls == []
    assert runtime.run_agentic_calls == []
    assert (artifacts_dir / "organized-output" / "files" / "organization-plan.md").exists()
    assert organized_root.is_dir()
    assert len(organized_files) == 3  # 2 source files + the plan


def test_report_result_writes_warnings_md(tmp_path):
    """Test that report_result writes warnings.md when warnings are present."""
    from agents.office.nodes import report_result

    state = {
        "_task_id": "task-test123",
        "_compass_task_id": "compass-test",
        "workspace_root": str(tmp_path),
        "capability": "summarize",
        "output_mode": "workspace",
        "summary": "Test summary",
        "validated_paths": ["/tmp/test.txt"],
        "success": True,
        "warnings": [
            "File file1.pdf could not be parsed (corrupted)",
            "File file2.doc is in legacy .doc format, needs conversion",
        ],
    }
    result = report_result(state)
    assert result["status"] == "completed"
    assert result["warnings_count"] == 2

    warnings_file = tmp_path / "warnings.md"
    assert warnings_file.exists(), "warnings.md should be created"
    content = warnings_file.read_text()
    assert "File file1.pdf could not be parsed" in content
    assert "File file2.doc is in legacy" in content


def test_receive_task_parses_folder_summarize():
    """Summarize requests should remain summarize even if the path contains 'folder'."""
    from agents.office.nodes import receive_task

    state = {
        "_task_id": "task-test",
        "user_request": "summarize /path/to/folder",
    }
    result = receive_task(state)
    assert result["capability"] == "summarize"
