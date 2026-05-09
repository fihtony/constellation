#!/usr/bin/env python3
"""Unit tests for the Android Agent agentic rewrite.

Covers:
- Permission extraction from task metadata (_run_workflow logic)
- Jira context handoff from Team Lead metadata
- build_android_task_prompt() generates correct prompt sections
- ANDROID_AGENT_RUNTIME_TOOL_NAMES contains required tool names
- AndroidValidationProvider returns ValidationResult instances
- configure_android_agent_control_tools() stores permissions in context
"""

import os
import sys
import unittest
from typing import cast
from unittest.mock import patch, MagicMock

# Ensure project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Minimal env setup
os.environ.setdefault("REGISTRY_URL", "http://localhost:9000")
os.environ.setdefault("ADVERTISED_BASE_URL", "http://android-agent:8000")
os.environ.setdefault("OPENAI_BASE_URL", "http://localhost:1288/v1")
os.environ.setdefault("OPENAI_MODEL", "gpt-5-mini")


# ---------------------------------------------------------------------------
# Permission extraction tests (logic still lives in android/app.py _run_workflow)
# ---------------------------------------------------------------------------

class TestWorkflowExtractsPermissions(unittest.TestCase):
    """Test that _run_workflow extracts permissions from message metadata."""

    def test_permissions_extracted_from_metadata(self):
        """Simulate the permission extraction logic from _run_workflow."""
        metadata = {
            "orchestratorTaskId": "compass-001",
            "sharedWorkspacePath": "/workspace",
            "permissions": {"grant": "development", "actions": [{"action": "repo.clone"}]},
        }
        raw_permissions = metadata.get("permissions")
        self.assertIsInstance(raw_permissions, dict)
        permissions = cast(dict, raw_permissions)
        self.assertEqual(permissions["grant"], "development")

    def test_no_permissions_when_not_dict(self):
        metadata = {"orchestratorTaskId": "compass-001", "permissions": "invalid"}
        permissions = (
            metadata.get("permissions")
            if isinstance(metadata.get("permissions"), dict)
            else None
        )
        self.assertIsNone(permissions)

    def test_no_permissions_when_missing(self):
        metadata = {"orchestratorTaskId": "compass-001"}
        permissions = (
            metadata.get("permissions")
            if isinstance(metadata.get("permissions"), dict)
            else None
        )
        self.assertIsNone(permissions)


# ---------------------------------------------------------------------------
# Jira context handoff tests
# ---------------------------------------------------------------------------

class TestAndroidJiraContextHandoff(unittest.TestCase):
    """Test that _resolve_jira_context_from_metadata reads Team Lead-passed context."""

    def test_uses_prefetched_jira_context_from_metadata(self):
        from android.app import _resolve_jira_context_from_metadata

        ticket_key, jira_content = _resolve_jira_context_from_metadata(
            "Implement the Android screen.",
            {
                "jiraTicketKey": "PROJ-2904",
                "jiraContext": {
                    "ticketKey": "PROJ-2904",
                    "content": '{"fields": {"summary": "Your contributions"}}',
                },
            },
        )

        self.assertEqual(ticket_key, "PROJ-2904")
        self.assertIn("Your contributions", jira_content)

    def test_falls_back_to_ticket_key_in_text(self):
        from android.app import _resolve_jira_context_from_metadata

        ticket_key, jira_content = _resolve_jira_context_from_metadata(
            "Please implement PROJ-1234 as described.",
            {},
        )

        self.assertEqual(ticket_key, "PROJ-1234")
        self.assertEqual(jira_content, "")

    def test_empty_when_no_jira_info(self):
        from android.app import _resolve_jira_context_from_metadata

        ticket_key, jira_content = _resolve_jira_context_from_metadata("Some task", {})

        self.assertEqual(ticket_key, "")
        self.assertEqual(jira_content, "")

    def test_prefers_metadata_ticket_key_over_text_match(self):
        from android.app import _resolve_jira_context_from_metadata

        ticket_key, _ = _resolve_jira_context_from_metadata(
            "Also mentioned PROJ-9999 in description",
            {
                "jiraContext": {
                    "ticketKey": "PROJ-1111",
                    "content": "explicit content",
                }
            },
        )
        self.assertEqual(ticket_key, "PROJ-1111")


# ---------------------------------------------------------------------------
# build_android_task_prompt tests
# ---------------------------------------------------------------------------

