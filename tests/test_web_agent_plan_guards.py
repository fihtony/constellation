from __future__ import annotations

import json
import importlib.util
import sys
import types
import tempfile
import unittest
from pathlib import Path

from web import app as web_app


_TEAM_LEAD_DIR = Path(__file__).resolve().parents[1] / "team-lead"
_TEAM_LEAD_PROMPTS_SPEC = importlib.util.spec_from_file_location("team_lead.prompts", _TEAM_LEAD_DIR / "prompts.py")
team_lead_prompts = importlib.util.module_from_spec(_TEAM_LEAD_PROMPTS_SPEC)
assert _TEAM_LEAD_PROMPTS_SPEC and _TEAM_LEAD_PROMPTS_SPEC.loader
_TEAM_LEAD_PROMPTS_SPEC.loader.exec_module(team_lead_prompts)

team_lead_package = types.ModuleType("team_lead")
team_lead_package.__path__ = [str(_TEAM_LEAD_DIR)]
team_lead_package.prompts = team_lead_prompts
sys.modules.setdefault("team_lead", team_lead_package)
sys.modules.setdefault("team_lead.prompts", team_lead_prompts)

_TEAM_LEAD_APP_PATH = Path(__file__).resolve().parents[1] / "team-lead" / "app.py"
_TEAM_LEAD_SPEC = importlib.util.spec_from_file_location("team_lead_app", _TEAM_LEAD_APP_PATH)
team_lead_app = importlib.util.module_from_spec(_TEAM_LEAD_SPEC)
assert _TEAM_LEAD_SPEC and _TEAM_LEAD_SPEC.loader
_TEAM_LEAD_SPEC.loader.exec_module(team_lead_app)


