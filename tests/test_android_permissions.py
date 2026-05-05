#!/usr/bin/env python3
"""Unit tests for the Android Agent permissions forwarding fix.

Verifies that _call_sync_agent, _clone_repo, _list_remote_branches,
_create_branch, _push_files, and _create_pr correctly forward
permissions to downstream agents.
"""

import json
import os
import sys
import tempfile
import time
import unittest
from typing import cast
from unittest.mock import patch, MagicMock

# Ensure correct paths
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "android"))

# Minimal env setup
os.environ.setdefault("AGENT_ID", "android-agent")
os.environ.setdefault("REGISTRY_URL", "http://localhost:9000")
os.environ.setdefault("ADVERTISED_BASE_URL", "http://android-agent:8000")
os.environ.setdefault("COMPASS_URL", "http://compass:8080")
os.environ.setdefault("OPENAI_BASE_URL", "http://localhost:1288/v1")
os.environ.setdefault("OPENAI_MODEL", "gpt-5-mini")
os.environ.setdefault("ALLOW_MOCK_FALLBACK", "1")


class TestCallSyncAgentPermissions(unittest.TestCase):
    """Test that _call_sync_agent forwards permissions in metadata."""

    @patch("android.app._a2a_send")
    @patch("android.app._resolve_agent_service_url")
    def test_permissions_included_in_message(self, mock_resolve, mock_send):
        from android.app import _call_sync_agent
        mock_resolve.return_value = "http://scm:8020"
        mock_send.return_value = {
            "id": "t1",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [],
        }
        perms = {"grant": "development", "actions": [{"action": "repo.clone", "scope": "*"}]}

        _call_sync_agent(
            "scm.git.clone",
            "Clone repo X",
            "task-001",
            "/workspace",
            "compass-task-001",
            permissions=perms,
        )

        # Verify the message sent includes permissions
        call_args = mock_send.call_args
        message = call_args[0][1]  # Second positional arg is the message
        self.assertIn("permissions", message.get("metadata", {}))
        self.assertEqual(message["metadata"]["permissions"], perms)

    @patch("android.app._a2a_send")
    @patch("android.app._resolve_agent_service_url")
    def test_no_permissions_when_none(self, mock_resolve, mock_send):
        from android.app import _call_sync_agent
        mock_resolve.return_value = "http://scm:8020"
        mock_send.return_value = {
            "id": "t1",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [],
        }

        _call_sync_agent(
            "scm.git.clone",
            "Clone repo X",
            "task-001",
            "/workspace",
            "compass-task-001",
            permissions=None,
        )

        call_args = mock_send.call_args
        message = call_args[0][1]
        self.assertNotIn("permissions", message.get("metadata", {}))


class TestCloneRepoPermissions(unittest.TestCase):
    """Test that _clone_repo passes permissions to _call_sync_agent."""

    @patch("android.app._call_sync_agent")
    def test_clone_repo_forwards_permissions(self, mock_call):
        from android.app import _clone_repo
        mock_call.return_value = {
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [
                {"parts": [{"text": json.dumps({"clonePath": "/workspace/repo"})}]}
            ],
        }
        perms = {"grant": "development", "actions": [{"action": "repo.clone", "scope": "*"}]}

        result = _clone_repo("task-001", "https://github.com/org/repo.git", "/workspace", "compass-001", permissions=perms)

        mock_call.assert_called_once()
        kwargs = mock_call.call_args[1]
        self.assertEqual(kwargs.get("permissions"), perms)
        self.assertEqual(result, "/workspace/repo")

    @patch("android.app._call_sync_agent")
    def test_clone_repo_no_permissions(self, mock_call):
        from android.app import _clone_repo
        mock_call.return_value = {
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [
                {"parts": [{"text": json.dumps({"clonePath": "/workspace/repo"})}]}
            ],
        }

        _clone_repo("task-001", "https://github.com/org/repo.git", "/workspace", "compass-001")

        kwargs = mock_call.call_args[1]
        self.assertIsNone(kwargs.get("permissions"))