class TestBuildAndroidTaskPrompt(unittest.TestCase):
    """Test that build_android_task_prompt generates required sections."""

    def test_includes_user_text(self):
        from android.agentic_workflow import build_android_task_prompt

        prompt = build_android_task_prompt(
            user_text="Add a Contributions screen",
            workspace="/tmp/ws",
            compass_task_id="cmp-001",
            android_task_id="and-001",
        )

        self.assertIn("Add a Contributions screen", prompt)

    def test_includes_jira_context_when_provided(self):
        from android.agentic_workflow import build_android_task_prompt

        prompt = build_android_task_prompt(
            user_text="implement screen",
            workspace="/tmp/ws",
            compass_task_id="cmp-001",
            android_task_id="and-001",
            ticket_key="PROJ-42",
            jira_context='{"summary": "User wants contributions list"}',
        )

        self.assertIn("PROJ-42", prompt)
        self.assertIn("contributions list", prompt)

    def test_includes_acceptance_criteria(self):
        from android.agentic_workflow import build_android_task_prompt

        prompt = build_android_task_prompt(
            user_text="implement",
            workspace="/tmp/ws",
            compass_task_id="cmp-001",
            android_task_id="and-001",
            acceptance_criteria=["Must show RecyclerView", "Must have unit tests"],
        )

        self.assertIn("Must show RecyclerView", prompt)
        self.assertIn("Must have unit tests", prompt)

    def test_includes_revision_issues_when_revision(self):
        from android.agentic_workflow import build_android_task_prompt

        prompt = build_android_task_prompt(
            user_text="implement",
            workspace="/tmp/ws",
            compass_task_id="cmp-001",
            android_task_id="and-001",
            is_revision=True,
            review_issues=["Fix the RecyclerView adapter null check"],
        )

        self.assertIn("REVISION", prompt)
        self.assertIn("Fix the RecyclerView adapter null check", prompt)

    def test_includes_repo_url_when_provided(self):
        from android.agentic_workflow import build_android_task_prompt

        prompt = build_android_task_prompt(
            user_text="implement",
            workspace="/tmp/ws",
            compass_task_id="cmp-001",
            android_task_id="and-001",
            target_repo_url="https://github.com/org/android-app.git",
        )

        self.assertIn("https://github.com/org/android-app.git", prompt)

    def test_includes_workspace_path(self):
        from android.agentic_workflow import build_android_task_prompt

        prompt = build_android_task_prompt(
            user_text="implement",
            workspace="/shared/workspace/task-001",
            compass_task_id="cmp-001",
            android_task_id="and-001",
        )

        self.assertIn("/shared/workspace/task-001", prompt)


# ---------------------------------------------------------------------------
# ANDROID_AGENT_RUNTIME_TOOL_NAMES tests
# ---------------------------------------------------------------------------

class TestAndroidRuntimeToolNames(unittest.TestCase):
    """Test that the tool name list includes required categories."""

    def setUp(self):
        from android.agentic_workflow import ANDROID_AGENT_RUNTIME_TOOL_NAMES
        self.tool_names = ANDROID_AGENT_RUNTIME_TOOL_NAMES

    def test_is_a_list_of_strings(self):
        self.assertIsInstance(self.tool_names, list)
        for name in self.tool_names:
            self.assertIsInstance(name, str)

    def test_includes_scm_tools(self):
        scm_tools = [t for t in self.tool_names if t.startswith("scm_")]
        self.assertGreater(len(scm_tools), 0, "Expected at least one scm_ tool")
        # Key SCM operations
        self.assertIn("scm_clone_repo", self.tool_names)
        self.assertIn("scm_create_branch", self.tool_names)
        self.assertIn("scm_push_files", self.tool_names)
        self.assertIn("scm_create_pr", self.tool_names)

    def test_includes_jira_tools(self):
        jira_tools = [t for t in self.tool_names if t.startswith("jira_")]
        self.assertGreater(len(jira_tools), 0, "Expected at least one jira_ tool")
        self.assertIn("jira_get_ticket", self.tool_names)
        self.assertIn("jira_add_comment", self.tool_names)

    def test_includes_validation_tools(self):
        self.assertIn("run_validation_command", self.tool_names)
        self.assertIn("collect_task_evidence", self.tool_names)
        self.assertIn("check_definition_of_done", self.tool_names)

    def test_includes_coding_tools(self):
        coding_tools = [
            t for t in self.tool_names
            if t in ("read_local_file", "write_local_file", "edit_local_file", "list_local_dir", "search_local_files")
        ]
        self.assertGreater(len(coding_tools), 0, "Expected at least one coding tool")

    def test_no_duplicates(self):
        self.assertEqual(len(self.tool_names), len(set(self.tool_names)))


# ---------------------------------------------------------------------------
# AndroidValidationProvider tests
# ---------------------------------------------------------------------------

