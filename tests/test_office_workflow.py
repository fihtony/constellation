"""New agentic workflow tests for Office Agent."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common.task_permissions import grant_permission, load_permission_grant
from office import app as office_app

# Permission grants reused from the main test file
_OFFICE_PERMISSIONS = load_permission_grant("office").to_dict()
_OFFICE_RW_PERMISSIONS = grant_permission(
    _OFFICE_PERMISSIONS,
    agent="office",
    action="write",
    scope="task_root",
    description="Allow in-place write during unit tests",
)


def _make_workflow_message(
    capability: str,
    target_paths: list[str],
    workspace: str,
    *,
    output_mode: str = "workspace",
    permissions: dict | None = None,
) -> dict:
    if permissions is None:
        permissions = _OFFICE_PERMISSIONS
    return {
        "parts": [{"text": f"Please run {capability} on the provided files."}],
        "metadata": {
            "requestedCapability": capability,
            "officeTargetPaths": target_paths,
            "officeInputRoot": workspace,
            "officeOutputMode": output_mode,
            "officeWorkspacePath": workspace,
            "sharedWorkspacePath": workspace,
            "orchestratorTaskId": "task-unit",
            "orchestratorCallbackUrl": "",
            "permissions": permissions,
        },
    }


class TestOfficeWorkflowRuntime(unittest.TestCase):
    """Tests for the new agentic _run_workflow that drives the LLM."""

    def test_run_workflow_completes_on_success(self):
        """_run_workflow transitions task to COMPLETED when run_agentic succeeds."""
        from common.runtime.adapter import AgenticResult

        with tempfile.TemporaryDirectory(prefix="office_wf_success_") as workspace:
            source = Path(workspace, "doc.txt")
            source.write_text("Hello world", encoding="utf-8")
            task = office_app.task_store.create()
            message = _make_workflow_message("office.document.summarize", [str(source)], workspace)
            mock_result = AgenticResult(
                success=True, summary="Summary complete.", artifacts=[], turns_used=5, tool_calls=3
            )
            mock_runtime = mock.MagicMock()
            mock_runtime.run_agentic.return_value = mock_result
            with mock.patch.object(office_app, "get_runtime", return_value=mock_runtime), \
                    mock.patch.object(office_app, "_notify_callback"), \
                    mock.patch.object(office_app, "_report_progress"):
                office_app._run_workflow(task.task_id, message)

            current = office_app.task_store.get(task.task_id)
            self.assertEqual(current.state, "TASK_STATE_COMPLETED")
            self.assertIn("Summary complete", current.status_message)
            mock_runtime.run_agentic.assert_called_once()

    def test_run_workflow_fails_on_runtime_failure(self):
        """_run_workflow transitions task to FAILED when run_agentic returns failure."""
        from common.runtime.adapter import AgenticResult

        with tempfile.TemporaryDirectory(prefix="office_wf_fail_") as workspace:
            source = Path(workspace, "data.csv")
            source.write_text("a,b\n1,2\n", encoding="utf-8")
            task = office_app.task_store.create()
            message = _make_workflow_message("office.data.analyze", [str(source)], workspace)
            mock_result = AgenticResult(
                success=False, summary="Could not process file.", artifacts=[], turns_used=3, tool_calls=1
            )
            mock_runtime = mock.MagicMock()
            mock_runtime.run_agentic.return_value = mock_result
            with mock.patch.object(office_app, "get_runtime", return_value=mock_runtime), \
                    mock.patch.object(office_app, "_notify_callback"), \
                    mock.patch.object(office_app, "_report_progress"):
                office_app._run_workflow(task.task_id, message)

            current = office_app.task_store.get(task.task_id)
            self.assertEqual(current.state, "TASK_STATE_FAILED")

    def test_run_workflow_permission_denied_for_inplace_without_write_grant(self):
        """_run_workflow fails fast before calling LLM if inplace mode lacks write permission."""
        with tempfile.TemporaryDirectory(prefix="office_perm_deny_") as workspace:
            source = Path(workspace, "doc.txt")
            source.write_text("Content.", encoding="utf-8")
            task = office_app.task_store.create()
            message = _make_workflow_message(
                "office.document.summarize",
                [str(source)],
                workspace,
                output_mode="inplace",
                permissions=_OFFICE_PERMISSIONS,  # read-only, no write grant
            )
            mock_runtime = mock.MagicMock()
            with mock.patch.object(office_app, "get_runtime", return_value=mock_runtime), \
                    mock.patch.object(office_app, "_notify_callback"), \
                    mock.patch.object(office_app, "_report_progress"):
                office_app._run_workflow(task.task_id, message)

            current = office_app.task_store.get(task.task_id)
            self.assertEqual(current.state, "TASK_STATE_FAILED")
            mock_runtime.run_agentic.assert_not_called()

    def test_run_workflow_permission_passes_for_workspace_mode(self):
        """_run_workflow proceeds to LLM for workspace mode with read-only permission."""
        from common.runtime.adapter import AgenticResult

        with tempfile.TemporaryDirectory(prefix="office_perm_ok_") as workspace:
            source = Path(workspace, "notes.txt")
            source.write_text("Study notes", encoding="utf-8")
            task = office_app.task_store.create()
            message = _make_workflow_message(
                "office.document.summarize",
                [str(source)],
                workspace,
                output_mode="workspace",
                permissions=_OFFICE_PERMISSIONS,  # read-only is enough for workspace mode
            )
            mock_result = AgenticResult(
                success=True, summary="Notes summarized.", artifacts=[], turns_used=1, tool_calls=0
            )
            mock_runtime = mock.MagicMock()
            mock_runtime.run_agentic.return_value = mock_result
            with mock.patch.object(office_app, "get_runtime", return_value=mock_runtime), \
                    mock.patch.object(office_app, "_notify_callback"), \
                    mock.patch.object(office_app, "_report_progress"):
                office_app._run_workflow(task.task_id, message)

            mock_runtime.run_agentic.assert_called_once()
            current = office_app.task_store.get(task.task_id)
            self.assertEqual(current.state, "TASK_STATE_COMPLETED")

    def test_run_workflow_inplace_allowed_with_write_grant(self):
        """_run_workflow proceeds to LLM for inplace mode when write permission is granted."""
        from common.runtime.adapter import AgenticResult

        with tempfile.TemporaryDirectory(prefix="office_perm_rw_") as workspace:
            source = Path(workspace, "report.txt")
            source.write_text("Report content", encoding="utf-8")
            task = office_app.task_store.create()
            message = _make_workflow_message(
                "office.document.summarize",
                [str(source)],
                workspace,
                output_mode="inplace",
                permissions=_OFFICE_RW_PERMISSIONS,
            )
            mock_result = AgenticResult(
                success=True, summary="Inplace summary created.", artifacts=[], turns_used=1, tool_calls=0
            )
            mock_runtime = mock.MagicMock()
            mock_runtime.run_agentic.return_value = mock_result
            with mock.patch.object(office_app, "get_runtime", return_value=mock_runtime), \
                    mock.patch.object(office_app, "_notify_callback"), \
                    mock.patch.object(office_app, "_report_progress"):
                office_app._run_workflow(task.task_id, message)

            mock_runtime.run_agentic.assert_called_once()
            current = office_app.task_store.get(task.task_id)
            self.assertEqual(current.state, "TASK_STATE_COMPLETED")

    def test_run_workflow_prompt_contains_capability_and_paths(self):
        """_run_workflow builds a task prompt containing the capability and target paths."""
        from common.runtime.adapter import AgenticResult

        with tempfile.TemporaryDirectory(prefix="office_prompt_") as workspace:
            source = Path(workspace, "data.csv")
            source.write_text("name,score\nAlice,90\n", encoding="utf-8")
            task = office_app.task_store.create()
            message = _make_workflow_message("office.data.analyze", [str(source)], workspace)
            captured: list[str] = []

            def mock_run_agentic(**kwargs):
                captured.append(kwargs.get("task", ""))
                return AgenticResult(success=True, summary="done", artifacts=[], turns_used=1, tool_calls=0)

            mock_runtime = mock.MagicMock()
            mock_runtime.run_agentic.side_effect = mock_run_agentic
            with mock.patch.object(office_app, "get_runtime", return_value=mock_runtime), \
                    mock.patch.object(office_app, "_notify_callback"), \
                    mock.patch.object(office_app, "_report_progress"):
                office_app._run_workflow(task.task_id, message)

            self.assertTrue(captured, "run_agentic was not called")
            prompt = captured[0]
            self.assertIn("office.data.analyze", prompt)
            self.assertIn(str(source), prompt)

    def test_run_workflow_creates_summary_artifact_with_metadata(self):
        """_run_workflow creates a summary artifact with agentId and capability metadata."""
        from common.runtime.adapter import AgenticResult

        with tempfile.TemporaryDirectory(prefix="office_artifact_") as workspace:
            source = Path(workspace, "notes.txt")
            source.write_text("content", encoding="utf-8")
            task = office_app.task_store.create()
            message = _make_workflow_message("office.document.summarize", [str(source)], workspace)
            mock_result = AgenticResult(
                success=True, summary="Notes processed.", artifacts=[], turns_used=2, tool_calls=1
            )
            mock_runtime = mock.MagicMock()
            mock_runtime.run_agentic.return_value = mock_result
            with mock.patch.object(office_app, "get_runtime", return_value=mock_runtime), \
                    mock.patch.object(office_app, "_notify_callback"), \
                    mock.patch.object(office_app, "_report_progress"):
                office_app._run_workflow(task.task_id, message)

            current = office_app.task_store.get(task.task_id)
            self.assertEqual(current.state, "TASK_STATE_COMPLETED")
            self.assertIsNotNone(current.artifacts)
            self.assertGreater(len(current.artifacts), 0)
            first_artifact = current.artifacts[0]
            self.assertEqual(first_artifact["metadata"]["agentId"], office_app.AGENT_ID)
            self.assertEqual(first_artifact["metadata"]["capability"], "office.document.summarize")


class TestOfficePermissionsWorkflow(unittest.TestCase):
    """Permission enforcement tests via _run_workflow (new agentic path)."""

    def test_workspace_output_allowed_with_default_office_permissions(self):
        from common.runtime.adapter import AgenticResult
        with tempfile.TemporaryDirectory(prefix="office_perm_ws_") as tmpdir:
            target_path = os.path.join(tmpdir, "sample.txt")
            with open(target_path, "w", encoding="utf-8") as fh:
                fh.write("sample")
            task = office_app.task_store.create()
            message = _make_workflow_message(
                "office.document.summarize", [target_path], tmpdir,
                output_mode="workspace", permissions=load_permission_grant("office").to_dict()
            )
            mock_result = AgenticResult(success=True, summary="ok", artifacts=[], turns_used=1, tool_calls=0)
            mock_runtime = mock.MagicMock()
            mock_runtime.run_agentic.return_value = mock_result
            with mock.patch.object(office_app, "get_runtime", return_value=mock_runtime), \
                    mock.patch.object(office_app, "_notify_callback"), \
                    mock.patch.object(office_app, "_report_progress"):
                office_app._run_workflow(task.task_id, message)

            current = office_app.task_store.get(task.task_id)
            self.assertEqual(current.state, "TASK_STATE_COMPLETED")
            mock_runtime.run_agentic.assert_called_once()

    def test_inplace_output_denied_without_explicit_write_grant(self):
        with tempfile.TemporaryDirectory(prefix="office_perm_no_write_") as tmpdir:
            target_path = os.path.join(tmpdir, "sample.txt")
            with open(target_path, "w", encoding="utf-8") as fh:
                fh.write("sample")
            task = office_app.task_store.create()
            message = _make_workflow_message(
                "office.document.summarize", [target_path], tmpdir,
                output_mode="inplace",
                permissions=load_permission_grant("office").to_dict(),  # read-only
            )
            mock_runtime = mock.MagicMock()
            with mock.patch.object(office_app, "get_runtime", return_value=mock_runtime), \
                    mock.patch.object(office_app, "_notify_callback"), \
                    mock.patch.object(office_app, "_report_progress"):
                office_app._run_workflow(task.task_id, message)

            current = office_app.task_store.get(task.task_id)
            self.assertEqual(current.state, "TASK_STATE_FAILED")
            mock_runtime.run_agentic.assert_not_called()

    def test_inplace_output_allowed_after_user_grant(self):
        from common.runtime.adapter import AgenticResult
        with tempfile.TemporaryDirectory(prefix="office_perm_rw_granted_") as tmpdir:
            target_path = os.path.join(tmpdir, "sample.txt")
            with open(target_path, "w", encoding="utf-8") as fh:
                fh.write("sample")
            permissions = grant_permission(
                load_permission_grant("office").to_dict(),
                agent="office", action="write", scope="task_root",
                description="Approved by user",
            )
            task = office_app.task_store.create()
            message = _make_workflow_message(
                "office.document.summarize", [target_path], tmpdir,
                output_mode="inplace", permissions=permissions
            )
            mock_result = AgenticResult(success=True, summary="ok", artifacts=[], turns_used=1, tool_calls=0)
            mock_runtime = mock.MagicMock()
            mock_runtime.run_agentic.return_value = mock_result
            with mock.patch.object(office_app, "get_runtime", return_value=mock_runtime), \
                    mock.patch.object(office_app, "_notify_callback"), \
                    mock.patch.object(office_app, "_report_progress"):
                office_app._run_workflow(task.task_id, message)

            current = office_app.task_store.get(task.task_id)
            self.assertEqual(current.state, "TASK_STATE_COMPLETED")
            mock_runtime.run_agentic.assert_called_once()


if __name__ == "__main__":
    unittest.main()