class TestListRemoteBranchesPermissions(unittest.TestCase):
    @patch("android.app._call_sync_agent")
    def test_list_branches_forwards_permissions(self, mock_call):
        from android.app import _list_remote_branches
        mock_call.return_value = {
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [
                {"parts": [{"text": json.dumps([{"name": "main"}, {"name": "dev"}])}]}
            ],
        }
        perms = {"grant": "development"}

        result = _list_remote_branches("task-001", "https://github.com/org/repo.git", "/ws", "c-001", permissions=perms)

        kwargs = mock_call.call_args[1]
        self.assertEqual(kwargs.get("permissions"), perms)
        self.assertEqual(result, {"main", "dev"})


class TestCreateBranchPermissions(unittest.TestCase):
    @patch("android.app._call_sync_agent")
    def test_create_branch_forwards_permissions(self, mock_call):
        from android.app import _create_branch
        mock_call.return_value = {
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [],
        }
        perms = {"grant": "development"}

        result = _create_branch("task-001", "https://github.com/org/repo.git", "feat/x", "main", "/ws", "c-001", permissions=perms)

        kwargs = mock_call.call_args[1]
        self.assertEqual(kwargs.get("permissions"), perms)
        self.assertTrue(result)


class TestPushFilesPermissions(unittest.TestCase):
    @patch("android.app._poll_task")
    @patch("android.app._a2a_send")
    @patch("android.app._resolve_agent_service_url")
    def test_push_files_includes_permissions(self, mock_resolve, mock_send, mock_poll):
        from android.app import _push_files
        mock_resolve.return_value = "http://scm:8020"
        mock_send.return_value = {
            "id": "t1",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [],
        }
        perms = {"grant": "development", "actions": [{"action": "git.push", "scope": "*"}]}

        files = [{"path": "src/main.kt", "content": "fun main() {}"}]
        result = _push_files(
            "task-001", "https://github.com/org/repo.git", "feat/x",
            files, "commit msg", "/ws", "c-001", "main",
            permissions=perms,
        )

        call_args = mock_send.call_args
        message = call_args[0][1]
        self.assertIn("permissions", message.get("metadata", {}))
        self.assertEqual(message["metadata"]["permissions"], perms)
        self.assertTrue(result)


class TestCreatePrPermissions(unittest.TestCase):
    @patch("android.app._poll_task")
    @patch("android.app._a2a_send")
    @patch("android.app._resolve_agent_service_url")
    def test_create_pr_includes_permissions(self, mock_resolve, mock_send, mock_poll):
        from android.app import _create_pr
        mock_resolve.return_value = "http://scm:8020"
        mock_send.return_value = {
            "id": "t1",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [
                {"parts": [{"text": json.dumps({"htmlUrl": "https://github.com/org/repo/pull/1"})}]}
            ],
        }
        perms = {"grant": "development", "actions": [{"action": "pr.create", "scope": "*"}]}

        result = _create_pr(
            "task-001", "https://github.com/org/repo.git", "feat/x", "main",
            "PR Title", "PR Body", "/ws", "c-001",
            permissions=perms,
        )

        call_args = mock_send.call_args
        message = call_args[0][1]
        self.assertIn("permissions", message.get("metadata", {}))
        self.assertEqual(message["metadata"]["permissions"], perms)
        self.assertEqual(result, "https://github.com/org/repo/pull/1")

    @patch("android.app._poll_task")
    @patch("android.app._a2a_send")
    @patch("android.app._resolve_agent_service_url")
    def test_create_pr_extracts_url_from_bitbucket_detail_links(self, mock_resolve, mock_send, mock_poll):
        from android.app import _create_pr

        mock_resolve.return_value = "http://scm:8020"
        mock_send.return_value = {
            "id": "t1",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [
                {
                    "parts": [
                        {
                            "text": json.dumps(
                                {
                                    "detail": {
                                        "links": {
                                            "self": [
                                                {"href": "https://bitbucket.example.com/projects/APP/repos/mobile/pull-requests/42"}
                                            ]
                                        }
                                    }
                                }
                            )
                        }
                    ]
                }
            ],
        }

        result = _create_pr(
            "task-001", "https://bitbucket.example.com/projects/APP/repos/mobile", "feat/x", "main",
            "PR Title", "PR Body", "/ws", "c-001",
        )

        self.assertEqual(result, "https://bitbucket.example.com/projects/APP/repos/mobile/pull-requests/42")

    @patch("android.app._poll_task")
    @patch("android.app._a2a_send")
    @patch("android.app._resolve_agent_service_url")
    def test_create_pr_extracts_url_from_artifact_metadata(self, mock_resolve, mock_send, mock_poll):
        from android.app import _create_pr

        mock_resolve.return_value = "http://scm:8020"
        mock_send.return_value = {
            "id": "t1",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [
                {
                    "metadata": {"prUrl": "https://github.com/org/widget/pull/7"},
                    "parts": [{"text": "PR created successfully"}],
                }
            ],
        }

        result = _create_pr(
            "task-001", "https://github.com/org/widget.git", "feat/x", "main",
            "PR Title", "PR Body", "/ws", "c-001",
        )

        self.assertEqual(result, "https://github.com/org/widget/pull/7")


