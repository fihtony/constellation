"""Tests for the Office plan-output gate orchestrator (_run_plan_output_gate).

Task 8: ties the framework gate, retry prompt, plan integrity snapshot, audit
log, and major-step emitters together. The orchestrator is exercised here in
isolation; the integration with ``execute_office_work`` is covered in Task 9.
"""
from __future__ import annotations

import json
import os
from typing import Any

import pytest

from agents.office import office_steps
from agents.office.nodes import _run_plan_output_gate
from framework.office.plan_output_gate import (
    GateReport,
    OutputContract,
    resolve_output_contract,
)
from framework.runtime.adapter import AgenticResult


class _StubRuntime:
    """In-memory stand-in for the agentic runtime.

    Each call to ``run_agentic`` pops the next stub result off the queue.
    The queue may contain either ``AgenticResult`` instances or callables
    that produce them (the latter are used to inject side effects between
    calls — e.g. materializing a missing file before the next gate run).
    """

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    def run_agentic(self, *args: Any, **kwargs: Any) -> AgenticResult:
        self.calls.append({"args": args, "kwargs": kwargs})
        if not self._results:
            raise RuntimeError("no more stub results")
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item()
        return item


class _StubSink:
    """Minimal capture sink that mimics ``MajorStepProgressSink.handle_event``."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def handle_event(self, event: dict[str, Any]) -> None:
        self.events.append(event)


# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def task_artifacts(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "organized-output" / "files").mkdir(parents=True)
    return artifacts, workspace


def _state(
    artifacts,
    workspace,
    capability: str,
    validated_paths: list[str],
    **extras: Any,
) -> dict[str, Any]:
    return {
        "capability": capability,
        "validated_paths": validated_paths,
        "artifacts_dir": str(artifacts),
        "output_mode": extras.get("output_mode", "workspace"),
        "source_paths": validated_paths,
        "lifecycle_state": "running",
        "_compass_task_id": "task-1",
        "_task_id": "task-1",
    }


# ---------------------------------------------------------------------------
# Round 0 — initial clean pass
# ---------------------------------------------------------------------------


def test_clean_first_pass_emits_done_step(task_artifacts):
    artifacts, workspace = task_artifacts
    # Workspace mode: plan and output both live under artifacts_dir.
    plan_path = artifacts / "organized-output" / "files" / "organization-plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "# Plan\n## Files Organized\n"
        "| source | destination |\n| --- | --- |\n"
        f"| /src/a.txt | files/a.txt |\n",
        encoding="utf-8",
    )
    (artifacts / "organized-output" / "files" / "files").mkdir(parents=True, exist_ok=True)
    (artifacts / "organized-output" / "files" / "files" / "a.txt").write_text("x", encoding="utf-8")

    state = _state(artifacts, workspace, "organize", ["/src/a.txt"])
    sink = _StubSink()
    state["_major_step_progress_sink"] = sink
    runtime = _StubRuntime([])  # No retries needed.
    state["_runtime"] = runtime

    report = _run_plan_output_gate(state, runtime=runtime)
    assert report.is_clean

    keys = [c["step_key"] for c in sink.events]
    assert "office.validating_plan_output" in keys
    validating = [c for c in sink.events if c["step_key"] == "office.validating_plan_output"]
    assert any(c["lifecycle_state"] == "done" for c in validating)


# ---------------------------------------------------------------------------
# Round 1 — mismatch triggers retry and emits reconciling step
# ---------------------------------------------------------------------------


def test_mismatch_triggers_retry_and_emits_warning(task_artifacts):
    artifacts, workspace = task_artifacts
    # Workspace mode: plan lives under artifacts_dir.
    plan_path = artifacts / "organized-output" / "files" / "organization-plan.md"
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        "# Plan\n## Files Organized\n"
        "| source | destination |\n| --- | --- |\n"
        f"| /src/a.txt | files/a.txt |\n",
        encoding="utf-8",
    )
    # No file materialized → the first gate run will be not-clean.

    state = _state(artifacts, workspace, "organize", ["/src/a.txt"])
    sink = _StubSink()
    state["_major_step_progress_sink"] = sink

    def _retry_writes_file() -> AgenticResult:
        target = artifacts / "organized-output" / "files" / "files" / "a.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
        return AgenticResult(
            success=True,
            summary="retry wrote file",
            tool_calls=[{"name": "delete_output_file", "ok": True}],
        )

    runtime = _StubRuntime([_retry_writes_file])
    state["_runtime"] = runtime

    report = _run_plan_output_gate(state, runtime=runtime)
    assert report.is_clean

    keys = [c["step_key"] for c in sink.events]
    assert "office.validating_plan_output" in keys
    assert "office.reconciling_plan_output" in keys
    reconciling = [c for c in sink.events if c["step_key"] == "office.reconciling_plan_output"]
    assert reconciling[0]["round"] == 1


# ---------------------------------------------------------------------------
# Exhausted — three rounds of no progress → gate_exhausted step
# ---------------------------------------------------------------------------


def test_exhausted_emits_gate_exhausted_step(task_artifacts):
    artifacts, workspace = task_artifacts
    plan_path = workspace / "organized-output" / "files" / "organization-plan.md"
    plan_path.write_text(
        "# Plan\n## Files Organized\n"
        "| source | destination |\n| --- | --- |\n"
        f"| /src/a.txt | files/a.txt |\n",
        encoding="utf-8",
    )

    state = _state(artifacts, workspace, "organize", ["/src/a.txt"])
    sink = _StubSink()
    state["_major_step_progress_sink"] = sink

    runtime = _StubRuntime(
        [
            AgenticResult(success=True, summary="no", tool_calls=[]),
            AgenticResult(success=True, summary="no", tool_calls=[]),
            AgenticResult(success=True, summary="no", tool_calls=[]),
        ]
    )
    state["_runtime"] = runtime

    report = _run_plan_output_gate(state, runtime=runtime)
    assert not report.is_clean

    keys = [c["step_key"] for c in sink.events]
    assert "office.gate_exhausted" in keys

    report_path = artifacts / "plan-output-gate-report.json"
    assert report_path.exists()


# ---------------------------------------------------------------------------
# Round 0 — missing plan is repaired by the LLM retry
# ---------------------------------------------------------------------------


def test_round_0_missing_plan_causes_missing_status(task_artifacts):
    artifacts, workspace = task_artifacts
    # No plan file is written.

    state = _state(artifacts, workspace, "summarize", ["/src/a.txt"])
    sink = _StubSink()
    state["_major_step_progress_sink"] = sink

    def _retry_writes_plan_and_output() -> AgenticResult:
        # Workspace mode: plan and output both live under artifacts_dir.
        plan = artifacts / "summary-plan.md"
        plan.write_text(
            "# Plan\n## Source -> Summary Mapping\n"
            "| source | summary_target |\n| --- | --- |\n"
            f"| /src/a.txt | a.md |\n",
            encoding="utf-8",
        )
        (artifacts / "a.md").write_text("x", encoding="utf-8")
        return AgenticResult(success=True, summary="ok", tool_calls=[])

    runtime = _StubRuntime([_retry_writes_plan_and_output])
    state["_runtime"] = runtime

    report = _run_plan_output_gate(state, runtime=runtime)
    assert report.is_clean
