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
from agents.office.nodes import _expected_output_paths


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


def test_expected_output_paths_for_file_analyze_still_works(tmp_path):
    source_file = tmp_path / "sales.csv"
    source_file.write_text("a,b\n1,2\n", encoding="utf-8")
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    expected = _expected_output_paths("analyze", [str(source_file)], "workspace", str(artifacts_dir))

    assert expected == [str(artifacts_dir / "sales.csv.analysis.md")]


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
