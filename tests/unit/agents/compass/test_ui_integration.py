"""Tests for Compass UI routes."""
import json
from pathlib import Path

import pytest
from framework.task_store import InMemoryTaskStore
from agents.compass.ui.routes import handle_ui_request


class TestUIRoutes:
    @pytest.fixture()
    def task_store(self):
        return InMemoryTaskStore()

    def test_get_ui_returns_html(self):
        result = handle_ui_request("GET", "/ui")
        assert result["status"] == 200
        assert "text/html" in result["headers"]["Content-Type"]
        assert result["headers"]["Cache-Control"] == "no-store, no-cache, must-revalidate"
        assert result["headers"]["Pragma"] == "no-cache"
        assert "Compass Chat" in result["body"]
        assert 'id="dashboard"' in result["body"]

    def test_get_tasks_returns_json(self, task_store):
        task_store.create_task(
            agent_id="compass",
            metadata={
                "summary": "Test task",
                "userRequest": "Please summarize this",
                "chat_history": [{"role": "USER", "text": "Please summarize this", "tone": "normal", "ts": "2026-05-28T00:00:00+00:00"}],
            },
        )
        result = handle_ui_request("GET", "/tasks", task_store=task_store)
        assert result["status"] == 200
        assert "application/json" in result["headers"]["Content-Type"]
        assert result["headers"]["Cache-Control"] == "no-store, no-cache, must-revalidate"
        assert result["headers"]["Pragma"] == "no-cache"
        assert result["body"]["tasks"]
        first = result["body"]["tasks"][0]
        assert "createdAt" in first
        assert "updatedAt" in first
        assert first["orchestratorTaskId"] == first["task_id"]
        assert first["userRequest"] == "Please summarize this"
        assert first["chatHistory"][0]["text"] == "Please summarize this"

    def test_get_task_detail(self, task_store):
        # Create a task first
        task = task_store.create_task(agent_id="compass", metadata={"summary": "Test task"})
        task_id = task.id

        result = handle_ui_request("GET", f"/tasks/{task_id}", task_store=task_store)
        assert result["status"] == 200
        assert result["headers"]["Cache-Control"] == "no-store, no-cache, must-revalidate"
        assert result["headers"]["Pragma"] == "no-cache"
        data = result["body"]
        assert data["task_id"] == task_id
        assert data["orchestratorTaskId"] == task_id

    def test_get_task_detail_includes_artifact_metadata(self, task_store):
        from framework.a2a.protocol import Artifact

        task = task_store.create_task(agent_id="compass", metadata={"summary": "Test task"})
        task_store.set_artifacts(
            task.id,
            [
                Artifact(
                    name="compass-response",
                    artifact_type="text/plain",
                    parts=[{"text": "Done"}],
                    metadata={"prUrl": "https://example.com/pr/1", "branch": "feature/x"},
                )
            ],
        )

        result = handle_ui_request("GET", f"/tasks/{task.id}", task_store=task_store)
        assert result["status"] == 200
        data = result["body"]
        assert data["artifacts"][0]["metadata"]["prUrl"] == "https://example.com/pr/1"
        assert data["artifacts"][0]["metadata"]["branch"] == "feature/x"

    def test_get_task_detail_includes_office_delivery_metadata(self, task_store):
        from framework.a2a.protocol import Artifact

        task = task_store.create_task(agent_id="compass", metadata={"summary": "Office task dispatched. Status: completed"})
        task_store.set_artifacts(
            task.id,
            [
                Artifact(
                    name="compass-response",
                    artifact_type="text/plain",
                    parts=[{"text": "Office task dispatched. Status: completed"}],
                    metadata={
                        "summary": "Analysis complete. The report has been written to the workspace.",
                        "deliveryReportPath": "artifacts/task-1/office/task-report.json",
                    },
                )
            ],
        )

        result = handle_ui_request("GET", f"/tasks/{task.id}", task_store=task_store)
        assert result["status"] == 200
        data = result["body"]
        assert data["artifacts"][0]["metadata"]["summary"] == "Analysis complete. The report has been written to the workspace."
        assert data["artifacts"][0]["metadata"]["deliveryReportPath"] == "artifacts/task-1/office/task-report.json"

    def test_completed_with_warning_serializes_warning_status_kind(self, task_store):
        from framework.a2a.protocol import Artifact

        task = task_store.create_task(
            agent_id="compass",
            metadata={
                "task_type": "office",
                "chat_history": [{"role": "AGENT", "text": "Completed with warnings.", "tone": "warning"}],
            },
        )
        task_store.complete_task(task.id, message="Completed with warnings.")
        task_store.set_artifacts(
            task.id,
            [
                Artifact(
                    name="compass-response",
                    artifact_type="text/plain",
                    parts=[{"text": "Completed with warnings."}],
                    metadata={
                        "status": "completed_with_warning",
                        "warnings_count": 1,
                        "summary": "Completed with warnings.",
                    },
                )
            ],
        )

        result = handle_ui_request("GET", f"/tasks/{task.id}", task_store=task_store)

        assert result["status"] == 200
        data = result["body"]
        assert data["status"] == "warning"
        assert data["statusKind"] == "warning"
        assert data["statusState"] == "TASK_STATE_COMPLETED"

    def test_ui_events_route_exists(self, task_store):
        result = handle_ui_request("GET", "/ui/events", task_store=task_store)
        assert result["status"] == 200
        assert result["headers"]["Content-Type"].startswith("text/event-stream")

    def test_logs_route_uses_filesystem_fallback(self, monkeypatch, tmp_path: Path):
        task_id = "task-logs"
        agent_dir = tmp_path / task_id / "compass"
        agent_dir.mkdir(parents=True)
        (agent_dir / "agent.log").write_text(
            "2026-05-28 10:30:00 [INFO] [compass] Accepted task\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("ARTIFACT_ROOT", str(tmp_path))

        result = handle_ui_request("GET", f"/logs/{task_id}")

        assert result["status"] == 200
        assert result["body"]["logs"][0]["task_id"] == task_id
        assert result["body"]["logs"][0]["message"] == "Accepted task"

    def test_get_task_detail_renders_office_rows_with_emitted_facts(
        self,
        task_store,
        monkeypatch,
        tmp_path: Path,
    ):
        source_dir = tmp_path / "source-folder"
        nested = source_dir / "nested"
        nested.mkdir(parents=True)
        (source_dir / "alpha.txt").write_text("alpha", encoding="utf-8")
        (nested / "beta.txt").write_text("beta", encoding="utf-8")
        (nested / "gamma.txt").write_text("gamma", encoding="utf-8")

        task = task_store.create_task(
            agent_id="compass",
            metadata={
                "task_type": "office",
                "office_request": {
                    "capability": "organize",
                    "output_mode": "workspace",
                    "source_paths": [str(source_dir)],
                },
                "major_step_rows": {
                    "office.received#0": {
                        "step_key": "office.received",
                        "step_instance_key": "office.received#0",
                        "title": "Office receiving task",
                        "agent": "office",
                        "lifecycle_state": "done",
                        "visual_state": "done",
                        "summary_template": "Office received the task: {capability} on {source_count} {source_kind} containing {discovered_source_count} file(s).",
                        "summary_facts": {"capability": "organize", "source_count": 1, "source_kind": "folder", "discovered_source_count": 3},
                        "started_at": "2026-06-03T10:00:00+00:00",
                        "ended_at": "2026-06-03T10:00:01+00:00",
                    },
                    "office.validating_plan_output#0": {
                        "step_key": "office.validating_plan_output",
                        "step_instance_key": "office.validating_plan_output#0",
                        "title": "Office validating output against plan",
                        "agent": "office",
                        "lifecycle_state": "warning",
                        "visual_state": "warning",
                        "summary_template": "Plan-output gate exhausted after {round_count} reconciliation round(s): {missing_count} missing, {unexpected_count} unexpected, {mismatch_count} mismatched. See plan-output-gate-report.json.",
                        "summary_facts": {"plan_status": "invalid", "invalid_plan_entry_count": 1, "round_count": 3, "missing_count": 0, "unexpected_count": 0, "mismatch_count": 0},
                        "started_at": "2026-06-03T10:00:02+00:00",
                        "ended_at": "2026-06-03T10:00:03+00:00",
                    },
                },
            },
            task_id="task-folder-organize",
        )

        result = handle_ui_request("GET", f"/tasks/{task.id}", task_store=task_store)

        assert result["status"] == 200
        rows = result["body"]["majorStepRows"]
        assert rows["office.received#0"]["summary_facts"]["source_kind"] == "folder"
        assert rows["office.received#0"]["summary_facts"]["discovered_source_count"] == 3
        assert rows["office.validating_plan_output#0"]["summary_facts"]["plan_status"] == "invalid"
        assert rows["office.validating_plan_output#0"]["summary_facts"]["invalid_plan_entry_count"] == 1


def test_office_skeleton_includes_validating_reconciling_exhausted():
    """Spec §5.7: the three new gate step keys exist in the office skeleton for all capabilities."""
    from agents.compass.agent import _office_major_step_skeleton
    for capability in ("analyze", "summarize", "organize"):
        rows = _office_major_step_skeleton({"capability": capability})
        keys = [r["step_key"] for r in rows]
        # validating is unconditional; the other two are conditional
        assert "office.validating_plan_output" in keys, f"missing validating for {capability}"
        reconciling = [r for r in rows if r["step_key"] == "office.reconciling_plan_output"]
        assert len(reconciling) == 1, f"reconciling row not unique for {capability}"
        assert reconciling[0].get("conditional") is True, f"reconciling not conditional for {capability}"
        exhausted = [r for r in rows if r["step_key"] == "office.gate_exhausted"]
        assert len(exhausted) == 1, f"exhausted row not unique for {capability}"
        assert exhausted[0].get("conditional") is True, f"exhausted not conditional for {capability}"
        # ordering: validating/reconciling/exhausted must come BEFORE verifying
        idx_validating = keys.index("office.validating_plan_output")
        idx_reconciling = keys.index("office.reconciling_plan_output")
        idx_exhausted = keys.index("office.gate_exhausted")
        idx_verifying = keys.index("office.verifying")
        assert idx_validating < idx_verifying, f"validating not before verifying for {capability}"
        assert idx_reconciling < idx_verifying, f"reconciling not before verifying for {capability}"
        assert idx_exhausted < idx_verifying, f"exhausted not before verifying for {capability}"
