"""Regression tests for Web Agent planning/build guards and prompt boundaries.

Phase 6 removed Team Lead's legacy Python-side planning helpers and state-machine
tests. Runtime-first Team Lead coverage now lives in tests/test_team_lead_agentic.py
and tests/test_agent_runtime_adoption.py. This module keeps the Web Agent guard
coverage plus prompt-boundary assertions that still apply after the refactor.
"""

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
from web import prompts as web_prompts
from common.runtime.connect_agent.adapter import DEFAULT_AGENTIC_SYSTEM


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


class WebAgentPlanGuardsTests(unittest.TestCase):

    def test_web_sync_agent_forwards_permissions_snapshot(self):
        captured: dict = {}
        permissions = {
            "taskType": "development",
            "allowed": [],
            "denied": [],
            "fallback": "deny_and_escalate",
        }

        def fake_send(agent_url: str, message: dict) -> dict:
            captured["agent_url"] = agent_url
            captured["message"] = message
            return {
                "id": "scm-task-1",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [],
            }

        with mock.patch.object(web_app, "_resolve_agent_service_url", return_value="http://scm:8020"), mock.patch.object(
            web_app,
            "_a2a_send",
            side_effect=fake_send,
        ):
            result = web_app._call_sync_agent(
                "scm.branch.list",
                "List branches in https://github.com/example/repo",
                "task-1",
                "/tmp/workspace",
                "compass-task-1",
                permissions=permissions,
            )

        self.assertEqual(result["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(captured["agent_url"], "http://scm:8020")
        self.assertEqual(captured["message"]["metadata"]["permissions"], permissions)

    def test_web_jira_request_json_transports_permissions_for_get_and_post(self):
        captured_requests: list[dict] = []
        permissions = {
            "taskType": "development",
            "allowed": [],
            "denied": [],
            "fallback": "deny_and_escalate",
        }

        def fake_call_sync(capability, message_text, task_id, workspace_path, compass_task_id, permissions=None, extra_metadata=None):
            captured_requests.append(
                {
                    "capability": capability,
                    "message_text": message_text,
                    "task_id": task_id,
                    "workspace": workspace_path,
                    "compass_task_id": compass_task_id,
                    "permissions": permissions,
                    "extra_metadata": extra_metadata or {},
                }
            )
            if capability == "jira.ticket.fetch":
                return {
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [
                        {"name": "jira-raw-payload", "parts": [{"text": json.dumps({"key": "PROJ-2"})}]}
                    ],
                }
            return {
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [
                    {"name": "jira-comment-add", "parts": [{"text": json.dumps({"result": "created"})}]}
                ],
            }

        with mock.patch.object(web_app, "_call_sync_agent", side_effect=fake_call_sync):
            web_app._jira_request_json(
                "jira.ticket.fetch",
                "GET",
                "/jira/tickets/PROJ-2",
                permissions=permissions,
                workspace="/tmp/workspace",
                task_id="task-1",
                compass_task_id="compass-1",
            )
            web_app._jira_request_json(
                "jira.comment.add",
                "POST",
                "/jira/comments/PROJ-2",
                payload={"text": "hello"},
                permissions=permissions,
                workspace="/tmp/workspace",
                task_id="task-1",
                compass_task_id="compass-1",
            )

        self.assertEqual(captured_requests[0]["capability"], "jira.ticket.fetch")
        self.assertEqual(captured_requests[0]["permissions"], permissions)
        self.assertEqual(captured_requests[0]["extra_metadata"]["ticketKey"], "PROJ-2")
        self.assertEqual(captured_requests[0]["compass_task_id"], "compass-1")

        self.assertEqual(captured_requests[1]["capability"], "jira.comment.add")
        self.assertEqual(captured_requests[1]["permissions"], permissions)
        self.assertEqual(captured_requests[1]["extra_metadata"]["ticketKey"], "PROJ-2")
        self.assertEqual(captured_requests[1]["extra_metadata"]["commentText"], "hello")

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
            return_value={"feature/PROJ-1_task-0003_1"},
        ):
            branch_name, branch_kind = web_app._select_branch_name(
                "Implement the landing page",
                {"task_summary": "Build the first landing page"},
                ["app/routes.py", "tests/test_landing.py"],
                "PROJ-1",
                "task-0006",
                "https://github.com/example/repo",
                "",
                "/tmp/workspace",
                "task-0003",
            )

        self.assertEqual(branch_kind, "feature")
        self.assertEqual(branch_name, "feature/PROJ-1_task-0003_2")

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

    def test_web_agent_resolves_ticket_key_from_metadata_when_instruction_lacks_one(self):
        ticket_key = web_app._resolve_ticket_key(
            "Implement the dashboard in the target repository.",
            {"jiraTicketKey": "PROJ-2903"},
        )

        self.assertEqual(ticket_key, "PROJ-2903")

    def test_web_agent_resolves_prefetched_jira_context_from_team_lead_metadata(self):
        ticket_key, jira_content = web_app._resolve_jira_context_from_metadata(
            "Implement the dashboard in the target repository.",
            {
                "jiraTicketKey": "PROJ-2903",
                "jiraContext": {
                    "ticketKey": "PROJ-2903",
                    "content": '{"fields": {"summary": "Implement dashboard"}}',
                },
            },
        )

        self.assertEqual(ticket_key, "PROJ-2903")
        self.assertIn("Implement dashboard", jira_content)

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

    def test_normalize_plan_path_converts_common_dotfile_aliases(self):
        self.assertEqual(web_app._normalize_plan_path("gitignore"), ".gitignore")
        self.assertEqual(web_app._normalize_plan_path("nvmrc"), ".nvmrc")
        self.assertEqual(web_app._normalize_plan_path("config/dockerignore"), "config/.dockerignore")

    def test_sanitize_plan_files_drops_non_example_env_files(self):
        files = [
            {"path": "client/.env", "action": "create"},
            {"path": "server/.env.example", "action": "create"},
            {"path": "gitignore", "action": "create"},
        ]

        kept, removed = web_app._sanitize_plan_files(
            files,
            {"frontend_framework": "react"},
            [],
        )

        self.assertEqual(
            [file_info["path"] for file_info in kept],
            ["server/.env.example", ".gitignore"],
        )
        self.assertEqual(removed[0]["path"], "client/.env")

    def test_jira_actions_are_appended_to_workspace_evidence(self):
        with tempfile.TemporaryDirectory(prefix="web_agent_jira_") as workspace:
            web_app._record_jira_action(
                workspace,
                "task-1",
                "PROJ-1",
                "transition",
                "completed",
                agent_task_id="web-task-9",
                targetStatus="In Progress",
            )
            web_app._record_jira_action(
                workspace,
                "task-1",
                "PROJ-1",
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
            "feature/PROJ-1_task-0001_1",
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
        # _apply_task_exit_rule replaces _maybe_schedule_shutdown_after_task.
        # With AUTO_STOP not set and rule type "immediate", shutdown is still skipped.
        with mock.patch.object(web_app, "_schedule_shutdown") as schedule_mock:
            with mock.patch.dict(os.environ, {"AUTO_STOP_AFTER_TASK": "0"}, clear=False):
                # "auto_stop" rule type is only honoured when AUTO_STOP_AFTER_TASK=1
                web_app._apply_task_exit_rule("task-x", {"type": "auto_stop"})
                # The background thread runs immediately but shouldn't schedule shutdown
            import time
            time.sleep(0.1)  # allow the daemon thread to run
            schedule_mock.assert_not_called()

            with mock.patch.dict(os.environ, {"AUTO_STOP_AFTER_TASK": "1"}, clear=False):
                web_app._apply_task_exit_rule("task-y", {"type": "auto_stop"})
            time.sleep(0.1)
            schedule_mock.assert_called_once()

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
                "Implement PROJ-1 in Flask.",
                ["GET / returns English Study Hub."],
                {"backend_framework": "flask", "frontend_framework": "none"},
                "README.md exists",
                "No design context provided.",
            )

        self.assertEqual(run_mock.call_count, 2)
        self.assertEqual([file_info["path"] for file_info in plan["files"]], ["app.py", "tests/test_app.py"])

    def test_plan_implementation_uses_extended_timeout_budget(self):
        valid_plan = {
            "plan_summary": "Create the React/Express implementation plan.",
            "files": [
                {
                    "path": "client/src/App.jsx",
                    "action": "create",
                    "purpose": "Render the main page.",
                    "key_logic": "Create the React entry component.",
                    "dependencies": ["react"],
                }
            ],
            "install_dependencies": ["react"],
            "setup_commands": ["npm install"],
            "notes": "Use the existing repository.",
        }

        with mock.patch.object(
            web_app,
            "_run_agentic",
            return_value=json.dumps(valid_plan),
        ) as run_mock:
            plan = web_app._plan_implementation(
                "Implement PROJ-4 in React/Express.",
                ["Render /study."],
                {"backend_framework": "express", "frontend_framework": "react"},
                "README.md exists",
                "Figma reference is rate-limited.",
            )

        self.assertEqual(plan["files"][0]["path"], "client/src/App.jsx")
        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(run_mock.call_args.kwargs.get("timeout"), web_app.PLAN_TIMEOUT_SECONDS)
        self.assertEqual(run_mock.call_args.kwargs.get("max_tokens"), web_app.PLAN_MAX_TOKENS)

    def test_web_agent_detects_node_build_steps_from_root_package_json(self):
        with tempfile.TemporaryDirectory(prefix="web_node_build_") as build_dir:
            Path(build_dir, "package.json").write_text(
                json.dumps(
                    {
                        "scripts": {
                            "test": "jest --coverage",
                            "build": "vite build",
                        },
                        "devDependencies": {
                            "jest": "^29.0.0",
                        },
                    }
                ),
                encoding="utf-8",
            )

            steps = web_app._detect_node_build_steps(build_dir)

        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0]["cwd"], build_dir)
        self.assertEqual(steps[0]["cmd"][:2], ["npm", "test"])
        self.assertIn("--coverage", steps[0]["cmd"])
        self.assertEqual(steps[1]["cmd"], ["npm", "run", "build"])

    def test_web_agent_installs_written_node_dependencies_for_generated_package_manifests(self):
        with tempfile.TemporaryDirectory(prefix="web_written_npm_") as build_dir:
            Path(build_dir, "package.json").write_text(json.dumps({"name": "root"}), encoding="utf-8")
            client_dir = Path(build_dir, "client")
            client_dir.mkdir(parents=True, exist_ok=True)
            Path(client_dir, "package.json").write_text(json.dumps({"name": "client"}), encoding="utf-8")
            server_dir = Path(build_dir, "server")
            server_dir.mkdir(parents=True, exist_ok=True)
            Path(server_dir, "package.json").write_text(json.dumps({"name": "server"}), encoding="utf-8")

            calls: list[str] = []

            def fake_run(*_args, **kwargs):
                calls.append(kwargs["cwd"])
                return mock.Mock(returncode=0)

            with mock.patch.object(web_app.subprocess, "run", side_effect=fake_run):
                web_app._install_written_node_dependencies(build_dir, lambda _message: None)

        self.assertEqual(calls, [build_dir, str(client_dir), str(server_dir)])

    def test_web_agent_sanitizes_plan_dependency_annotations_before_npm_install(self):
        calls: list[dict] = []

        def fake_run(command, **_kwargs):
            calls.append({"command": command, "cwd": _kwargs.get("cwd")})
            return mock.Mock(returncode=0)

        with tempfile.TemporaryDirectory(prefix="web_plan_deps_") as build_dir, mock.patch.object(
            web_app.subprocess,
            "run",
            side_effect=fake_run,
        ):
            web_app._install_plan_dependencies(
                ["react", "@vitejs/plugin-react (optional)", "`cross-env`"],
                "javascript",
                lambda _message: None,
                cwd=build_dir,
            )

        self.assertEqual(
            calls,
            [
                {
                    "command": ["npm", "install", "--save", "react", "@vitejs/plugin-react", "cross-env"],
                    "cwd": build_dir,
                }
            ],
        )

    def test_web_agent_reinstalls_node_dependencies_after_manifest_fix(self):
        with tempfile.TemporaryDirectory(prefix="web_retry_npm_") as build_dir:
            Path(build_dir, "package.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")

            install_calls: list[str] = []
            log_messages: list[str] = []

            with mock.patch.object(web_app, "_ensure_local_python_env", return_value=None), mock.patch.object(
                web_app,
                "_run_build",
                side_effect=[
                    (False, "MISSING DEP  Can not find dependency 'jsdom'"),
                    (True, "build ok"),
                ],
            ), mock.patch.object(web_app, "_read_source_files", return_value=[]), mock.patch.object(
                web_app,
                "_run_agentic",
                return_value='{"diagnosis":"missing jsdom","fixes":[{"path":"package.json","content":"{\\"name\\":\\"demo\\",\\"devDependencies\\":{\\"jsdom\\":\\"^24.0.0\\"}}"}]}',
            ), mock.patch.object(
                web_app,
                "_install_written_node_dependencies",
                side_effect=lambda path, _log_fn: install_calls.append(path),
            ):
                passed, output, attempts = web_app._build_and_test_with_recovery(
                    build_dir,
                    "Implement landing page",
                    "javascript",
                    log_messages.append,
                )

        self.assertTrue(passed)
        self.assertEqual(output, "build ok")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(install_calls, [build_dir])

    def test_web_agent_auto_installs_missing_node_dependency_before_llm_fix(self):
        with tempfile.TemporaryDirectory(prefix="web_missing_dep_") as build_dir:
            Path(build_dir, "package.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")

            npm_calls: list[dict] = []
            agentic_mock = mock.Mock(return_value="{}")

            def fake_run(command, **kwargs):
                npm_calls.append({"command": command, "cwd": kwargs.get("cwd")})
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(web_app, "_ensure_local_python_env", return_value=None), mock.patch.object(
                web_app,
                "_run_build",
                side_effect=[
                    (False, "Error: Failed to load url prop-types (resolved id: prop-types) in src/components/Landing.jsx"),
                    (True, "build ok"),
                ],
            ), mock.patch.object(web_app.subprocess, "run", side_effect=fake_run), mock.patch.object(
                web_app,
                "_run_agentic",
                agentic_mock,
            ):
                passed, output, attempts = web_app._build_and_test_with_recovery(
                    build_dir,
                    "Implement landing page",
                    "javascript",
                    lambda _message: None,
                )

        self.assertTrue(passed)
        self.assertEqual(output, "build ok")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(
            npm_calls,
            [{"command": ["npm", "install", "--save", "prop-types"], "cwd": build_dir}],
        )
        agentic_mock.assert_not_called()

    def test_web_agent_final_retry_auto_installs_missing_node_dependency(self):
        with tempfile.TemporaryDirectory(prefix="web_final_missing_dep_") as build_dir:
            Path(build_dir, "package.json").write_text(json.dumps({"name": "demo"}), encoding="utf-8")

            npm_calls: list[dict] = []

            def fake_run(command, **kwargs):
                npm_calls.append({"command": command, "cwd": kwargs.get("cwd")})
                return mock.Mock(returncode=0, stdout="", stderr="")

            with mock.patch.object(web_app, "_ensure_local_python_env", return_value=None), mock.patch.object(
                web_app,
                "_run_build",
                side_effect=[
                    (False, "ReferenceError: expect is not defined"),
                    (False, "ReferenceError: expect is not defined"),
                    (False, "Error: Failed to load url prop-types (resolved id: prop-types) in src/components/Landing.jsx"),
                    (True, "build ok"),
                ],
            ), mock.patch.object(web_app.subprocess, "run", side_effect=fake_run), mock.patch.object(
                web_app,
                "_read_source_files",
                return_value=[],
            ), mock.patch.object(
                web_app,
                "_run_agentic",
                return_value="{}",
            ), mock.patch.object(
                web_app,
                "_parse_json_from_llm",
                side_effect=[
                    {"diagnosis": "set up vitest expect", "fixes": [{"path": "src/test-setup.js", "content": "export {};"}]},
                    {"diagnosis": "keep vitest setup", "fixes": [{"path": "src/test-setup.js", "content": "export const ready = true;"}]},
                ],
            ):
                passed, output, attempts = web_app._build_and_test_with_recovery(
                    build_dir,
                    "Implement landing page",
                    "javascript",
                    lambda _message: None,
                )

        self.assertTrue(passed)
        self.assertEqual(output, "build ok")
        self.assertEqual(len(attempts), 4)
        self.assertEqual(attempts[-1]["attempt"], web_app.MAX_BUILD_RETRIES + 1)
        self.assertEqual(
            npm_calls,
            [{"command": ["npm", "install", "--save", "prop-types"], "cwd": build_dir}],
        )

    def test_web_agent_auto_fixes_vitest_jest_dom_setup(self):
        with tempfile.TemporaryDirectory(prefix="web_vitest_fix_") as build_dir:
            Path(build_dir, "package.json").write_text(
                json.dumps(
                    {
                        "name": "demo",
                        "scripts": {"test": "vitest --run"},
                        "devDependencies": {
                            "vitest": "^1.6.1",
                            "@testing-library/jest-dom": "^6.0.0",
                        },
                        "vitest": {"environment": "jsdom"},
                    }
                ),
                encoding="utf-8",
            )
            test_file = Path(build_dir, "src", "components", "__tests__", "Landing.test.jsx")
            test_file.parent.mkdir(parents=True, exist_ok=True)
            test_file.write_text(
                "import * as matchers from '@testing-library/jest-dom/matchers';\n"
                "// Register jest-dom matchers with Vitest's expect\n"
                "expect.extend(matchers);\n"
                "import { describe, it, expect } from 'vitest';\n"
                "describe('Landing', () => { it('works', () => expect(true).toBe(true)); });\n",
                encoding="utf-8",
            )

            fixed = web_app._auto_fix_vitest_jest_dom_setup(build_dir, lambda _message: None)

            package_json = json.loads(Path(build_dir, "package.json").read_text(encoding="utf-8"))
            updated_test = test_file.read_text(encoding="utf-8")
            setup_file = Path(build_dir, "vitest.setup.js").read_text(encoding="utf-8")

        self.assertTrue(fixed)
        self.assertEqual(package_json["vitest"]["setupFiles"], "./vitest.setup.js")
        self.assertNotIn("@testing-library/jest-dom/matchers", updated_test)
        self.assertNotIn("expect.extend(matchers)", updated_test)
        self.assertEqual(setup_file, "import '@testing-library/jest-dom/vitest';\n")

    def test_web_agent_detects_client_dev_launch_plan_for_ui_screenshot(self):
        with tempfile.TemporaryDirectory(prefix="web_ui_launch_") as build_dir:
            client_dir = Path(build_dir, "client")
            client_dir.mkdir(parents=True, exist_ok=True)
            Path(client_dir, "package.json").write_text(
                json.dumps(
                    {
                        "scripts": {
                            "dev": "vite",
                            "build": "vite build",
                        }
                    }
                ),
                encoding="utf-8",
            )

            plan = web_app._detect_ui_launch_plan(
                build_dir,
                {"frontend_framework": "react"},
                43123,
            )

        self.assertIsNotNone(plan)
        self.assertEqual(plan["cwd"], str(client_dir))
        self.assertEqual(
            plan["cmd"],
            ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", "43123"],
        )
        self.assertIn("http://127.0.0.1:43123/", plan["urls"])

    def test_web_agent_registers_generated_artifact_for_commit(self):
        with tempfile.TemporaryDirectory(prefix="web_artifact_commit_") as temp_dir:
            clone_dir = Path(temp_dir, "repo")
            clone_dir.mkdir(parents=True, exist_ok=True)
            source_path = Path(temp_dir, "implementation-screenshot.png")
            source_path.write_bytes(b"png-data")
            generated_files: list[dict] = []

            registered = web_app._register_generated_artifact(
                str(clone_dir),
                generated_files,
                str(source_path),
                "docs/evidence/implementation-screenshot-desktop.png",
                lambda _message: None,
            )

            artifact_exists = Path(
                clone_dir,
                "docs/evidence/implementation-screenshot-desktop.png",
            ).is_file()

        self.assertTrue(registered)
        self.assertTrue(artifact_exists)
        self.assertEqual(generated_files[0]["path"], "docs/evidence/implementation-screenshot-desktop.png")

    def test_web_agent_registers_runtime_repo_artifacts_for_commit(self):
        with tempfile.TemporaryDirectory(prefix="web_runtime_artifacts_") as temp_dir:
            clone_dir = Path(temp_dir, "repo")
            artifact_dir = clone_dir / "artifacts" / "figma" / "file123" / "1_470"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            screenshot_path = artifact_dir / "design_desktop.png"
            screenshot_path.write_bytes(b"png-data")
            generated_files: list[dict] = []

            registered_count = web_app._register_runtime_repo_artifacts(
                str(clone_dir),
                generated_files,
                ["artifacts/figma"],
                lambda _message: None,
            )

        self.assertEqual(registered_count, 1)
        self.assertEqual(generated_files[0]["path"], "artifacts/figma/file123/1_470/design_desktop.png")

    def test_web_agent_requires_shared_workspace_for_repo_tasks(self):
        with self.assertRaisesRegex(RuntimeError, "Shared workspace path is required"):
            web_app._require_shared_workspace_for_repo_task(
                "https://github.com/example-org/example-app",
                "",
            )

    def test_web_agent_rejects_clone_outside_shared_workspace(self):
        with tempfile.TemporaryDirectory(prefix="web_workspace_") as workspace, tempfile.TemporaryDirectory(prefix="web_clone_") as outside:
            with self.assertRaisesRegex(RuntimeError, "must stay inside the shared workspace"):
                web_app._ensure_clone_path_in_workspace(workspace, outside)