class WebAgentPlanGuardsTests(unittest.TestCase):
    def test_team_lead_extracts_and_enforces_python_flask_constraints(self):
        constraints = team_lead_app._extract_tech_stack_constraints(
            "Implement the landing page",
            "tech stack: python 3.12, flask",
        )

        plan = team_lead_app._enforce_plan_constraints(
            {
                "dev_instruction": "Implement the feature in the target repository.",
                "acceptance_criteria": ["Landing page renders successfully."],
            },
            constraints,
        )

        self.assertEqual(constraints["language"], "python")
        self.assertEqual(constraints["backend_framework"], "flask")
        self.assertIn("HARD TECH STACK CONSTRAINTS", plan["dev_instruction"])
        self.assertIn("Python 3.12 and Flask", plan["acceptance_criteria"][0])

    def test_revision_metadata_preserves_constraints_and_workflow_requirements(self):
        metadata = team_lead_app._build_dev_task_metadata(
            dev_capability="web.task.execute",
            compass_task_id="task-1",
            team_lead_task_id="task-1",
            workspace="/tmp/workspace",
            target_repo_url="https://github.com/example/repo",
            tech_stack_constraints={
                "language": "python",
                "python_version": "3.12",
                "backend_framework": "flask",
            },
            acceptance_criteria=["Tests pass."],
            requires_tests=True,
            is_revision=True,
            revision_cycle=2,
            review_issues=["Re-run pytest and attach evidence."],
        )

        self.assertEqual(metadata["targetRepoUrl"], "https://github.com/example/repo")
        self.assertEqual(metadata["techStackConstraints"]["backend_framework"], "flask")
        self.assertEqual(metadata["acceptanceCriteria"], ["Tests pass."])
        self.assertTrue(metadata["requiresTests"])
        self.assertTrue(metadata["isRevision"])
        self.assertEqual(metadata["revisionCycle"], 2)
        self.assertEqual(metadata["reviewIssues"], ["Re-run pytest and attach evidence."])
        self.assertIn("transition the Jira ticket to 'In Progress'", metadata["devWorkflowInstructions"])

    def test_web_analysis_constraints_override_frontend_guess(self):
        analysis = {
            "scope": "frontend_only",
            "frontend_framework": "react",
            "backend_framework": "none",
            "language": "typescript",
        }

        updated = web_app._apply_tech_stack_constraints(
            analysis,
            {"language": "python", "python_version": "3.12", "backend_framework": "flask"},
        )

        self.assertEqual(updated["language"], "python")
        self.assertEqual(updated["backend_framework"], "flask")
        self.assertEqual(updated["frontend_framework"], "none")
        self.assertEqual(updated["scope"], "fullstack")

    def test_nextjs_plan_drops_spa_and_operational_files(self):
        files = [
            {"path": "pages/index.tsx", "action": "create"},
            {"path": "src/components/Hero.tsx", "action": "create"},
            {"path": "src/App.tsx", "action": "modify"},
            {"path": "src/routes.tsx", "action": "modify"},
            {"path": "src/pages/LandingPage.tsx", "action": "create"},
            {"path": "src/pages/__tests__/LandingPage.test.tsx", "action": "create"},
            {"path": "artifacts/ci-log.txt", "action": "create"},
            {"path": "PR description (pull request body)", "action": "create"},
            {"path": "STEP-0-DETECT.md", "action": "create"},
        ]

        kept, removed = web_app._sanitize_plan_files(
            files,
            {"frontend_framework": "nextjs"},
            ["Resolve framework duplication. If Next.js is chosen: remove SPA react-router files."],
        )

        self.assertEqual(
            [file_info["path"] for file_info in kept],
            ["pages/index.tsx", "src/components/Hero.tsx"],
        )
        removed_paths = {item["path"] for item in removed}
        self.assertIn("src/App.tsx", removed_paths)
        self.assertIn("src/routes.tsx", removed_paths)
        self.assertIn("src/pages/LandingPage.tsx", removed_paths)
        self.assertIn("src/pages/__tests__/LandingPage.test.tsx", removed_paths)
        self.assertIn("artifacts/ci-log.txt", removed_paths)
        self.assertIn("PR description (pull request body)", removed_paths)
        self.assertIn("STEP-0-DETECT.md", removed_paths)

    def test_react_plan_drops_nextjs_files(self):
        files = [
            {"path": "src/App.tsx", "action": "modify"},
            {"path": "src/routes.tsx", "action": "modify"},
            {"path": "src/pages/LandingPage.tsx", "action": "create"},
            {"path": "pages/index.tsx", "action": "create"},
            {"path": "app/page.tsx", "action": "create"},
            {"path": "src/pages/__tests__/LandingPage.next.test.tsx", "action": "create"},
        ]

        kept, removed = web_app._sanitize_plan_files(
            files,
            {"frontend_framework": "react"},
            ["If React Router is chosen: remove Next.js pages/app routes."],
        )

        self.assertEqual(
            [file_info["path"] for file_info in kept],
            ["src/App.tsx", "src/routes.tsx", "src/pages/LandingPage.tsx"],
        )
        removed_paths = {item["path"] for item in removed}
        self.assertIn("pages/index.tsx", removed_paths)
        self.assertIn("app/page.tsx", removed_paths)
        self.assertIn("src/pages/__tests__/LandingPage.next.test.tsx", removed_paths)

    def test_jira_actions_are_appended_to_workspace_evidence(self):
        with tempfile.TemporaryDirectory(prefix="web_agent_jira_") as workspace:
            web_app._record_jira_action(
                workspace,
                "task-1",
                "CSTL-1",
                "transition",
                "completed",
                targetStatus="In Progress",
            )
            web_app._record_jira_action(
                workspace,
                "task-1",
                "CSTL-1",
                "comment",
                "completed",
                commentPreview="Implemented landing page",
            )

            payload = json.loads(
                Path(workspace, "web-agent", "jira-actions.json").read_text(encoding="utf-8")
            )

        self.assertEqual(len(payload["events"]), 2)
        self.assertEqual(payload["events"][0]["action"], "transition")
        self.assertEqual(payload["events"][1]["action"], "comment")
        self.assertEqual(payload["events"][1]["commentPreview"], "Implemented landing page")

    def test_pr_evidence_is_merged_across_updates(self):
        with tempfile.TemporaryDirectory(prefix="web_agent_pr_") as workspace:
            web_app._save_pr_evidence(
                workspace,
                taskId="task-1",
                repoUrl="https://github.com/example/repo",
                title="feat: landing page",
                body="Implements the landing page and tests.",
            )
            web_app._save_pr_evidence(
                workspace,
                branch="feature/task-1",
                url="https://github.com/example/repo/pull/123",
                buildPassed=True,
            )

            payload = json.loads(
                Path(workspace, "web-agent", "pr-evidence.json").read_text(encoding="utf-8")
            )

        self.assertEqual(payload["taskId"], "task-1")
        self.assertEqual(payload["title"], "feat: landing page")
        self.assertEqual(payload["url"], "https://github.com/example/repo/pull/123")
        self.assertEqual(payload["branch"], "feature/task-1")
        self.assertTrue(payload["buildPassed"])


if __name__ == "__main__":
    unittest.main()