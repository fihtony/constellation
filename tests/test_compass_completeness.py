from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from compass import app as compass_app


class CompassCompletenessTests(unittest.TestCase):
    def _write_json(self, workspace: str, relative_path: str, payload: dict) -> None:
        full_path = Path(workspace, relative_path)
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def test_extract_team_lead_completeness_issues_passes_when_review_passed(self):
        """reviewPassed=True in Team Lead summary artifact → no issues (trust the review)."""
        with tempfile.TemporaryDirectory(prefix="compass_complete_") as workspace:
            task = SimpleNamespace(workspace_path=workspace, artifacts=[])
            # All evidence comes via A2A artifacts; workspace files are irrelevant here.
            artifacts = [{"metadata": {"capability": "team-lead.task.analyze", "reviewPassed": True}}]

            issues = compass_app._extract_team_lead_completeness_issues(task, artifacts)

        self.assertEqual(issues, [])

    def test_extract_team_lead_completeness_issues_detects_failed_review(self):
        """reviewPassed=False → 'Team Lead review did not pass.' issue raised."""
        with tempfile.TemporaryDirectory(prefix="compass_incomplete_") as workspace:
            task = SimpleNamespace(workspace_path=workspace, artifacts=[])
            # reviewPassed=False and no PR evidence in artifacts
            artifacts = [{"metadata": {"capability": "team-lead.task.analyze", "reviewPassed": False}}]

            issues = compass_app._extract_team_lead_completeness_issues(task, artifacts)

        self.assertIn("Team Lead review did not pass.", issues)

    def test_extract_team_lead_completeness_issues_detects_missing_pr_in_artifacts(self):
        """When reviewPassed is absent (fallback path), missing PR URL in artifacts is detected."""
        with tempfile.TemporaryDirectory(prefix="compass_no_pr_") as workspace:
            self._write_json(workspace, "team-lead/plan.json", {
                "target_repo_url": "https://github.com/example/repo",
            })
            self._write_json(workspace, "team-lead/stage-summary.json", {
                "analysis": {"target_repo_url": "https://github.com/example/repo"},
            })
            # reviewPassed not set and no prUrl in artifacts → PR missing issue
            task = SimpleNamespace(workspace_path=workspace, artifacts=[])
            artifacts = [{"metadata": {"capability": "team-lead.task.analyze"}}]

            issues = compass_app._extract_team_lead_completeness_issues(task, artifacts)

        self.assertIn("Pull request URL is missing from execution agent artifacts.", issues)
        self.assertIn("Branch name is missing from execution agent artifacts.", issues)

    def test_extract_team_lead_completeness_issues_pr_found_in_artifacts(self):
        """When reviewPassed is absent but prUrl is in artifacts, no PR issues raised."""
        with tempfile.TemporaryDirectory(prefix="compass_pr_ok_") as workspace:
            self._write_json(workspace, "team-lead/plan.json", {
                "target_repo_url": "https://github.com/example/repo",
            })
            self._write_json(workspace, "team-lead/stage-summary.json", {
                "analysis": {"target_repo_url": "https://github.com/example/repo"},
            })
            task = SimpleNamespace(workspace_path=workspace, artifacts=[])
            artifacts = [
                {"metadata": {"capability": "team-lead.task.analyze"}},
                {"metadata": {
                    "capability": "android.task.execute",
                    "prUrl": "https://bitbucket.example.com/pull-requests/42",
                    "url": "https://bitbucket.example.com/pull-requests/42",
                    "branch": "agent/feature/PROJ-1_task-001",
                }},
            ]

            issues = compass_app._extract_team_lead_completeness_issues(task, artifacts)

        # No PR-related issues
        self.assertNotIn("Pull request URL is missing from execution agent artifacts.", issues)
        self.assertNotIn("Branch name is missing from execution agent artifacts.", issues)

    def test_extract_team_lead_completeness_issues_skips_validation_checkpoint(self):
        """validationCheckpoint=True → no issues regardless of other state."""
        with tempfile.TemporaryDirectory(prefix="compass_validation_checkpoint_") as workspace:
            task = SimpleNamespace(workspace_path=workspace, artifacts=[])
            artifacts = [
                {
                    "metadata": {
                        "capability": "team-lead.task.analyze",
                        "reviewPassed": True,
                        "validationCheckpoint": True,
                    }
                }
            ]

            issues = compass_app._extract_team_lead_completeness_issues(task, artifacts)

        self.assertEqual(issues, [])

    def test_extract_team_lead_completeness_issues_skips_max_cycles_reached(self):
        """reviewMaxCyclesReached=True → skip retry regardless of reviewPassed value."""
        with tempfile.TemporaryDirectory(prefix="compass_max_cycles_") as workspace:
            task = SimpleNamespace(workspace_path=workspace, artifacts=[])
            artifacts = [
                {
                    "metadata": {
                        "capability": "team-lead.task.analyze",
                        "reviewPassed": False,
                        "reviewMaxCyclesReached": True,
                    }
                }
            ]

            issues = compass_app._extract_team_lead_completeness_issues(task, artifacts)

        self.assertEqual(issues, [])


if __name__ == "__main__":
    unittest.main()