class AgentPromptBoundaryTests(unittest.TestCase):
    def test_connect_agent_default_prompt_stays_runtime_generic(self):
        lowered = DEFAULT_AGENTIC_SYSTEM.lower()

        self.assertIn("task-specific system prompt", lowered)
        self.assertNotIn("tailwind", lowered)
        self.assertNotIn("react", lowered)
        self.assertNotIn("figma", lowered)
        self.assertNotIn("jira comment", lowered)

    def test_team_lead_prompts_enforce_planning_and_repo_clone_boundary(self):
        plan_lower = team_lead_prompts.PLAN_SYSTEM.lower()
        review_lower = team_lead_prompts.REVIEW_SYSTEM.lower()

        self.assertIn("you do not write implementation code yourself", plan_lower)
        self.assertIn("clone the target repository", plan_lower)
        self.assertIn("shared workspace", plan_lower)
        self.assertIn("missing scm evidence is a delivery failure", review_lower)

    def test_web_prompts_require_cloned_repo_and_explicit_section_surfaces(self):
        analyze_lower = web_prompts.ANALYZE_SYSTEM.lower()
        plan_lower = web_prompts.PLAN_SYSTEM.lower()
        codegen_lower = web_prompts.CODEGEN_SYSTEM.lower()
        design_lower = web_prompts.DESIGN_COMPARE_SYSTEM.lower()

        self.assertIn("team lead", analyze_lower)
        self.assertIn("cloned repository tree", plan_lower)
        self.assertIn("headers, title/hero wrappers, footers", codegen_lower)
        self.assertIn("never apply black (#000000)", codegen_lower)
        self.assertIn("unexpected black/default backgrounds", design_lower)


if __name__ == "__main__":
    unittest.main()