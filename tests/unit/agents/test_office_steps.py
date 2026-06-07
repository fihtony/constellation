"""Unit tests for proposal-aligned Office major-step helpers."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from agents.office import office_steps


class _Store:
    def __init__(self) -> None:
        self._tasks: dict[str, dict] = {"task-x": {"metadata": {}}}

    def get_task(self, task_id: str):
        task = self._tasks.get(task_id)
        if task is None:
            return None
        wrapped = MagicMock()
        wrapped.metadata = task["metadata"]
        return wrapped

    def update_metadata(self, task_id: str, delta: dict) -> None:
        if task_id not in self._tasks:
            return
        self._tasks[task_id]["metadata"].update(delta)


def _state(capability: str, *, output_mode: str = "workspace") -> dict:
    return {
        "_task_id": "task-x",
        "_task_store": _Store(),
        "capability": capability,
        "output_mode": output_mode,
        "source_paths": ["/tmp/example-a", "/tmp/example-b"],
    }


def test_emit_executing_capability_uses_proposal_step_for_summarize():
    state = _state("summarize")
    office_steps.emit_executing_capability(state)

    row = state["_task_store"].get_task("task-x").metadata["major_step_rows"]["office.reading#0"]
    assert row["title"] == "Office reading documents"
    assert row["summary_template"] == "Office read {source_count} {source_kind} via MCP tools."
    assert row["summary_facts"]["source_count"] == 2


def test_emit_capability_completion_rows_marks_multifile_summary_steps_done():
    state = _state("summarize")
    office_steps.emit_capability_completion_rows(
        {
            **state,
            "validated_paths": ["/tmp/example-a.pdf", "/tmp/example-b.pdf"],
        }
    )

    rows = state["_task_store"].get_task("task-x").metadata["major_step_rows"]
    assert rows["office.reading#0"]["lifecycle_state"] == "done"
    assert rows["office.summarizing#0"]["lifecycle_state"] == "done"
    assert rows["office.combining#0"]["lifecycle_state"] == "done"


def test_emit_writing_uses_analysis_summary_template_for_analyze():
    state = _state("analyze")
    office_steps.emit_writing(state, output_count=1, lifecycle_state="done")

    row = state["_task_store"].get_task("task-x").metadata["major_step_rows"]["office.writing#0"]
    assert row["title"] == "Office writing deliverable"
    assert row["summary_template"] == "Office wrote {output_count} analysis report(s) to {output_location}."
    assert row["summary_facts"]["output_location"] == "the workspace"


def test_emit_writing_uses_moving_files_step_for_organize():
    state = _state("organize", output_mode="inplace")
    office_steps.emit_writing(state, output_count=4, file_count=9, lifecycle_state="done")

    row = state["_task_store"].get_task("task-x").metadata["major_step_rows"]["office.moving_files#0"]
    assert row["title"] == "Office moving files into organized structure"
    assert row["summary_template"] == (
        "Office placed {file_count} file(s) into their organized locations under {output_location}."
    )
    assert row["summary_facts"]["file_count"] == 9
    assert row["summary_facts"]["output_location"] == "the source folder"


def test_emit_verifying_records_done_verification_row():
    state = _state("summarize")
    office_steps.emit_verifying(state, output_count=3)

    row = state["_task_store"].get_task("task-x").metadata["major_step_rows"]["office.verifying#0"]
    assert row["lifecycle_state"] == "done"
    assert row["summary_facts"]["output_count"] == 3


def test_execute_office_work_emits_live_summary_phase_transitions(tmp_path):
    from agents.office.nodes import execute_office_work

    source_a = tmp_path / "a.txt"
    source_b = tmp_path / "b.txt"
    source_a.write_text("Alpha document", encoding="utf-8")
    source_b.write_text("Beta document", encoding="utf-8")
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    class _Runtime:
        def run(self, *args, **kwargs):
            filename = Path(kwargs.get("cwd") or "").name
            text = (
                "# Summary: file\n\n"
                "## Document Info\n"
                "- Type: TXT\n\n"
                "## Key Points\n"
                "- point\n\n"
                "## Executive Summary\n"
                "Short summary.\n"
            )
            return {"summary": text, "raw_response": text}

    state = {
        "_task_id": "task-x",
        "_task_store": _Store(),
        "_runtime": _Runtime(),
        "capability": "summarize",
        "output_mode": "workspace",
        "source_paths": [str(source_a), str(source_b)],
        "validated_paths": [str(source_a), str(source_b)],
        "artifacts_dir": str(artifacts_dir),
        "workspace_root": str(tmp_path),
    }

    result = execute_office_work(state)

    assert result["success"] is True
    events = state["_task_store"].get_task("task-x").metadata["major_step_events"]

    def _lifecycles(step_key: str) -> list[str]:
        sequence = [
            event["lifecycle_state"]
            for event in events
            if event["step_key"] == step_key
        ]
        deduped: list[str] = []
        for item in sequence:
            if not deduped or deduped[-1] != item:
                deduped.append(item)
        return deduped

    assert _lifecycles("office.reading") == ["running", "done"]
    assert _lifecycles("office.summarizing") == ["running", "done"]
    assert _lifecycles("office.combining") == ["running", "done"]
    assert _lifecycles("office.writing") == ["running", "done"]
