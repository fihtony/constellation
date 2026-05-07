"""Tests for compass/completeness.py and compass/office_routing.py."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from compass.completeness import (
    build_completeness_follow_up_message,
    derive_task_card_status,
    extract_pr_evidence_from_artifacts,
    extract_team_lead_completeness_issues,
)
from compass.office_routing import (
    build_output_target_question,
    build_write_permission_question,
    can_defer_office_path_existence_check,
    is_containerized,
    path_within_base,
    resume_office_clarification,
    validate_office_target_paths,
)


# ---------------------------------------------------------------------------
# compass_completeness — extract_pr_evidence_from_artifacts
# ---------------------------------------------------------------------------

class TestExtractPrEvidence(unittest.TestCase):
    def test_returns_empty_when_no_artifacts(self):
        self.assertEqual(extract_pr_evidence_from_artifacts([]), {})
        self.assertEqual(extract_pr_evidence_from_artifacts(None), {})  # type: ignore[arg-type]

    def test_extracts_prUrl_from_metadata(self):
        artifacts = [
            {"metadata": {"prUrl": "https://github.com/org/repo/pull/7", "branch": "feature/PROJ-1", "jiraInReview": True}}
        ]
        result = extract_pr_evidence_from_artifacts(artifacts)
        self.assertEqual(result["url"], "https://github.com/org/repo/pull/7")
        self.assertEqual(result["branch"], "feature/PROJ-1")
        self.assertTrue(result["jiraInReview"])

    def test_falls_back_to_url_field(self):
        artifacts = [{"metadata": {"url": "https://bitbucket.example.com/pr/42", "branch": "dev/fix"}}]
        result = extract_pr_evidence_from_artifacts(artifacts)
        self.assertEqual(result["url"], "https://bitbucket.example.com/pr/42")

    def test_skips_artifact_with_no_pr_url(self):
        artifacts = [
            {"metadata": {"capability": "team-lead.task.analyze"}},
            {"metadata": {"prUrl": "https://github.com/org/repo/pull/9", "branch": "feat/X", "jiraInReview": False}},
        ]
        result = extract_pr_evidence_from_artifacts(artifacts)
        self.assertEqual(result["url"], "https://github.com/org/repo/pull/9")
        self.assertFalse(result["jiraInReview"])


# ---------------------------------------------------------------------------
# compass_completeness — extract_team_lead_completeness_issues
# ---------------------------------------------------------------------------

class TestExtractCompletenessIssues(unittest.TestCase):
    def _write_json(self, workspace: str, relative_path: str, payload: dict) -> None:
        full_path = Path(workspace, relative_path)
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(json.dumps(payload), encoding="utf-8")

    def test_review_passed_returns_no_issues(self):
        artifacts = [{"metadata": {"capability": "team-lead.task.analyze", "reviewPassed": True}}]
        issues = extract_team_lead_completeness_issues("", artifacts)
        self.assertEqual(issues, [])

    def test_review_failed_returns_issue(self):
        artifacts = [{"metadata": {"capability": "team-lead.task.analyze", "reviewPassed": False}}]
        issues = extract_team_lead_completeness_issues("", artifacts)
        self.assertIn("Team Lead review did not pass.", issues)

    def test_validation_checkpoint_returns_no_issues(self):
        artifacts = [{"metadata": {"capability": "team-lead.task.analyze", "validationCheckpoint": True, "reviewPassed": False}}]
        issues = extract_team_lead_completeness_issues("", artifacts)
        self.assertEqual(issues, [])

    def test_max_cycles_reached_returns_no_issues(self):
        artifacts = [{"metadata": {"capability": "team-lead.task.analyze", "reviewMaxCyclesReached": True, "reviewPassed": False}}]
        issues = extract_team_lead_completeness_issues("", artifacts)
        self.assertEqual(issues, [])

    def test_missing_pr_detected_when_repo_in_workspace(self):
        with tempfile.TemporaryDirectory() as workspace:
            self._write_json(workspace, "team-lead/plan.json", {"target_repo_url": "https://github.com/x/y"})
            self._write_json(workspace, "team-lead/stage-summary.json", {})
            artifacts = [{"metadata": {"capability": "team-lead.task.analyze"}}]
            issues = extract_team_lead_completeness_issues(workspace, artifacts)
        self.assertIn("Pull request URL is missing from execution agent artifacts.", issues)
        self.assertIn("Branch name is missing from execution agent artifacts.", issues)

    def test_pr_in_artifacts_clears_issues(self):
        with tempfile.TemporaryDirectory() as workspace:
            self._write_json(workspace, "team-lead/plan.json", {"target_repo_url": "https://github.com/x/y"})
            artifacts = [
                {"metadata": {"capability": "team-lead.task.analyze"}},
                {"metadata": {"prUrl": "https://github.com/x/y/pull/3", "branch": "feat/task", "jiraInReview": True}},
            ]
            issues = extract_team_lead_completeness_issues(workspace, artifacts)
        self.assertEqual(issues, [])


# ---------------------------------------------------------------------------
# compass_completeness — build_completeness_follow_up_message
# ---------------------------------------------------------------------------

class TestBuildFollowUpMessage(unittest.TestCase):
    def test_appends_issues_to_text(self):
        original = {"parts": [{"text": "Implement PROJ-1"}], "metadata": {}}
        issues = ["Pull request URL is missing.", "Branch is missing."]
        result = build_completeness_follow_up_message(original, issues, revision_cycle=1)
        text = result["parts"][0]["text"]
        self.assertIn("revision 1", text)
        self.assertIn("Pull request URL is missing.", text)
        self.assertEqual(result["metadata"]["compassCompletenessRevision"], 1)
        self.assertEqual(result["metadata"]["completenessIssues"], issues)

    def test_does_not_mutate_original(self):
        original = {"parts": [{"text": "Task text"}], "metadata": {}}
        _ = build_completeness_follow_up_message(original, ["Issue A"], 2)
        self.assertEqual(original["parts"][0]["text"], "Task text")


# ---------------------------------------------------------------------------
# compass_completeness — derive_task_card_status
# ---------------------------------------------------------------------------

class TestDeriveTaskCardStatus(unittest.TestCase):
    def test_working_returns_in_progress(self):
        kind, label = derive_task_card_status("TASK_STATE_WORKING", {})
        self.assertEqual(kind, "in_progress")
        self.assertEqual(label, "In Progress")

    def test_input_required(self):
        kind, label = derive_task_card_status("TASK_STATE_INPUT_REQUIRED", {})
        self.assertEqual(kind, "waiting_for_info")

    def test_completed_without_pr(self):
        kind, label = derive_task_card_status("TASK_STATE_COMPLETED", {})
        self.assertEqual(kind, "completed")
        self.assertEqual(label, "Completed")

    def test_completed_with_pr(self):
        kind, label = derive_task_card_status("TASK_STATE_COMPLETED", {"url": "https://github.com/x/y/pull/1"})
        self.assertEqual(label, "Completed / PR Raised")

    def test_completed_with_pr_and_jira_in_review(self):
        kind, label = derive_task_card_status(
            "TASK_STATE_COMPLETED",
            {"url": "https://github.com/x/y/pull/1", "jiraInReview": True},
        )
        self.assertEqual(label, "Completed / In Review")

    def test_failed_states(self):
        for state in ("TASK_STATE_FAILED", "FAILED", "NO_CAPABLE_AGENT", "POLICY_DENIED"):
            kind, _ = derive_task_card_status(state, {})
            self.assertEqual(kind, "failed", msg=f"Expected 'failed' for state {state}")


# ---------------------------------------------------------------------------
# compass_office_routing — path helpers
# ---------------------------------------------------------------------------

class TestPathHelpers(unittest.TestCase):
    def test_path_within_base_returns_true(self):
        with tempfile.TemporaryDirectory() as base:
            child = os.path.join(base, "subdir", "file.txt")
            Path(child).parent.mkdir(parents=True, exist_ok=True)
            Path(child).write_text("x", encoding="utf-8")
            self.assertTrue(path_within_base(child, base))

    def test_path_within_base_returns_false(self):
        with tempfile.TemporaryDirectory() as base, tempfile.TemporaryDirectory() as other:
            child = os.path.join(other, "file.txt")
            Path(child).write_text("x", encoding="utf-8")
            self.assertFalse(path_within_base(child, base))

    def test_can_defer_outside_container(self):
        with mock.patch("compass.office_routing.is_containerized", return_value=False):
            self.assertFalse(can_defer_office_path_existence_check("/abs/path/file.txt"))

    def test_can_defer_inside_container_absolute(self):
        with mock.patch("compass.office_routing.is_containerized", return_value=True):
            self.assertTrue(can_defer_office_path_existence_check("/abs/path/file.txt"))

    def test_can_defer_inside_container_relative_is_false(self):
        with mock.patch("compass.office_routing.is_containerized", return_value=True):
            self.assertFalse(can_defer_office_path_existence_check("relative/path"))


# ---------------------------------------------------------------------------
# compass_office_routing — validate_office_target_paths
# ---------------------------------------------------------------------------

class TestValidateOfficePaths(unittest.TestCase):
    def test_rejects_relative_path(self):
        paths, error = validate_office_target_paths(["docs/report.txt"])
        self.assertEqual(paths, [])
        self.assertIn("Path must be absolute", error)

    def test_rejects_nonexistent_path_outside_container(self):
        with mock.patch("compass.office_routing.is_containerized", return_value=False):
            paths, error = validate_office_target_paths(["/nonexistent/path/file.txt"])
        self.assertEqual(paths, [])
        self.assertIn("Path does not exist", error)

    def test_defers_existence_check_inside_container(self):
        with mock.patch("compass.office_routing.is_containerized", return_value=True):
            paths, error = validate_office_target_paths(["/nonexistent/host/file.txt"])
        self.assertEqual(paths, ["/nonexistent/host/file.txt"])
        self.assertEqual(error, "")

    def test_rejects_path_outside_whitelist(self):
        with tempfile.TemporaryDirectory() as allowed, tempfile.TemporaryDirectory() as other:
            outside = Path(other, "report.txt")
            outside.write_text("x", encoding="utf-8")
            paths, error = validate_office_target_paths([str(outside)], allowed_base_paths=[allowed])
        self.assertEqual(paths, [])
        self.assertIn("outside OFFICE_ALLOWED_BASE_PATHS", error)

    def test_accepts_path_inside_whitelist(self):
        with tempfile.TemporaryDirectory() as allowed:
            inside = Path(allowed, "report.txt")
            inside.write_text("x", encoding="utf-8")
            paths, error = validate_office_target_paths([str(inside)], allowed_base_paths=[allowed])
        # realpath resolves macOS /var → /private/var; compare resolved paths
        self.assertEqual(paths, [os.path.realpath(str(inside))])
        self.assertEqual(error, "")

    def test_deduplicates_paths(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir, "file.txt")
            f.write_text("x", encoding="utf-8")
            paths, _ = validate_office_target_paths([str(f), str(f)])
        self.assertEqual(len(paths), 1)

    def test_empty_input_returns_empty(self):
        paths, error = validate_office_target_paths([])
        self.assertEqual(paths, [])
        self.assertEqual(error, "")


# ---------------------------------------------------------------------------
# compass_office_routing — question builders
# ---------------------------------------------------------------------------

class TestQuestionBuilders(unittest.TestCase):
    def test_output_target_question_mentions_both_options(self):
        question = build_output_target_question(["/path/to/file.xlsx"])
        self.assertIn("[A]", question)
        self.assertIn("[B]", question)
        self.assertIn("/path/to/file.xlsx", question)

    def test_write_permission_question_asks_yes_no(self):
        question = build_write_permission_question(["/path/to/dir"])
        self.assertIn("yes", question.lower())
        self.assertIn("/path/to/dir", question)


# ---------------------------------------------------------------------------
# compass_office_routing — resume_office_clarification
# ---------------------------------------------------------------------------

class TestResumeOfficeClarification(unittest.TestCase):
    def _make_ctx(self, step, **extra):
        base = {
            "kind": "office",
            "awaitingStep": step,
            "requestedCapability": "office.data.analyze",
            "targetPaths": ["/Users/example/data.csv"],
        }
        base.update(extra)
        return base

    def test_output_mode_workspace_dispatches(self):
        ctx = self._make_ctx("output_mode")
        result = resume_office_clarification(
            ctx, "A", interpret_reply_fn=lambda c, r: {"action": "workspace", "clarification_question": None}
        )
        self.assertEqual(result["action"], "dispatch")
        self.assertEqual(result["router_context"]["outputMode"], "workspace")

    def test_output_mode_inplace_moves_to_confirm_write(self):
        ctx = self._make_ctx("output_mode")
        result = resume_office_clarification(
            ctx, "B", interpret_reply_fn=lambda c, r: {"action": "inplace", "clarification_question": None}
        )
        self.assertEqual(result["action"], "input_required")
        self.assertEqual(result["router_context"]["awaitingStep"], "confirm_write")

    def test_confirm_write_approve_dispatches(self):
        ctx = self._make_ctx("confirm_write", outputMode="inplace")
        result = resume_office_clarification(
            ctx, "yes", interpret_reply_fn=lambda c, r: {"action": "approve", "clarification_question": None}
        )
        self.assertEqual(result["action"], "dispatch")
        self.assertTrue(result["router_context"]["officeWriteApproved"])

    def test_confirm_write_deny_falls_back_to_workspace(self):
        ctx = self._make_ctx("confirm_write", outputMode="inplace")
        result = resume_office_clarification(
            ctx, "no", interpret_reply_fn=lambda c, r: {"action": "deny", "clarification_question": None}
        )
        self.assertEqual(result["action"], "dispatch")
        self.assertEqual(result["router_context"]["outputMode"], "workspace")
        self.assertNotIn("officeWriteApproved", result["router_context"])

    def test_ambiguous_reply_asks_clarification(self):
        ctx = self._make_ctx("output_mode")
        result = resume_office_clarification(
            ctx, "maybe", interpret_reply_fn=lambda c, r: {"action": "unclear", "clarification_question": "Please choose A or B."}
        )
        self.assertEqual(result["action"], "input_required")
        self.assertIn("A or B", result["question"])


if __name__ == "__main__":
    unittest.main()