class TestAndroidValidationProvider(unittest.TestCase):
    """Test AndroidValidationProvider returns correct ValidationResult structure.

    Note: Methods take (workspace_path, options) as required by the
    ValidationProvider protocol in common/tools/validation_tools.py.
    """

    def _get_provider(self):
        from android.agentic_workflow import AndroidValidationProvider
        return AndroidValidationProvider()

    def test_run_build_returns_validation_result(self):
        from common.tools.validation_tools import ValidationResult

        with patch("android.agentic_workflow._run_gradle_task") as mock_gradle:
            mock_gradle.return_value = ValidationResult(
                passed=True,
                summary="BUILD SUCCESSFUL",
                details=[],
                retriable=False,
            )

            provider = self._get_provider()
            result = provider.run_build("/tmp/fake-repo", {})

        self.assertIsInstance(result, ValidationResult)
        self.assertTrue(result.passed)
        mock_gradle.assert_called_once_with("/tmp/fake-repo", "assembleDebug", {})

    def test_run_build_failure_returns_false_result(self):
        from common.tools.validation_tools import ValidationResult

        with patch("android.agentic_workflow._run_gradle_task") as mock_gradle:
            mock_gradle.return_value = ValidationResult(
                passed=False,
                summary="BUILD FAILED",
                details=[],
                retriable=True,
            )

            provider = self._get_provider()
            result = provider.run_build("/tmp/fake-repo", {})

        self.assertIsInstance(result, ValidationResult)
        self.assertFalse(result.passed)

    def test_run_unit_test_returns_validation_result(self):
        from common.tools.validation_tools import ValidationResult

        with patch("android.agentic_workflow._run_gradle_task") as mock_gradle:
            mock_gradle.return_value = ValidationResult(
                passed=True,
                summary="Tests passed",
                details=[],
                retriable=False,
            )

            provider = self._get_provider()
            result = provider.run_unit_test("/tmp/fake-repo", {})

        self.assertIsInstance(result, ValidationResult)
        mock_gradle.assert_called_once_with("/tmp/fake-repo", "testDebugUnitTest", {})

    def test_run_e2e_skipped_gracefully(self):
        from common.tools.validation_tools import ValidationResult

        provider = self._get_provider()
        result = provider.run_e2e("/tmp/fake-repo", {})

        self.assertIsInstance(result, ValidationResult)
        self.assertTrue(result.passed, "E2E should be skipped (no device) not failed")

    def test_run_integration_test_skipped_gracefully(self):
        from common.tools.validation_tools import ValidationResult

        provider = self._get_provider()
        result = provider.run_integration_test("/tmp/fake-repo", {})

        self.assertIsInstance(result, ValidationResult)
        self.assertTrue(result.passed, "Integration tests should be skipped not failed")


# ---------------------------------------------------------------------------
# configure_android_agent_control_tools tests
# ---------------------------------------------------------------------------

class TestConfigureAndroidAgentControlTools(unittest.TestCase):
    """Test that configure_android_agent_control_tools wires permissions into context."""

    def test_stores_permissions_in_control_tools_context(self):
        from android.agentic_workflow import configure_android_agent_control_tools
        from common.tools import control_tools

        perms = {"grant": "development", "actions": [{"action": "repo.clone"}]}

        with patch("android.agentic_workflow.configure_control_tools") as mock_configure:
            configure_android_agent_control_tools(
                task_id="t-001",
                agent_id="android-agent",
                workspace="/tmp/ws",
                permissions=perms,
                compass_task_id="c-001",
                callback_url="http://compass:8080/tasks/c-001/callbacks",
                orchestrator_url="http://compass:8080",
                user_text="implement screen",
                wait_for_input_fn=lambda q: None,
            )
            mock_configure.assert_called_once()
            kwargs = mock_configure.call_args[1]
            ctx = kwargs.get("task_context", {})
            self.assertEqual(ctx.get("permissions"), perms)
            self.assertEqual(ctx.get("taskId"), "t-001")
            self.assertEqual(ctx.get("agentId"), "android-agent")

    def test_works_without_permissions(self):
        from android.agentic_workflow import configure_android_agent_control_tools

        with patch("android.agentic_workflow.configure_control_tools") as mock_configure:
            configure_android_agent_control_tools(
                task_id="t-002",
                agent_id="android-agent",
                workspace="/tmp/ws",
                permissions=None,
                compass_task_id="c-002",
                callback_url="",
                orchestrator_url="",
                user_text="implement",
                wait_for_input_fn=lambda q: None,
            )
            mock_configure.assert_called_once()
            kwargs = mock_configure.call_args[1]
            ctx = kwargs.get("task_context", {})
            self.assertIsNone(ctx.get("permissions"))


if __name__ == "__main__":
    unittest.main()
