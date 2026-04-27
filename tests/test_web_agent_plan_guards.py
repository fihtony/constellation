from __future__ import annotations

import json
import importlib.util
import sys
import types
import tempfile
import unittest
from pathlib import Path
import os
from unittest import mock

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

    def test_team_lead_enriches_analysis_from_jira_raw_payload(self):
        analysis = {"target_repo_url": "", "design_url": None, "needs_design_context": False}
        jira_info = {
            "ticket_key": "CSTL-1",
            "content": json.dumps(
                {
                    "fields": {
                        "customfield_repo": "https://github.com/example/english-study-hub",
                        "customfield_design": "https://www.figma.com/file/abc123/landing-page",
                    }
                },
                ensure_ascii=False,
            ),
        }

        enriched = team_lead_app._enrich_analysis_from_context(analysis, jira_info, None, "")

        self.assertEqual(enriched["target_repo_url"], "https://github.com/example/english-study-hub")
        self.assertEqual(enriched["design_url"], "https://www.figma.com/file/abc123/landing-page")
        self.assertEqual(enriched["design_type"], "figma")
        self.assertTrue(enriched["needs_design_context"])

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

    def test_branch_selection_uses_jira_key_orchestrator_task_id_and_increment(self):
        with mock.patch.object(
            web_app,
            "_list_remote_branches",
            return_value={"feature/CSTL-1_task-0003_1"},
        ):
            branch_name, branch_kind = web_app._select_branch_name(
                "Implement the landing page",
                {"task_summary": "Build the first landing page"},
                ["app/routes.py", "tests/test_landing.py"],
                "CSTL-1",
                "task-0006",
                "https://github.com/example/repo",
                "",
                "/tmp/workspace",
                "task-0003",
            )

        self.assertEqual(branch_kind, "feature")
        self.assertEqual(branch_name, "feature/CSTL-1_task-0003_2")

    def test_docs_and_tests_only_tasks_can_use_chore_branch_without_ticket(self):
        with mock.patch.object(web_app, "_list_remote_branches", return_value=set()):
            branch_name, branch_kind = web_app._select_branch_name(
                "Update the README and add regression tests",
                {"task_summary": "Refresh docs and tests"},
                ["README.md", "tests/test_landing.py"],
                "",
                "task-0006",
                "https://github.com/example/repo",
                "",
                "/tmp/workspace",
                "task-0003",
            )

        self.assertEqual(branch_kind, "chore")
        self.assertEqual(branch_name, "chore/task-0003_1")

    def test_feature_tasks_without_ticket_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "require a Jira ticket"):
            web_app._select_branch_name(
                "Implement a new dashboard",
                {"task_summary": "Build a dashboard"},
                ["app/dashboard.py"],
                "",
                "task-0006",
                "https://github.com/example/repo",
                "",
                "/tmp/workspace",
                "task-0003",
            )

    def test_team_lead_launches_fresh_instance_for_per_task_capability(self):
        with mock.patch.object(
            team_lead_app.agent_directory,
            "find_capability",
            return_value=[
                {
                    "agent_id": "web-agent",
                    "execution_mode": "per-task",
                    "instances": [{"instance_id": "old-1", "status": "idle", "service_url": "http://old"}],
                }
            ],
        ):
            agent_def, instance = team_lead_app._find_agent_instance("web.task.execute")

        self.assertEqual(agent_def["agent_id"], "web-agent")
        self.assertIsNone(instance)

    def test_team_lead_acquire_dev_agent_launches_fresh_per_task_instance(self):
        with mock.patch.object(
            team_lead_app,
            "_find_agent_instance",
            return_value=({"agent_id": "web-agent", "execution_mode": "per-task"}, None),
        ), mock.patch.object(
            team_lead_app.launcher,
            "launch_instance",
            return_value={"container_name": "web-agent-task-1234-abcd"},
        ) as launch_mock, mock.patch.object(
            team_lead_app,
            "_wait_for_idle_instance",
            return_value={
                "instance_id": "web-2",
                "status": "idle",
                "service_url": "http://web-agent-task-1234-abcd:8050",
            },
        ):
            agent_def, instance, service_url = team_lead_app._acquire_dev_agent(
                "web.task.execute",
                "task-1234",
            )

        launch_mock.assert_called_once()
        self.assertEqual(agent_def["agent_id"], "web-agent")
        self.assertEqual(instance["instance_id"], "web-2")
        self.assertEqual(service_url, "http://web-agent-task-1234-abcd:8050")

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
                agent_task_id="web-task-9",
                targetStatus="In Progress",
            )
            web_app._record_jira_action(
                workspace,
                "task-1",
                "CSTL-1",
                "comment",
                "completed",
                agent_task_id="web-task-9",
                commentPreview="Implemented landing page",
            )

            payload = json.loads(
                Path(workspace, "web-agent", "jira-actions.json").read_text(encoding="utf-8")
            )

        self.assertEqual(len(payload["events"]), 2)
        self.assertEqual(payload["events"][0]["action"], "transition")
        self.assertEqual(payload["events"][0]["taskId"], "task-1")
        self.assertEqual(payload["events"][0]["agentTaskId"], "web-task-9")
        self.assertEqual(payload["events"][1]["action"], "comment")
        self.assertEqual(payload["events"][1]["commentPreview"], "Implemented landing page")

    def test_pr_jira_comment_adf_uses_clickable_link(self):
        adf = web_app._build_pr_jira_comment_adf(
            "https://github.com/example/repo/pull/13",
            "feature/CSTL-1_task-0001_1",
            "✅ Build/tests passed",
            [{"path": "requirements.txt"}, {"path": "run.py"}],
            "Landing page implemented.",
        )

        pr_line = adf["content"][1]["content"]
        self.assertEqual(pr_line[1]["text"], "https://github.com/example/repo/pull/13")
        self.assertEqual(
            pr_line[1]["marks"][0]["attrs"]["href"],
            "https://github.com/example/repo/pull/13",
        )

    def test_maybe_schedule_shutdown_after_task_only_when_enabled(self):
        with mock.patch.object(web_app, "_schedule_shutdown") as schedule_mock:
            with mock.patch.dict(os.environ, {"AUTO_STOP_AFTER_TASK": "0"}, clear=False):
                self.assertFalse(web_app._maybe_schedule_shutdown_after_task())
            schedule_mock.assert_not_called()

            with mock.patch.dict(os.environ, {"AUTO_STOP_AFTER_TASK": "1"}, clear=False):
                self.assertTrue(web_app._maybe_schedule_shutdown_after_task())
            schedule_mock.assert_called_once_with(delay_seconds=5)

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

    def test_plan_implementation_repairs_invalid_or_empty_plan_response(self):
        repaired_plan = {
            "plan_summary": "Scaffold a minimal Flask landing page app.",
            "files": [
                {
                    "path": "app.py",
                    "action": "create",
                    "purpose": "Expose the Flask application factory and root route.",
                    "key_logic": "Define create_app and register GET /.",
                    "dependencies": ["flask"],
                },
                {
                    "path": "tests/test_app.py",
                    "action": "create",
                    "purpose": "Cover the Flask landing page behaviour.",
                    "key_logic": "Assert create_app works and GET / returns English Study Hub.",
                    "dependencies": ["pytest", "app.py"],
                },
            ],
            "install_dependencies": ["flask", "pytest"],
            "setup_commands": ["pip install -r requirements.txt"],
            "notes": "Keep the stack on Python 3.12 + Flask.",
        }

        with mock.patch.object(
            web_app,
            "_run_agentic",
            side_effect=[
                '{"plan_summary": "Scaffold a minimal Flask app", "files": [',
                json.dumps(repaired_plan),
            ],
        ) as run_mock:
            plan = web_app._plan_implementation(
                "Implement CSTL-1 in Flask.",
                ["GET / returns English Study Hub."],
                {"backend_framework": "flask", "frontend_framework": "none"},
                "README.md exists",
                "No design context provided.",
            )

        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual([file_info["path"] for file_info in plan["files"]], ["app.py", "tests/test_app.py"])


if __name__ == "__main__":
    unittest.main()