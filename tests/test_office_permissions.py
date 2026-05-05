#!/usr/bin/env python3
"""Focused unit tests for Office Agent permission enforcement."""

from __future__ import annotations

import os
import sys
import tempfile
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from common.task_permissions import PermissionDeniedError, grant_permission, load_permission_grant
from office import app as office_app


def _message_for(target_path: str, *, output_mode: str, permissions: dict | None = None) -> dict:
    return {
        "parts": [{"text": "Summarize the selected folder."}],
        "metadata": {
            "requestedCapability": "office.document.summarize",
            "officeTargetPaths": [target_path],
            "officeInputRoot": os.path.dirname(target_path),
            "officeOutputMode": output_mode,
            "sharedWorkspacePath": os.path.dirname(target_path),
            "permissions": permissions,
        },
    }


def test_workspace_output_allowed_with_default_office_permissions():
    with tempfile.TemporaryDirectory(prefix="office_perm_") as tmpdir:
        target_path = os.path.join(tmpdir, "sample.txt")
        with open(target_path, "w", encoding="utf-8") as handle:
            handle.write("sample")

        message = _message_for(
            target_path,
            output_mode="workspace",
            permissions=load_permission_grant("office").to_dict(),
        )
        with mock.patch.object(
            office_app,
            "_execute_summary",
            return_value={"summary": "ok", "artifacts": [], "warnings": []},
        ):
            result = office_app._execute_capability("task-1", message)

        assert result["summary"] == "ok"


def test_inplace_output_denied_without_explicit_write_grant():
    with tempfile.TemporaryDirectory(prefix="office_perm_") as tmpdir:
        target_path = os.path.join(tmpdir, "sample.txt")
        with open(target_path, "w", encoding="utf-8") as handle:
            handle.write("sample")

        message = _message_for(
            target_path,
            output_mode="inplace",
            permissions=load_permission_grant("office").to_dict(),
        )
        with mock.patch.object(
            office_app,
            "_execute_summary",
            return_value={"summary": "ok", "artifacts": [], "warnings": []},
        ):
            try:
                office_app._execute_capability("task-1", message)
            except PermissionDeniedError as exc:
                assert exc.details.permission_agent == "office"
                assert exc.details.action == "write"
            else:
                raise AssertionError("Expected in-place Office output to require explicit write permission")


def test_inplace_output_allowed_after_user_grant():
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
        message = _message_for(target_path, output_mode="inplace", permissions=permissions)
        with mock.patch.object(
            office_app,
            "_execute_summary",
            return_value={"summary": "ok", "artifacts": [], "warnings": []},
        ):
            result = office_app._execute_capability("task-1", message)

        assert result["summary"] == "ok"
