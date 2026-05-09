"""Tests for the aggregate_task_card and derive_user_facing_status control tools."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Pre-set ARTIFACT_ROOT so common.task_store doesn't complain at import time
os.environ.setdefault(
    "ARTIFACT_ROOT",
    os.path.join(tempfile.gettempdir(), "constellation-test-artifacts"),
)

from common.tools.control_tools import (
    AggregateTaskCardTool,
    DeriveUserFacingStatusTool,
    configure_control_tools,
)


def _write_json(workspace: str, relative_path: str, payload: dict) -> None:
    full_path = Path(workspace, relative_path)
    full_path.parent.mkdir(parents=True, exist_ok=True)
    full_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class TestAggregateTaskCardTool(unittest.TestCase):
    def setUp(self):
        self.tool = AggregateTaskCardTool()

    def test_returns_complete_when_review_passed(self):
        with tempfile.TemporaryDirectory() as workspace:
            configure_control_tools(task_context={"workspacePath": workspace})
            artifacts = [{"metadata": {"capability": "team-lead.task.analyze", "reviewPassed": True}}]
            result = self.tool.execute({"artifacts": artifacts})

        data = json.loads(result["content"][0]["text"])
        self.assertTrue(data["isComplete"])
        self.assertEqual(data["completenessIssues"], [])

    def test_returns_incomplete_when_review_failed(self):
        with tempfile.TemporaryDirectory() as workspace:
            configure_control_tools(task_context={"workspacePath": workspace})
            artifacts = [{"metadata": {"capability": "team-lead.task.analyze", "reviewPassed": False}}]
            result = self.tool.execute({"artifacts": artifacts})

        data = json.loads(result["content"][0]["text"])
        self.assertFalse(data["isComplete"])
        self.assertIn("Team Lead review did not pass.", data["completenessIssues"])

    def test_extracts_pr_evidence_from_artifacts(self):
        with tempfile.TemporaryDirectory() as workspace:
            configure_control_tools(task_context={"workspacePath": workspace})
            artifacts = [
                {"metadata": {"capability": "team-lead.task.analyze", "reviewPassed": True}},
                {"metadata": {"prUrl": "https://github.com/x/y/pull/7", "branch": "feat/X", "jiraInReview": True}},
            ]
            result = self.tool.execute({"artifacts": artifacts})

        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["prEvidence"]["url"], "https://github.com/x/y/pull/7")
        self.assertEqual(data["prEvidence"]["branch"], "feat/X")
        self.assertTrue(data["prEvidence"]["jiraInReview"])

    def test_reads_current_phase_from_workspace(self):
        with tempfile.TemporaryDirectory() as workspace:
            _write_json(workspace, "team-lead/stage-summary.json", {"currentPhase": "Reviewing PR"})
            configure_control_tools(task_context={"workspacePath": workspace})
            artifacts = [{"metadata": {"capability": "team-lead.task.analyze", "reviewPassed": True}}]
            result = self.tool.execute({"artifacts": artifacts})

        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["currentPhase"], "Reviewing PR")

    def test_handles_missing_repo_url_gracefully(self):
        with tempfile.TemporaryDirectory() as workspace:
            configure_control_tools(task_context={"workspacePath": workspace})
            # reviewPassed unset, no plan.json → no PR issues expected
            artifacts = [{"metadata": {"capability": "team-lead.task.analyze"}}]
            result = self.tool.execute({"artifacts": artifacts})

        data = json.loads(result["content"][0]["text"])
        self.assertTrue(data["isComplete"])

    def test_artifact_count_reported(self):
        with tempfile.TemporaryDirectory() as workspace:
            configure_control_tools(task_context={"workspacePath": workspace})
            artifacts = [
                {"metadata": {"capability": "team-lead.task.analyze", "reviewPassed": True}},
                {"metadata": {"prUrl": "https://github.com/x/y/pull/1", "branch": "b"}},
            ]
            result = self.tool.execute({"artifacts": artifacts})

        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["artifactCount"], 2)


class TestDeriveUserFacingStatusTool(unittest.TestCase):
    def setUp(self):
        self.tool = DeriveUserFacingStatusTool()

    def test_in_progress(self):
        result = self.tool.execute({"task_state": "TASK_STATE_WORKING", "artifacts": []})
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["statusKind"], "in_progress")
        self.assertEqual(data["statusLabel"], "In Progress")

    def test_waiting_for_info(self):
        result = self.tool.execute({"task_state": "TASK_STATE_INPUT_REQUIRED", "artifacts": []})
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["statusKind"], "waiting_for_info")

    def test_failed(self):
        result = self.tool.execute({"task_state": "TASK_STATE_FAILED", "artifacts": []})
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["statusKind"], "failed")

    def test_completed_no_pr(self):
        result = self.tool.execute({"task_state": "TASK_STATE_COMPLETED", "artifacts": []})
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["statusKind"], "completed")
        self.assertEqual(data["statusLabel"], "Completed")

    def test_completed_with_pr(self):
        artifacts = [{"metadata": {"prUrl": "https://github.com/x/y/pull/1", "branch": "feat/X"}}]
        result = self.tool.execute({"task_state": "TASK_STATE_COMPLETED", "artifacts": artifacts})
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["statusLabel"], "Completed / PR Raised")

    def test_completed_with_pr_and_jira_in_review(self):
        artifacts = [{"metadata": {"prUrl": "https://github.com/x/y/pull/1", "jiraInReview": True, "branch": "feat/X"}}]
        result = self.tool.execute({"task_state": "TASK_STATE_COMPLETED", "artifacts": artifacts})
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["statusLabel"], "Completed / In Review")

    def test_pr_evidence_returned_in_result(self):
        artifacts = [{"metadata": {"prUrl": "https://github.com/x/y/pull/5", "branch": "b"}}]
        result = self.tool.execute({"task_state": "TASK_STATE_COMPLETED", "artifacts": artifacts})
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["prEvidence"]["url"], "https://github.com/x/y/pull/5")

    def test_policy_denied_is_failed(self):
        result = self.tool.execute({"task_state": "POLICY_DENIED", "artifacts": []})
        data = json.loads(result["content"][0]["text"])
        self.assertEqual(data["statusKind"], "failed")


if __name__ == "__main__":
    unittest.main()
