from __future__ import annotations

import json
import importlib.util
import sys
import types
import tempfile
import unittest
from pathlib import Path
import os
import threading
import time
from unittest import mock

from web import app as web_app
from web import prompts as web_prompts
from common.task_store import TaskStore
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

_TEAM_LEAD_APP_PATH = Path(__file__).resolve().parents[1] / "team-lead" / "app.py"
_TEAM_LEAD_SPEC = importlib.util.spec_from_file_location("team_lead_app", _TEAM_LEAD_APP_PATH)
team_lead_app = importlib.util.module_from_spec(_TEAM_LEAD_SPEC)
assert _TEAM_LEAD_SPEC and _TEAM_LEAD_SPEC.loader
_TEAM_LEAD_SPEC.loader.exec_module(team_lead_app)


class WebAgentPlanGuardsTests(unittest.TestCase):
    def setUp(self):
        self._original_task_store = team_lead_app.task_store
        team_lead_app.task_store = TaskStore()
        with team_lead_app._TASK_CONTEXTS_LOCK:
            team_lead_app._TASK_CONTEXTS.clear()
        with team_lead_app._INPUT_EVENTS_LOCK:
            team_lead_app._INPUT_EVENTS.clear()
        with team_lead_app._CALLBACK_LOCK:
            team_lead_app._CALLBACK_EVENTS.clear()
            team_lead_app._CALLBACK_RESULTS.clear()

    def tearDown(self):
        team_lead_app.task_store = self._original_task_store
        with team_lead_app._TASK_CONTEXTS_LOCK:
            team_lead_app._TASK_CONTEXTS.clear()
        with team_lead_app._INPUT_EVENTS_LOCK:
            team_lead_app._INPUT_EVENTS.clear()
        with team_lead_app._CALLBACK_LOCK:
            team_lead_app._CALLBACK_EVENTS.clear()
            team_lead_app._CALLBACK_RESULTS.clear()

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
            "ticket_key": "PROJ-1",
            "content": json.dumps(
                {
                    "fields": {
                        "customfield_repo": "https://github.com/example/example-app",
                        "customfield_design": "https://www.figma.com/file/abc123/landing-page",
                    }
                },
                ensure_ascii=False,
            ),
        }

        enriched = team_lead_app._enrich_analysis_from_context(analysis, jira_info, None, "")

        self.assertEqual(enriched["target_repo_url"], "https://github.com/example/example-app")
        self.assertEqual(enriched["design_url"], "https://www.figma.com/file/abc123/landing-page")
        self.assertEqual(enriched["design_type"], "figma")
        self.assertTrue(enriched["needs_design_context"])

    def test_team_lead_requires_jira_ticket_for_implementation_requests(self):
        with self.assertRaisesRegex(RuntimeError, "A Jira ticket is required"):
            team_lead_app._ensure_jira_ticket_for_workflow(
                {
                    "task_type": "feature",
                    "platform": "web",
                    "jira_ticket_key": None,
                    "summary": "Implement a dashboard",
                },
                "Implement a new dashboard without a ticket.",
            )

    def test_team_lead_recovers_jira_ticket_key_from_request_text(self):
        updated = team_lead_app._ensure_jira_ticket_for_workflow(
            {
                "task_type": "feature",
                "platform": "web",
                "jira_ticket_key": None,
                "needs_jira_fetch": False,
            },
            "Implement https://example.atlassian.net/browse/PROJ-2 in the target repo.",
        )

        self.assertEqual(updated["jira_ticket_key"], "PROJ-2")
        self.assertTrue(updated["needs_jira_fetch"])

    def test_team_lead_builds_design_fetch_request_from_page_name(self):
        capability, message_text, page_name = team_lead_app._build_design_fetch_request(
            {
                "design_url": "https://www.figma.com/design/abc123/Example-App",
                "design_type": "figma",
                "design_page_name": "Practice Quiz",
            }
        )

        self.assertEqual(capability, "figma.page.fetch")
        self.assertEqual(page_name, "Practice Quiz")
        self.assertIn("page: Practice Quiz", message_text)

    def test_team_lead_skips_redundant_figma_fetch_for_equivalent_node_labels(self):
        ctx = team_lead_app._TaskContext()
        ctx.design_info = {
            "url": "https://www.figma.com/design/mockDesign123?node-id=1-470",
            "type": "figma",
            "content": "Summary (fetch failed — rate limited)",
            "page_name": "node_id=1-470",
            "fetchedBy": "figma.page.fetch",
            "request": "Fetch design from https://www.figma.com/design/mockDesign123?node-id=1-470",
        }
        analysis = {
            "design_url": "https://www.figma.com/design/mockDesign123?node-id=1-470",
            "design_type": "figma",
            "design_page_name": "node 1-470",
        }
        action = {
            "action": "fetch_agent_context",
            "capability": "figma.page.fetch",
            "message": "Fetch design from https://www.figma.com/design/mockDesign123?node-id=1-470 page: node 1-470",
            "reason": "Need the design specification before planning.",
        }

        with mock.patch.object(team_lead_app, "_call_sync_agent") as sync_mock:
            progressed = team_lead_app._execute_gather_action(
                action,
                analysis,
                ctx,
                team_lead_task_id="task-1",
                workspace="",
                compass_task_id="compass-task-1",
                log_fn=lambda _message: None,
            )

        self.assertFalse(progressed)
        sync_mock.assert_not_called()

    def test_team_lead_requests_tech_stack_confirmation_when_ticket_is_ambiguous(self):
        updated = team_lead_app._apply_tech_stack_confirmation_policy(
            {
                "task_type": "feature",
                "platform": "web",
                "jira_ticket_key": "PROJ-2",
                "missing_info": [],
                "question_for_user": None,
            },
            {},
            "Implement PROJ-2.",
        )

        self.assertIn("confirmed web tech stack", updated["missing_info"])
        self.assertIn("tech stack", updated["question_for_user"].lower())

    def test_team_lead_filters_defaultable_web_styling_questions(self):
        ctx = team_lead_app._TaskContext()
        ctx.jira_info = {"ticket_key": "PROJ-4", "content": "Jira ticket content."}
        ctx.repo_info = {
            "repo_url": "https://github.com/example-org/example-app",
            "content": "Repo uses React app routing and CSS modules.",
        }

        unresolved = team_lead_app._filter_unresolved_missing_info(
            {
                "platform": "web",
                "missing_info": [
                    "Preferred styling approach or tooling (plain CSS, SASS, Tailwind, CSS-in-JS, component library) if any.",
                    "Exact analytics event names.",
                ],
            },
            ctx,
        )

        self.assertEqual(unresolved, ["Exact analytics event names."])

    def test_team_lead_platform_stays_unknown_without_direct_evidence(self):
        updated = team_lead_app._apply_platform_evidence_policy(
            {"platform": "web"},
            "Implement PROJ-2 from the Jira ticket.",
            "",
        )

        self.assertEqual(updated["platform"], "unknown")

    def test_team_lead_platform_infers_android_from_repo_signals(self):
        updated = team_lead_app._apply_platform_evidence_policy(
            {"platform": "unknown"},
            "Implement PROJ-4.",
            "app/build.gradle.kts uses compileSdk 36 and Kotlin 2.0.21 with Jetpack Compose.",
        )

        self.assertEqual(updated["platform"], "android")

    def test_team_lead_clears_tech_stack_question_after_user_confirms_stack(self):
        updated = team_lead_app._apply_tech_stack_confirmation_policy(
            {
                "task_type": "feature",
                "platform": "web",
                "jira_ticket_key": "PROJ-2",
                "missing_info": ["preferred framework"],
                "question_for_user": "Which framework should I use?",
            },
            {"language": "python", "python_version": "3.12", "backend_framework": "flask"},
            "Please use Python 3.12 and Flask.",
        )

        self.assertEqual(updated["missing_info"], [])
        self.assertIsNone(updated["question_for_user"])

    def test_team_lead_prioritizes_stack_question_after_empty_repo_search(self):
        ctx = team_lead_app._TaskContext()
        ctx.repo_info = {
            "repo_url": "",
            "content": "",
            "request": "queries=[\"PROJ-2\",\"lesson-library\"]",
        }

        analysis = {
            "task_type": "feature",
            "platform": "web",
            "target_repo_url": None,
            "question_for_user": "The Jira ticket does not specify the web tech stack. Please confirm the stack to use.",
        }

        self.assertTrue(team_lead_app._should_prioritize_stack_question(analysis, ctx))

    def test_team_lead_same_task_resumes_and_carries_stack_constraints_into_dev_launch(self):
        class StopBeforeDevLaunch(RuntimeError):
            pass

        with tempfile.TemporaryDirectory(prefix="team_lead_resume_") as workspace:
            task = team_lead_app.task_store.create()
            ctx = team_lead_app._TaskContext()
            ctx.compass_task_id = "compass-task-1"
            ctx.compass_callback_url = "http://compass.local/tasks/task-1/callbacks"
            ctx.compass_url = "http://compass.local"
            ctx.shared_workspace_path = workspace
            ctx.user_text = "Implement https://example.atlassian.net/browse/PROJ-2"

            analyze_calls: list[str] = []
            agent_calls: list[tuple[str, str]] = []
            captured_dev_message: dict = {}

            def fake_analyze(user_text: str, additional_info: str = "") -> dict:
                analyze_calls.append(additional_info)
                if len(analyze_calls) == 1:
                    return {
                        "task_type": "feature",
                        "platform": "web",
                        "needs_jira_fetch": True,
                        "jira_ticket_key": "PROJ-2",
                        "needs_design_context": False,
                        "missing_info": [],
                        "question_for_user": None,
                        "summary": "Implement PROJ-2.",
                    }
                if len(analyze_calls) == 2:
                    return {
                        "task_type": "feature",
                        "platform": "web",
                        "needs_jira_fetch": True,
                        "jira_ticket_key": "PROJ-2",
                        "needs_design_context": True,
                        "design_url": "https://www.figma.com/design/abc123/Example-App",
                        "design_type": "figma",
                        "design_page_name": "Practice Quiz",
                        "target_repo_url": "",
                        "missing_info": [],
                        "question_for_user": None,
                        "summary": "Implement PROJ-2 from Figma.",
                    }
                if len(analyze_calls) == 3:
                    return {
                        "task_type": "feature",
                        "platform": "web",
                        "needs_jira_fetch": True,
                        "jira_ticket_key": "PROJ-2",
                        "needs_design_context": True,
                        "design_url": "https://www.figma.com/design/abc123/Example-App",
                        "design_type": "figma",
                        "design_page_name": "Practice Quiz",
                        "target_repo_url": "",
                        "missing_info": [],
                        "question_for_user": None,
                        "summary": "Implement PROJ-2 from Figma.",
                    }

                self.assertIn("python 3.12", additional_info.lower())
                self.assertIn("flask", additional_info.lower())
                return {
                    "task_type": "feature",
                    "platform": "web",
                    "needs_jira_fetch": True,
                    "jira_ticket_key": "PROJ-2",
                    "needs_design_context": True,
                    "design_url": "https://www.figma.com/design/abc123/Example-App",
                    "design_type": "figma",
                    "design_page_name": "Practice Quiz",
                    "target_repo_url": "",
                    "missing_info": [],
                    "question_for_user": None,
                    "summary": "Implement PROJ-2 from Figma in Flask.",
                }

            def fake_plan_information_gathering(_user_text: str, analysis: dict, workflow_ctx, **_kwargs) -> dict:
                if analysis.get("needs_jira_fetch") and workflow_ctx.jira_info is None:
                    return {
                        "pending_tasks": ["Fetch Jira ticket PROJ-2"],
                        "actions": [
                            {
                                "action": "fetch_agent_context",
                                "capability": "jira.ticket.fetch",
                                "message": "Fetch ticket PROJ-2",
                                "reason": "Need the Jira ticket details before planning.",
                            }
                        ],
                    }

                if analysis.get("needs_design_context") and workflow_ctx.design_info is None:
                    capability, message_text, page_name = team_lead_app._build_design_fetch_request(analysis)
                    pending_text = f"Fetch design from {analysis['design_url']}"
                    if page_name:
                        pending_text += f" page: {page_name}"
                    return {
                        "pending_tasks": [pending_text],
                        "actions": [
                            {
                                "action": "fetch_agent_context",
                                "capability": capability,
                                "message": message_text,
                                "reason": "Need the design specification before planning.",
                            }
                        ],
                    }

                if analysis.get("question_for_user"):
                    return {
                        "pending_tasks": [f"Ask user: {analysis['question_for_user']}"],
                        "actions": [
                            {
                                "action": "ask_user",
                                "question": analysis["question_for_user"],
                                "reason": "Need user clarification before planning.",
                            }
                        ],
                    }

                return {
                    "pending_tasks": ["Proceed to implementation planning"],
                    "actions": [
                        {
                            "action": "proceed_to_plan",
                            "reason": "All critical implementation context is available.",
                        }
                    ],
                }

            def fake_call_sync_agent(capability: str, message_text: str, *_args, **_kwargs) -> dict:
                agent_calls.append((capability, message_text))
                if capability == "jira.ticket.fetch":
                    return {
                        "artifacts": [
                            {
                                "parts": [
                                    {
                                        "text": "Ticket content with Figma https://www.figma.com/design/abc123/Example-App and page Practice Quiz."
                                    }
                                ]
                            }
                        ]
                    }
                if capability == "figma.page.fetch":
                    return {"artifacts": [{"parts": [{"text": "Practice Quiz UI spec"}]}]}
                raise AssertionError(f"Unexpected capability: {capability}")

            def fake_create_plan(*_args, **_kwargs) -> dict:
                return {
                    "platform": "web",
                    "dev_capability": "web.task.execute",
                    "target_repo_url": "",
                    "dev_instruction": "Implement the requested flow in Flask.",
                    "acceptance_criteria": ["Practice Quiz screen matches the design."],
                    "requires_tests": True,
                    "test_requirements": "Add integration coverage for the Practice Quiz screen.",
                    "screenshot_requirements": None,
                }

            def fake_acquire_dev_agent(*_args, **_kwargs):
                return (
                    {"agent_id": "web-agent", "execution_mode": "per-task"},
                    {"instance_id": "web-1", "status": "idle", "service_url": "http://web-agent:8050"},
                    "http://web-agent:8050",
                )

            def fake_a2a_send(agent_url: str, message: dict, context_id: str | None = None) -> dict:
                captured_dev_message["agent_url"] = agent_url
                captured_dev_message["context_id"] = context_id
                captured_dev_message["message"] = message
                raise StopBeforeDevLaunch("stop before launching the real web agent")

            with mock.patch.object(team_lead_app, "_analyze_task", side_effect=fake_analyze), mock.patch.object(
                team_lead_app,
                "_call_sync_agent",
                side_effect=fake_call_sync_agent,
            ), mock.patch.object(
                team_lead_app,
                "_plan_information_gathering",
                side_effect=fake_plan_information_gathering,
            ), mock.patch.object(team_lead_app, "_create_plan", side_effect=fake_create_plan), mock.patch.object(
                team_lead_app,
                "_acquire_dev_agent",
                side_effect=fake_acquire_dev_agent,
            ), mock.patch.object(team_lead_app, "_a2a_send", side_effect=fake_a2a_send), mock.patch.object(
                team_lead_app,
                "_notify_compass",
            ), mock.patch.object(team_lead_app, "_report_progress"), mock.patch.object(
                team_lead_app,
                "_generate_summary",
                return_value="workflow intercepted for test",
            ), mock.patch.object(team_lead_app.registry, "mark_instance_busy"), mock.patch.object(
                team_lead_app.registry,
                "mark_instance_idle",
            ):
                worker = threading.Thread(
                    target=team_lead_app._run_workflow,
                    args=(task.task_id, ctx),
                    daemon=True,
                )
                worker.start()

                deadline = time.time() + 5
                while time.time() < deadline:
                    current = team_lead_app.task_store.get(task.task_id)
                    if current and current.state == "TASK_STATE_INPUT_REQUIRED":
                        break
                    time.sleep(0.05)
                else:
                    self.fail("Team Lead never entered TASK_STATE_INPUT_REQUIRED")

                current = team_lead_app.task_store.get(task.task_id)
                self.assertIsNotNone(current)
                self.assertIn("tech stack", current.status_message.lower())
                self.assertIn(
                    ("figma.page.fetch", "Fetch design from https://www.figma.com/design/abc123/Example-App page: Practice Quiz"),
                    agent_calls,
                )

                with team_lead_app._INPUT_EVENTS_LOCK:
                    entry = team_lead_app._INPUT_EVENTS.get(task.task_id)
                    self.assertIsNotNone(entry)
                    entry["info"] = "Use Python 3.12 and Flask."
                    entry["event"].set()

                worker.join(timeout=5)

            metadata = captured_dev_message["message"]["metadata"]
            self.assertEqual(metadata["techStackConstraints"]["language"], "python")
            self.assertEqual(metadata["techStackConstraints"]["python_version"], "3.12")
            self.assertEqual(metadata["techStackConstraints"]["backend_framework"], "flask")

            history_states = [entry["state"] for entry in team_lead_app.task_store.get(task.task_id).history]
            self.assertIn("TASK_STATE_INPUT_REQUIRED", history_states)
            self.assertIn("EXECUTING", history_states)

    def test_team_lead_falls_back_to_user_question_when_fetch_actions_make_no_progress(self):
        with tempfile.TemporaryDirectory(prefix="team_lead_no_progress_") as workspace:
            task = team_lead_app.task_store.create()
            ctx = team_lead_app._TaskContext()
            ctx.compass_task_id = "compass-task-1"
            ctx.compass_callback_url = "http://compass.local/tasks/task-1/callbacks"
            ctx.compass_url = "http://compass.local"
            ctx.shared_workspace_path = workspace
            ctx.user_text = "Implement PROJ-2."
            ctx.original_message = {"metadata": {"stopBeforeDevDispatch": True}}
            ctx.jira_info = {
                "ticket_key": "PROJ-2",
                "content": "Existing Jira ticket content.",
                "request": "Fetch ticket PROJ-2",
            }

            def fake_analyze(_user_text: str, additional_info: str = "") -> dict:
                if not additional_info:
                    return {
                        "task_type": "feature",
                        "platform": "web",
                        "needs_jira_fetch": True,
                        "jira_ticket_key": "PROJ-2",
                        "needs_design_context": False,
                        "missing_info": ["confirmed web tech stack"],
                        "question_for_user": "Please confirm the web tech stack.",
                        "summary": "Implement PROJ-2.",
                    }
                return {
                    "task_type": "feature",
                    "platform": "web",
                    "needs_jira_fetch": True,
                    "jira_ticket_key": "PROJ-2",
                    "needs_design_context": False,
                    "missing_info": [],
                    "question_for_user": None,
                    "summary": "Implement PROJ-2 in Flask.",
                }

            def fake_plan_information_gathering(_user_text: str, _analysis: dict, _workflow_ctx, **_kwargs) -> dict:
                return {
                    "pending_tasks": [
                        "Refresh Jira ticket PROJ-2",
                        "Ask user: Please confirm the web tech stack.",
                    ],
                    "actions": [
                        {
                            "action": "fetch_agent_context",
                            "capability": "jira.ticket.fetch",
                            "message": "Fetch ticket PROJ-2",
                            "reason": "Need the latest Jira ticket content before planning.",
                        }
                    ],
                }

            fake_plan = {
                "platform": "web",
                "dev_capability": "web.task.execute",
                "target_repo_url": "",
                "dev_instruction": "Implement the requested flow in Flask.",
                "acceptance_criteria": ["Practice Quiz screen matches the design."],
                "requires_tests": True,
                "test_requirements": "Add integration coverage for the Practice Quiz screen.",
                "screenshot_requirements": None,
            }

            with mock.patch.object(team_lead_app, "_analyze_task", side_effect=fake_analyze), mock.patch.object(
                team_lead_app,
                "_plan_information_gathering",
                side_effect=fake_plan_information_gathering,
            ), mock.patch.object(
                team_lead_app,
                "_call_sync_agent",
                side_effect=AssertionError("no fetch action should run when it makes no progress"),
            ) as sync_mock, mock.patch.object(
                team_lead_app,
                "_create_plan",
                return_value=fake_plan,
            ), mock.patch.object(team_lead_app, "_notify_compass"), mock.patch.object(
                team_lead_app,
                "_report_progress",
            ), mock.patch.object(
                team_lead_app,
                "_generate_summary",
                return_value="validation checkpoint reached",
            ):
                worker = threading.Thread(
                    target=team_lead_app._run_workflow,
                    args=(task.task_id, ctx),
                    daemon=True,
                )
                worker.start()

                deadline = time.time() + 5
                while time.time() < deadline:
                    current = team_lead_app.task_store.get(task.task_id)
                    if current and current.state == "TASK_STATE_INPUT_REQUIRED":
                        break
                    time.sleep(0.05)
                else:
                    self.fail("Team Lead never fell back to TASK_STATE_INPUT_REQUIRED")

                current = team_lead_app.task_store.get(task.task_id)
                self.assertIsNotNone(current)
                self.assertIn("tech stack", current.status_message.lower())

                with team_lead_app._INPUT_EVENTS_LOCK:
                    entry = team_lead_app._INPUT_EVENTS.get(task.task_id)
                    self.assertIsNotNone(entry)
                    entry["info"] = "Use Python 3.12 and Flask."
                    entry["event"].set()

                worker.join(timeout=5)
                self.assertFalse(worker.is_alive(), "workflow thread did not finish")
                sync_mock.assert_not_called()

            current = team_lead_app.task_store.get(task.task_id)
            self.assertEqual(current.state, "TASK_STATE_COMPLETED")
            history_states = [entry["state"] for entry in current.history]
            self.assertIn("TASK_STATE_INPUT_REQUIRED", history_states)

    def test_team_lead_executes_new_fallback_fetch_when_runtime_plan_repeats_stale_fetches(self):
        with tempfile.TemporaryDirectory(prefix="team_lead_fallback_fetch_") as workspace:
            task = team_lead_app.task_store.create()
            ctx = team_lead_app._TaskContext()
            ctx.compass_task_id = "compass-task-1"
            ctx.compass_callback_url = "http://compass.local/tasks/task-1/callbacks"
            ctx.compass_url = "http://compass.local"
            ctx.shared_workspace_path = workspace
            ctx.user_text = "Implement PROJ-4."
            ctx.additional_info = "Use React with an Express backend."
            ctx.original_message = {"metadata": {"stopBeforeDevDispatch": True}}
            ctx.jira_info = {
                "ticket_key": "PROJ-4",
                "content": "Ticket content with linked Figma design.",
                "request": "Fetch ticket PROJ-4",
            }
            ctx.repo_info = {
                "repo_url": "https://github.com/example-org/example-app",
                "content": "Repository metadata already fetched.",
                "request": "Inspect repository https://github.com/example-org/example-app",
            }

            analysis = {
                "task_type": "feature",
                "platform": "web",
                "needs_jira_fetch": True,
                "jira_ticket_key": "PROJ-4",
                "needs_design_context": True,
                "design_url": "https://www.figma.com/design/abc123/Example-App",
                "design_type": "figma",
                "design_page_name": "Practice Quiz",
                "target_repo_url": "https://github.com/example-org/example-app",
                "missing_info": [],
                "question_for_user": None,
                "summary": "Implement PROJ-4.",
            }
            expected_capability, expected_message, _ = team_lead_app._build_design_fetch_request(analysis)

            def fake_plan_information_gathering(_user_text: str, _analysis: dict, _workflow_ctx, **_kwargs) -> dict:
                return {
                    "pending_tasks": [
                        "Fetch the full Jira issue PROJ-4 (all fields, AC, links, attachments, comments, sprint/epic context).",
                        "Inspect the target repository example-org/example-app (branch, file tree, manifests, README, CI, infer tech stack).",
                        "Fetch the referenced Figma design node/page from the provided Figma URL (capture images, node structure, styles, and page name).",
                    ],
                    "actions": [
                        {
                            "action": "fetch_agent_context",
                            "capability": "jira.ticket.fetch",
                            "message": "Fetch ticket PROJ-4",
                            "reason": "Need the latest Jira issue details.",
                        },
                        {
                            "action": "fetch_agent_context",
                            "capability": "scm.repo.inspect",
                            "message": "Inspect repository https://github.com/example-org/example-app",
                            "reason": "Need the latest repository metadata.",
                        },
                    ],
                }

            def fake_call_sync_agent(capability: str, message_text: str, *_args, **_kwargs) -> dict:
                self.assertEqual(capability, expected_capability)
                self.assertEqual(message_text, expected_message)
                return {"artifacts": [{"parts": [{"text": "Practice Quiz design context"}]}]}

            fake_plan = {
                "platform": "web",
                "dev_capability": "web.task.execute",
                "target_repo_url": "https://github.com/example-org/example-app",
                "dev_instruction": "Implement the requested page in the target repository.",
                "acceptance_criteria": ["Practice Quiz screen matches the design."],
                "requires_tests": True,
                "test_requirements": "Add integration coverage for the Practice Quiz screen.",
                "screenshot_requirements": None,
            }

            with mock.patch.object(team_lead_app, "_analyze_task", return_value=analysis), mock.patch.object(
                team_lead_app,
                "_plan_information_gathering",
                side_effect=fake_plan_information_gathering,
            ), mock.patch.object(
                team_lead_app.agent_directory,
                "list_agents",
                return_value=[
                    {
                        "agent_id": "ui-design-agent",
                        "capabilities": ["figma.page.fetch"],
                        "instances": [
                            {
                                "instance_id": "ui-1",
                                "status": "idle",
                                "service_url": "http://ui-design:8040",
                            }
                        ],
                    }
                ],
            ), mock.patch.object(
                team_lead_app,
                "_call_sync_agent",
                side_effect=fake_call_sync_agent,
            ) as sync_mock, mock.patch.object(
                team_lead_app,
                "_create_plan",
                return_value=fake_plan,
            ), mock.patch.object(team_lead_app, "_notify_compass"), mock.patch.object(
                team_lead_app,
                "_report_progress",
            ), mock.patch.object(
                team_lead_app,
                "_generate_summary",
                return_value="validation checkpoint reached",
            ):
                team_lead_app._run_workflow(task.task_id, ctx)

            sync_mock.assert_called_once_with(
                expected_capability,
                expected_message,
                task.task_id,
                workspace,
                "compass-task-1",
                permissions=None,
            )
            self.assertIsNotNone(ctx.design_info)
            self.assertEqual(ctx.design_info["fetchedBy"], expected_capability)
            self.assertTrue(Path(workspace, "team-lead/design-context.json").is_file())

            current = team_lead_app.task_store.get(task.task_id)
            self.assertEqual(current.state, "TASK_STATE_COMPLETED")

    def test_team_lead_does_not_plan_with_unresolved_missing_info(self):
        with tempfile.TemporaryDirectory(prefix="team_lead_unresolved_missing_") as workspace:
            task = team_lead_app.task_store.create()
            ctx = team_lead_app._TaskContext()
            ctx.compass_task_id = "compass-task-1"
            ctx.compass_callback_url = "http://compass.local/tasks/task-1/callbacks"
            ctx.compass_url = "http://compass.local"
            ctx.shared_workspace_path = workspace
            ctx.user_text = "Implement PROJ-2."
            ctx.original_message = {"metadata": {}}

            analysis = {
                "task_type": "feature",
                "platform": "web",
                "needs_jira_fetch": False,
                "needs_design_context": False,
                "missing_info": ["Exact Stitch screen ID is still missing"],
                "question_for_user": None,
                "summary": "Implement PROJ-2.",
            }

            with mock.patch.object(team_lead_app, "_analyze_task", return_value=analysis), mock.patch.object(
                team_lead_app,
                "_plan_information_gathering",
                return_value={
                    "pending_tasks": ["Proceed to implementation planning"],
                    "actions": [
                        {
                            "action": "proceed_to_plan",
                            "reason": "Planner claims it can proceed.",
                        }
                    ],
                },
            ), mock.patch.object(
                team_lead_app,
                "_create_plan",
            ) as create_plan_mock, mock.patch.object(team_lead_app, "_notify_compass"), mock.patch.object(
                team_lead_app,
                "_report_progress",
            ), mock.patch.object(
                team_lead_app,
                "_generate_summary",
                return_value="failure summary",
            ):
                team_lead_app._run_workflow(task.task_id, ctx)

            create_plan_mock.assert_not_called()
            current = team_lead_app.task_store.get(task.task_id)
            self.assertEqual(current.state, "TASK_STATE_FAILED")
            self.assertEqual(current.status_message, "failure summary")

    def test_team_lead_validation_mode_can_checkpoint_with_noncritical_missing_info(self):
        with tempfile.TemporaryDirectory(prefix="team_lead_validation_checkpoint_") as workspace:
            task = team_lead_app.task_store.create()
            ctx = team_lead_app._TaskContext()
            ctx.compass_task_id = "compass-task-1"
            ctx.compass_callback_url = "http://compass.local/tasks/task-1/callbacks"
            ctx.compass_url = "http://compass.local"
            ctx.shared_workspace_path = workspace
            ctx.user_text = "Implement PROJ-2."
            ctx.original_message = {"metadata": {"stopBeforeDevDispatch": True}}
            ctx.jira_info = {
                "ticket_key": "PROJ-2",
                "content": "Jira content with the Stitch URL and no repo URL.",
                "request": "Fetch ticket PROJ-2",
            }
            ctx.design_info = {
                "url": "https://stitch.withgoogle.com/projects/mock-project-id?pli=1",
                "type": "stitch",
                "content": "Lesson Library screen metadata already fetched.",
                "page_name": "Lesson Library page",
                "request": "Fetch design from Stitch",
            }
            ctx.additional_info = "Use Python 3.12 and Flask."

            def fake_analyze(_user_text: str, additional_info: str = "") -> dict:
                return {
                    "task_type": "feature",
                    "platform": "web",
                    "needs_jira_fetch": True,
                    "jira_ticket_key": "PROJ-2",
                    "needs_design_context": True,
                    "design_url": "https://stitch.withgoogle.com/projects/mock-project-id?pli=1",
                    "design_type": "stitch",
                    "design_page_name": "Lesson Library page",
                    "target_repo_url": None,
                    "missing_info": [
                        "Google Stitch read/export API token or grant export permissions so screens and PNG assets can be fetched"
                    ],
                    "question_for_user": None,
                    "summary": "Implement the Lesson Library page.",
                }

            fake_plan = {
                "platform": "web",
                "dev_capability": "web.task.execute",
                "target_repo_url": "",
                "dev_instruction": "Implement the requested flow in Flask.",
                "acceptance_criteria": ["Lesson Library page matches the design."],
                "requires_tests": True,
                "test_requirements": "Add integration coverage.",
                "screenshot_requirements": None,
            }

            with mock.patch.object(team_lead_app, "_analyze_task", side_effect=fake_analyze), mock.patch.object(
                team_lead_app,
                "_plan_information_gathering",
                return_value={
                    "pending_tasks": ["Fetch more Jira and Stitch details"],
                    "actions": [
                        {
                            "action": "fetch_agent_context",
                            "capability": "jira.ticket.fetch",
                            "message": "Fetch ticket PROJ-2 again",
                            "reason": "Gather more details.",
                        }
                    ],
                },
            ) as gather_mock, mock.patch.object(
                team_lead_app,
                "_call_sync_agent",
                side_effect=AssertionError("validation checkpoint should not fetch more context once ready"),
            ) as sync_mock, mock.patch.object(
                team_lead_app,
                "_create_plan",
                return_value=fake_plan,
            ), mock.patch.object(team_lead_app, "_notify_compass"), mock.patch.object(
                team_lead_app,
                "_report_progress",
            ), mock.patch.object(
                team_lead_app,
                "_generate_summary",
                return_value="validation checkpoint reached",
            ):
                team_lead_app._run_workflow(task.task_id, ctx)

            gather_mock.assert_not_called()
            sync_mock.assert_not_called()
            current = team_lead_app.task_store.get(task.task_id)
            self.assertEqual(current.state, "TASK_STATE_COMPLETED")
            self.assertIn("validation checkpoint reached", current.status_message)

    def test_team_lead_reports_missing_jira_capability_clearly(self):
        with mock.patch.object(
            team_lead_app.agent_directory,
            "resolve_capability",
            side_effect=team_lead_app.CapabilityUnavailableError("missing jira"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Required capability 'jira.ticket.fetch' is unavailable"):
                team_lead_app._call_sync_agent(
                    "jira.ticket.fetch",
                    "Fetch ticket PROJ-2",
                    "task-1",
                    "/tmp/workspace",
                    "compass-task-1",
                )

    def test_team_lead_reports_missing_scm_capability_clearly(self):
        with mock.patch.object(
            team_lead_app.agent_directory,
            "resolve_capability",
            side_effect=team_lead_app.CapabilityUnavailableError("missing scm"),
        ):
            with self.assertRaisesRegex(RuntimeError, "Required capability 'scm.repo.inspect' is unavailable"):
                team_lead_app._call_sync_agent(
                    "scm.repo.inspect",
                    "Inspect repository https://github.com/example/repo",
                    "task-1",
                    "/tmp/workspace",
                    "compass-task-1",
                )

    def test_team_lead_sync_agent_timeout_raises_instead_of_returning_working_task(self):
        with mock.patch.object(
            team_lead_app.agent_directory,
            "resolve_capability",
            return_value=(
                {"agent_id": "ui-design-agent"},
                {"service_url": "http://ui-design:8040"},
            ),
        ), mock.patch.object(
            team_lead_app,
            "_a2a_send",
            return_value={
                "id": "ui-task-1",
                "status": {"state": "TASK_STATE_WORKING"},
                "artifacts": [],
            },
        ), mock.patch.object(team_lead_app, "_poll_agent_task", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "did not complete within sync timeout"):
                team_lead_app._call_sync_agent(
                    "figma.page.fetch",
                    "Fetch design from https://www.figma.com/design/abc123/Test page: Community",
                    "task-1",
                    "/tmp/workspace",
                    "compass-task-1",
                )

    def test_team_lead_filters_defaultable_web_ui_missing_info(self):
        ctx = team_lead_app._TaskContext()
        ctx.jira_info = {"content": "Jira ticket content is available."}
        ctx.repo_info = {
            "repo_url": "https://bitbucket.example.com/users/demo/repos/web-ui-test/browse",
            "content": "Repository context is available.",
        }
        ctx.design_info = {"content": "Figma fetch returned rate-limit details."}

        unresolved = team_lead_app._filter_unresolved_missing_info(
            {
                "platform": "web",
                "missing_info": [
                    "Desired route/path and integration point in the repo (where page should be mounted)",
                    "Any required dynamic data or API endpoints / mock data contracts",
                    "Responsive breakpoints and browser support expectations",
                ],
            },
            ctx,
        )

        self.assertEqual(unresolved, [])

    def test_team_lead_filters_backend_api_vs_static_data_question_for_web_ui(self):
        ctx = team_lead_app._TaskContext()
        ctx.jira_info = {"content": "Jira ticket content is available."}
        ctx.repo_info = {
            "repo_url": "https://bitbucket.example.com/users/demo/repos/web-ui-test/browse",
            "content": "Repository context is available.",
        }
        ctx.design_info = {"content": "Figma node context is available."}

        unresolved = team_lead_app._filter_unresolved_missing_info(
            {
                "platform": "web",
                "missing_info": [
                    "Whether the page requires real backend APIs (endpoints/specs) or can use mocked/static data for initial delivery.",
                ],
            },
            ctx,
        )

        self.assertEqual(unresolved, [])

    def test_team_lead_suppresses_defaultable_web_ui_questions_with_full_context(self):
        ctx = team_lead_app._TaskContext()
        ctx.jira_info = {"content": "Jira ticket content is available."}
        ctx.repo_info = {"content": "Repository context is available."}
        ctx.design_info = {"content": "Figma node context is available."}

        updated = team_lead_app._suppress_redundant_questions(
            {
                "platform": "web",
                "missing_info": [
                    "Explicit acceptance criteria (pass/fail conditions, pages/components to implement, responsive breakpoints, test/QA steps).",
                    "Desired assignment and any reviewer/owner to set on the Jira ticket before implementation.",
                ],
                "question_for_user": "Please provide explicit acceptance criteria for PROJ-4 and the desired reviewer.",
            },
            ctx,
        )

        self.assertEqual(updated.get("question_for_user"), None)
        self.assertEqual(updated.get("missing_info"), [])

    def test_team_lead_gather_planner_prefers_registered_fetch_over_user_question(self):
        ctx = team_lead_app._TaskContext()
        analysis = {
            "task_type": "feature",
            "platform": "web",
            "needs_jira_fetch": True,
            "jira_ticket_key": "PROJ-2",
            "needs_design_context": False,
            "missing_info": ["jira ticket content"],
            "question_for_user": "Please paste the Jira ticket details.",
            "summary": "Implement PROJ-2.",
        }

        runtime_plan = {
            "pending_tasks": ["Ask user for the Jira ticket details"],
            "actions": [
                {
                    "action": "ask_user",
                    "question": "Please paste the Jira ticket details.",
                    "reason": "Need more detail.",
                },
                {
                    "action": "fetch_agent_context",
                    "capability": "jira.ticket.fetch",
                    "message": "Fetch ticket PROJ-2",
                    "reason": "Need the Jira ticket body before planning.",
                },
            ],
            "summary": "Fetch the ticket before asking the user.",
        }

        with mock.patch.object(
            team_lead_app.agent_directory,
            "list_agents",
            return_value=[
                {
                    "agent_id": "jira-agent",
                    "capabilities": ["jira.ticket.fetch"],
                    "instances": [{"instance_id": "jira-1", "status": "idle"}],
                }
            ],
        ), mock.patch.object(team_lead_app, "_run_agentic", return_value=json.dumps(runtime_plan)):
            gather_plan = team_lead_app._plan_information_gathering(
                "Implement PROJ-2.",
                analysis,
                ctx,
            )

        self.assertEqual(gather_plan["actions"][0]["action"], "fetch_agent_context")
        self.assertEqual(gather_plan["actions"][0]["capability"], "jira.ticket.fetch")
        self.assertIn("Ask user", gather_plan["pending_tasks"][0])

    def test_team_lead_finds_recovered_boundary_agent_on_next_attempt(self):
        class FakeRegistry:
            def __init__(self):
                self.calls = 0

            def find_any_active(self):
                self.calls += 1
                if self.calls <= 2:
                    return []
                return [
                    {
                        "agent_id": "jira-agent",
                        "capabilities": ["jira.ticket.fetch"],
                        "instances": [
                            {
                                "instance_id": "jira-1",
                                "status": "idle",
                                "service_url": "http://jira:8010",
                            }
                        ],
                    }
                ]

            def get_topology(self):
                return {"version": self.calls, "updatedAt": self.calls}

        recovered_directory = team_lead_app.AgentDirectory(
            "team-lead-agent",
            FakeRegistry(),
            cache_ttl_seconds=999,
            watch_interval_seconds=999,
        )

        with mock.patch.object(team_lead_app, "agent_directory", recovered_directory), mock.patch.object(
            team_lead_app,
            "_a2a_send",
            return_value={
                "id": "jira-task-1",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [{"parts": [{"text": "Recovered Jira capability."}]}],
            },
        ):
            with self.assertRaisesRegex(RuntimeError, "Required capability 'jira.ticket.fetch' is unavailable"):
                team_lead_app._call_sync_agent(
                    "jira.ticket.fetch",
                    "Fetch ticket PROJ-2",
                    "task-1",
                    "/tmp/workspace",
                    "compass-task-1",
                )

            result = team_lead_app._call_sync_agent(
                "jira.ticket.fetch",
                "Fetch ticket PROJ-2",
                "task-1",
                "/tmp/workspace",
                "compass-task-1",
            )

        self.assertEqual(result["status"]["state"], "TASK_STATE_COMPLETED")

    def test_revision_metadata_preserves_constraints_and_workflow_requirements(self):
        metadata = team_lead_app._build_dev_task_metadata(
            dev_capability="web.task.execute",
            compass_task_id="task-1",
            team_lead_task_id="task-1",
            workspace="/tmp/workspace",
            target_repo_url="https://github.com/example/repo",
            permissions={"taskType": "development", "allowed": [], "denied": [], "fallback": "deny_and_escalate"},
            tech_stack_constraints={
                "language": "python",
                "python_version": "3.12",
                "backend_framework": "flask",
            },
            acceptance_criteria=["Tests pass."],
            requires_tests=True,
            screenshot_requirements="Include design-reference.png and screenshot-1280x900.png in the PR.",
            is_revision=True,
            revision_cycle=2,
            review_issues=["Re-run pytest and attach evidence."],
        )

        self.assertEqual(metadata["targetRepoUrl"], "https://github.com/example/repo")
        self.assertEqual(metadata["techStackConstraints"]["backend_framework"], "flask")
        self.assertEqual(metadata["acceptanceCriteria"], ["Tests pass."])
        self.assertTrue(metadata["requiresTests"])
        self.assertEqual(
            metadata["screenshotRequirements"],
            "Include design-reference.png and screenshot-1280x900.png in the PR.",
        )
        self.assertTrue(metadata["isRevision"])
        self.assertEqual(metadata["revisionCycle"], 2)
        self.assertEqual(metadata["reviewIssues"], ["Re-run pytest and attach evidence."])
        self.assertEqual(metadata["permissions"]["taskType"], "development")
        self.assertIn("transition the Jira ticket to 'In Progress'", metadata["devWorkflowInstructions"])

    def test_team_lead_sync_agent_forwards_permissions_snapshot(self):
        captured: dict = {}
        permissions = {
            "taskType": "development",
            "allowed": [],
            "denied": [],
            "fallback": "deny_and_escalate",
        }

        def fake_send(agent_url: str, message: dict, context_id: str | None = None) -> dict:
            captured["agent_url"] = agent_url
            captured["message"] = message
            captured["context_id"] = context_id
            return {
                "id": "jira-task-1",
                "status": {"state": "TASK_STATE_COMPLETED"},
                "artifacts": [],
            }

        with mock.patch.object(
            team_lead_app.agent_directory,
            "resolve_capability",
            return_value=(
                {"agent_id": "jira-agent"},
                {"service_url": "http://jira:8010"},
            ),
        ), mock.patch.object(team_lead_app, "_a2a_send", side_effect=fake_send):
            result = team_lead_app._call_sync_agent(
                "jira.ticket.fetch",
                "Fetch ticket PROJ-2",
                "task-1",
                "/tmp/workspace",
                "compass-task-1",
                permissions=permissions,
            )

        self.assertEqual(result["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(captured["agent_url"], "http://jira:8010")
        self.assertEqual(captured["message"]["metadata"]["permissions"], permissions)

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

    def test_team_lead_review_evidence_includes_clone_branch_and_pr_metadata(self):
        with tempfile.TemporaryDirectory(prefix="review_evidence_") as workspace:
            agent_dir = Path(workspace) / "web-agent"
            agent_dir.mkdir(parents=True, exist_ok=True)
            (agent_dir / "clone-info.json").write_text(
                json.dumps(
                    {
                        "status": "completed",
                        "repoUrl": "https://github.com/example-org/example-app",
                        "clonePath": f"{workspace}/example-app",
                    }
                ),
                encoding="utf-8",
            )
            (agent_dir / "branch-info.json").write_text(
                json.dumps({"branch": "feature/proj-2", "baseBranch": "main"}),
                encoding="utf-8",
            )
            (agent_dir / "pr-evidence.json").write_text(
                json.dumps(
                    {
                        "prUrl": "https://github.com/example-org/example-app/pull/1",
                        "branch": "feature/proj-2",
                        "buildPassed": True,
                        "generatedFiles": ["src/App.tsx"],
                    }
                ),
                encoding="utf-8",
            )

            evidence = team_lead_app._load_workspace_review_evidence(workspace)

        self.assertIn("Clone status: completed", evidence)
        self.assertIn("Branch evidence: branch=feature/proj-2, base=main", evidence)
        self.assertIn("PR URL: https://github.com/example-org/example-app/pull/1", evidence)

    def test_team_lead_keeps_repo_and_stack_missing_when_repo_search_failed(self):
        ctx = team_lead_app._TaskContext()
        ctx.jira_info = {"content": "Fetched Jira ticket."}
        ctx.repo_info = {
            "content": "Repository search failed: missing_default_project",
            "repo_url": "",
        }
        analysis = {
            "platform": "web",
            "missing_info": ["target repository URL", "confirmed tech stack / framework"],
        }

        unresolved = team_lead_app._filter_unresolved_missing_info(analysis, ctx, tech_stack_constraints={})

        self.assertIn("target repository URL", unresolved)
        self.assertIn("confirmed tech stack / framework", unresolved)

    def test_team_lead_does_not_suppress_stack_question_when_repo_url_missing(self):
        ctx = team_lead_app._TaskContext()
        ctx.jira_info = {"content": "Fetched Jira ticket."}
        ctx.repo_info = {
            "content": "Repository search failed: missing_default_project",
            "repo_url": "",
        }
        analysis = {
            "platform": "web",
            "question_for_user": (
                "The Jira ticket does not specify the web tech stack. Please confirm the stack to use, "
                "for example Python Flask or Node.js/Express."
            ),
            "missing_info": ["confirmed tech stack / framework"],
        }

        updated = team_lead_app._suppress_redundant_questions(analysis, ctx)

        self.assertEqual(updated["question_for_user"], analysis["question_for_user"])


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