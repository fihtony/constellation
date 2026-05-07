#!/usr/bin/env python3
"""Focused unit tests for Office Agent permission enforcement via _run_workflow."""

from __future__ import annotations

import os
import sys
import tempfile
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from common.task_permissions import grant_permission, load_permission_grant
from office import app as office_app


def _message_for(target_path: str, *, output_mode: str, permissions: dict | None = None) -> dict:
    workspace = os.path.dirname(target_path)
    return {
        "parts": [{"text": "Summarize the selected folder."}],
        "metadata": {
            "requestedCapability": "office.document.summarize",
            "officeTargetPaths": [target_path],
            "officeInputRoot": workspace,
            "officeOutputMode": output_mode,
            "officeWorkspacePath": workspace,
            "sharedWorkspacePath": workspace,
            "orchestratorTaskId": "task-perm-test",
            "orchestratorCallbackUrl": "",
            "permissions": permissions,
        },
    }


def test_workspace_output_allowed_with_default_office_permissions():
    from common.runtime.adapter import AgenticResult

    with tempfile.TemporaryDirectory(prefix="office_perm_") as tmpdir:
        target_path = os.path.join(tmpdir, "sample.txt")
        with open(target_path, "w", encoding="utf-8") as handle:
            handle.write("sample")

        task = office_app.task_store.create()
        message = _message_for(
            target_path,
            output_mode="workspace",
            permissions=load_permission_grant("office").to_dict(),
        )
        mock_result = AgenticResult(
            success=True, summary="ok", artifacts=[], turns_used=1, tool_calls=0
        )
        mock_runtime = mock.MagicMock()
        mock_runtime.run_agentic.return_value = mock_result
        with mock.patch.object(office_app, "get_runtime", return_value=mock_runtime), \
                mock.patch.object(office_app, "_notify_callback"), \
                mock.patch.object(office_app, "_report_progress"):
            office_app._run_workflow(task.task_id, message)

        current = office_app.task_store.get(task.task_id)
        assert current.state == "TASK_STATE_COMPLETED"
        mock_runtime.run_agentic.assert_called_once()


def test_inplace_output_denied_without_explicit_write_grant():
    with tempfile.TemporaryDirectory(prefix="office_perm_") as tmpdir:
        target_path = os.path.join(tmpdir, "sample.txt")
        with open(target_path, "w", encoding="utf-8") as handle:
            handle.write("sample")

        task = office_app.task_store.create()
        message = _message_for(
            target_path,
            output_mode="inplace",
            permissions=load_permission_grant("office").to_dict(),
        )
        mock_runtime = mock.MagicMock()
        with mock.patch.object(office_app, "get_runtime", return_value=mock_runtime), \
                mock.patch.object(office_app, "_notify_callback"), \
                mock.patch.object(office_app, "_report_progress"):
            office_app._run_workflow(task.task_id, message)

        current = office_app.task_store.get(task.task_id)
        assert current.state == "TASK_STATE_FAILED"
        mock_runtime.run_agentic.assert_not_called()


def test_inplace_output_allowed_after_user_grant():
    from common.runtime.adapter import AgenticResult

    with tempfile.TemporaryDirectory(prefix="office_perm_") as tmpdir:
        target_path = os.path.join(tmpdir, "sample.txt")
        with open(target_path, "w", encoding="utf-8") as handle:
            handle.write("sample")

        permissions = grant_permission(
            load_permission_grant("office").to_dict(),
            agent="office",
            action="write",
            scope="task_root",
            description="Approved by user",
        )
        task = office_app.task_store.create()
        message = _message_for(target_path, output_mode="inplace", permissions=permissions)
        mock_result = AgenticResult(
            success=True, summary="ok", artifacts=[], turns_used=1, tool_calls=0
        )
        mock_runtime = mock.MagicMock()
        mock_runtime.run_agentic.return_value = mock_result
        with mock.patch.object(office_app, "get_runtime", return_value=mock_runtime), \
                mock.patch.object(office_app, "_notify_callback"), \
                mock.patch.object(office_app, "_report_progress"):
            office_app._run_workflow(task.task_id, message)

        current = office_app.task_store.get(task.task_id)
        assert current.state == "TASK_STATE_COMPLETED"
        mock_runtime.run_agentic.assert_called_once()