class TestJiraRequestPermissions(unittest.TestCase):
    @patch("android.app._call_sync_agent")
    def test_jira_request_uses_a2a_metadata_permissions(self, mock_call_sync):
        from android.app import _jira_request_json
        mock_call_sync.return_value = {
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [
                {"name": "jira-raw-payload", "parts": [{"text": json.dumps({"key": "TEST-1"})}]}
            ],
        }
        perms = {"grant": "development", "actions": [{"action": "ticket.fetch", "scope": "*"}]}

        result = _jira_request_json(
            "jira.ticket.fetch", "GET", "/jira/tickets/TEST-1",
            task_id="task-001",
            compass_task_id="compass-001",
            permissions=perms,
        )

        kwargs = mock_call_sync.call_args.kwargs
        self.assertEqual(kwargs.get("permissions"), perms)
        self.assertEqual(kwargs.get("extra_metadata", {}).get("ticketKey"), "TEST-1")
        self.assertEqual(result["issue"]["key"], "TEST-1")


class TestWorkflowExtractsPermissions(unittest.TestCase):
    """Test that _run_workflow extracts permissions from metadata."""

    def test_permissions_extracted_from_metadata(self):
        """Simulate the permission extraction logic from _run_workflow."""
        metadata = {
            "orchestratorTaskId": "compass-001",
            "sharedWorkspacePath": "/workspace",
            "permissions": {"grant": "development", "actions": [{"action": "repo.clone"}]},
        }
        # Same logic used in _run_workflow
        raw_permissions = metadata.get("permissions")
        self.assertIsInstance(raw_permissions, dict)
        permissions = cast(dict, raw_permissions)
        self.assertEqual(permissions["grant"], "development")

    def test_no_permissions_when_not_dict(self):
        metadata = {"orchestratorTaskId": "compass-001", "permissions": "invalid"}
        permissions = metadata.get("permissions") if isinstance(metadata.get("permissions"), dict) else None
        self.assertIsNone(permissions)

    def test_no_permissions_when_missing(self):
        metadata = {"orchestratorTaskId": "compass-001"}
        permissions = metadata.get("permissions") if isinstance(metadata.get("permissions"), dict) else None
        self.assertIsNone(permissions)


class TestAndroidSdkPreparation(unittest.TestCase):
    def test_resolve_android_sdk_dir_prefers_android_home(self):
        from android.app import _resolve_android_sdk_dir

        with patch.dict(os.environ, {"ANDROID_HOME": "/opt/android-sdk", "ANDROID_SDK_ROOT": "/tmp/other"}, clear=False):
            self.assertEqual(_resolve_android_sdk_dir(), "/opt/android-sdk")

    def test_prepare_android_local_properties_writes_sdk_dir(self):
        from android.app import _prepare_android_local_properties

        messages: list[str] = []
        with tempfile.TemporaryDirectory() as temp_dir, patch.dict(os.environ, {"ANDROID_HOME": "/opt/android-sdk"}, clear=False):
            sdk_dir = _prepare_android_local_properties(temp_dir, messages.append)

            self.assertEqual(sdk_dir, "/opt/android-sdk")
            with open(os.path.join(temp_dir, "local.properties"), encoding="utf-8") as fh:
                self.assertEqual(fh.read().strip(), "sdk.dir=/opt/android-sdk")
            self.assertTrue(any("Prepared Android SDK local.properties" in item for item in messages))


if __name__ == "__main__":
    unittest.main()
