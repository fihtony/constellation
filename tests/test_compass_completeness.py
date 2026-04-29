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

    def test_extract_team_lead_completeness_issues_passes_with_required_evidence(self):
        with tempfile.TemporaryDirectory(prefix="compass_complete_") as workspace:
            self._write_json(workspace, "team-lead/stage-summary.json", {
                "analysis": {"jira_ticket_key": "CSTL-1", "target_repo_url": "https://github.com/example/repo"},
            })
            self._write_json(workspace, "team-lead/plan.json", {
                "target_repo_url": "https://github.com/example/repo",
            })
            self._write_json(workspace, "web-agent/pr-evidence.json", {
                "url": "https://github.com/example/repo/pull/13",
                "branch": "feature/CSTL-1_task-0001_1",
            })
            self._write_json(workspace, "web-agent/stage-summary.json", {"buildPassed": True})
            self._write_json(workspace, "web-agent/jira-actions.json", {
                "events": [
                    {"action": "transition", "status": "completed", "targetStatus": "In Progress"},
                    {"action": "transition", "status": "completed", "targetStatus": "In Review"},
                    {"action": "comment", "status": "completed"},
                ]
            })

            task = SimpleNamespace(workspace_path=workspace)
            artifacts = [{"metadata": {"capability": "team-lead.task.analyze", "reviewPassed": True}}]

            issues = compass_app._extract_team_lead_completeness_issues(task, artifacts)

        self.assertEqual(issues, [])

    def test_extract_team_lead_completeness_issues_detects_missing_pr_and_jira_evidence(self):
        with tempfile.TemporaryDirectory(prefix="compass_incomplete_") as workspace:
            self._write_json(workspace, "team-lead/stage-summary.json", {
                "analysis": {"jira_ticket_key": "CSTL-1", "target_repo_url": "https://github.com/example/repo"},
            })
            self._write_json(workspace, "team-lead/plan.json", {
                "target_repo_url": "https://github.com/example/repo",
            })
            self._write_json(workspace, "web-agent/pr-evidence.json", {"branch": ""})
            self._write_json(workspace, "web-agent/stage-summary.json", {"buildPassed": False})
            self._write_json(workspace, "web-agent/jira-actions.json", {
                "events": [
                    {"action": "transition", "status": "completed", "targetStatus": "In Progress"},
                ]
            })

            task = SimpleNamespace(workspace_path=workspace)
            artifacts = [{"metadata": {"capability": "team-lead.task.analyze", "reviewPassed": False}}]

            issues = compass_app._extract_team_lead_completeness_issues(task, artifacts)

        self.assertIn("Team Lead review did not pass.", issues)
        self.assertIn("Pull request URL is missing from web-agent/pr-evidence.json.", issues)
        self.assertIn("Branch name is missing from web-agent/pr-evidence.json.", issues)
        self.assertIn("Web agent reported failing build or test status.", issues)
        self.assertIn("Jira transition to 'In Review' is missing.", issues)
        self.assertIn("Jira PR comment is missing.", issues)

    def test_extract_team_lead_completeness_issues_skips_validation_checkpoint(self):
        with tempfile.TemporaryDirectory(prefix="compass_validation_checkpoint_") as workspace:
            self._write_json(workspace, "team-lead/stage-summary.json", {
                "analysis": {"jira_ticket_key": "CSTL-2", "target_repo_url": "https://github.com/example/repo"},
            })
            self._write_json(workspace, "team-lead/plan.json", {
                "target_repo_url": "https://github.com/example/repo",
            })

            task = SimpleNamespace(workspace_path=workspace)
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


if __name__ == "__main__":
    unittest.main()