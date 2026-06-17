"""Regression tests for office analyze-task expected output enforcement.

The bug: ``_expected_output_paths`` previously registered an expected
``*.analysis.md`` only when the validated source was a *file*.  For directory
inputs (e.g. ``analyze /path/to/csv``) no expected output was registered, so
delivery verification passed vacuously with zero checks.  That allowed an
office run to report ``status="completed"`` even when the LLM said "I cannot
find the file" and never wrote any output.

These tests pin the fix: directory inputs must register the same
``{basename}.analysis.md`` target the prompt asks the LLM to write, and the
compass must downgrade a status="completed" result whose summary clearly
describes a failure.
"""
from __future__ import annotations

from agents.compass.agent import (
    _office_dispatch_failed,
    _summary_indicates_office_failure,
)
from agents.office.nodes import (
    _canonicalize_workspace_root_analysis_outputs,
    _expected_output_paths,
    execute_office_work,
)


class _NoToolAgenticRuntime:
    def __init__(self, response: str):
        self.response = response
        self.run_calls: list[dict] = []
        self.run_agentic_calls: list[dict] = []

    def run(self, prompt, **kwargs):
        self.run_calls.append({"prompt": prompt, "kwargs": kwargs})
        return {"summary": self.response, "raw_response": self.response}

    def run_agentic(self, *args, **kwargs):
        self.run_agentic_calls.append({"args": args, "kwargs": kwargs})
        raise AssertionError("bounded analyze must not require agentic tools")


def test_expected_output_paths_for_directory_analyze(tmp_path):
    """A directory analyze input must register a target output for the LLM to write."""
    source_dir = tmp_path / "csv"
    source_dir.mkdir()
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    expected = _expected_output_paths("analyze", [str(source_dir)], "workspace", str(artifacts_dir))

    assert expected == [str(artifacts_dir / "csv.analysis.md")]


def test_expected_output_paths_for_inplace_directory_analyze(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()

    expected = _expected_output_paths("analyze", [str(source_dir)], "inplace", "/tmp/artifacts")

    # In inplace mode, the analysis should land next to the source directory.
    assert expected == [str(source_dir / "data.analysis.md")]


def test_execute_office_work_analyze_directory_uses_bounded_single_shot_runtime(tmp_path):
    """Analyze should have a tool-free bounded path for backends without tool support."""
    source_dir = tmp_path / "sales"
    source_dir.mkdir()
    (source_dir / "q1.csv").write_text("region,revenue\nEast,10\nWest,20\n", encoding="utf-8")
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    runtime = _NoToolAgenticRuntime(
        "# Data Analysis: sales\n\n"
        "## File Overview\n- 1 CSV file\n\n"
        "## Summary Statistics\n| field | count |\n| --- | --- |\n| revenue | 2 |\n"
    )

    result = execute_office_work(
        {
            "_runtime": runtime,
            "capability": "analyze",
            "validated_paths": [str(source_dir)],
            "output_mode": "inplace",
            "artifacts_dir": str(artifacts_dir),
            "workspace_root": str(artifacts_dir),
            "user_request": "analyze the sales data and create report",
        }
    )

    output_path = source_dir / "sales.analysis.md"
    assert result["status"] == "completed"
    assert result["success"] is True
    assert output_path.exists()
    assert "Data Analysis" in output_path.read_text(encoding="utf-8")
    assert len(runtime.run_calls) == 1
    assert runtime.run_agentic_calls == []


def test_expected_output_paths_for_file_analyze_still_works(tmp_path):
    source_file = tmp_path / "sales.csv"
    source_file.write_text("a,b\n1,2\n", encoding="utf-8")
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    expected = _expected_output_paths("analyze", [str(source_file)], "workspace", str(artifacts_dir))

    assert expected == [str(artifacts_dir / "sales.csv.analysis.md")]


def test_canonicalize_workspace_root_analysis_outputs_moves_stray_file(tmp_path):
    """Copilot CLI may create analysis files in the office workspace root.

    Delivery verification expects canonical deliverables under
    ``office/artifacts``. The repair must move only the same expected filename
    from the workspace root into the canonical artifacts directory.
    """
    workspace_root = tmp_path / "office"
    artifacts_dir = workspace_root / "artifacts"
    artifacts_dir.mkdir(parents=True)
    stray = workspace_root / "sales.csv.analysis.md"
    stray.write_text("# Data Analysis: sales.csv\n", encoding="utf-8")
    expected = artifacts_dir / "sales.csv.analysis.md"

    repaired = _canonicalize_workspace_root_analysis_outputs(
        [str(expected)],
        str(artifacts_dir),
    )

    assert repaired == [str(expected)]
    assert expected.read_text(encoding="utf-8") == "# Data Analysis: sales.csv\n"
    assert not stray.exists()


def test_expected_output_paths_for_missing_file_analyze(tmp_path):
    """A path that does not exist on disk must still register an expected output.

    Without this, a typo in the user-supplied path (``sales_data.cs`` instead
    of ``sales_data.csv``) would make ``expected_outputs`` empty and let the
    LLM's "file not found" reply slip through delivery verification as
    ``success: True``.
    """
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()
    missing_file = tmp_path / "sales_data.cs"  # does not exist

    expected = _expected_output_paths("analyze", [str(missing_file)], "workspace", str(artifacts_dir))

    assert expected == [str(artifacts_dir / "sales_data.cs.analysis.md")]


def test_summary_indicates_office_failure_for_cannot_be_found():
    assert _summary_indicates_office_failure(
        "The source file `/app/userdata` cannot be found or accessed."
    )
    assert _summary_indicates_office_failure(
        "Error encountered: The path does not exist."
    )
    assert _summary_indicates_office_failure(
        "Required action: please provide a valid source path."
    )


def test_summary_does_not_flag_real_success():
    assert not _summary_indicates_office_failure(
        "Analysis complete. Written to /app/artifacts/.../csv.analysis.md."
    )
    assert not _summary_indicates_office_failure("")
    assert not _summary_indicates_office_failure(
        "Summarized 3 documents and wrote summaries.md."
    )


def test_office_dispatch_failed_treats_error_summary_as_failure():
    """Even when the office agent claims status=completed, an error summary
    must surface as a failure so the orchestrator marks the task accordingly."""
    dispatch = {
        "status": "completed",
        "summary": (
            "The source file `/app/userdata` cannot be found or accessed.\n"
            "Error encountered: The path `/app/userdata` does not exist."
        ),
    }
    assert _office_dispatch_failed(dispatch) is True
