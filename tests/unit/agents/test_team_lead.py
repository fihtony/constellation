"""Tests for Team Lead Agent (graph-first, ReAct-inside-nodes)."""
import pytest
import asyncio
import time
import json
from typing import Any
from unittest.mock import MagicMock
from agents.team_lead.agent import TeamLeadAgent, team_lead_definition
from framework.agent import AgentMode, AgentServices, ExecutionMode


def _make_agent(mock_runtime):
    from unittest.mock import MagicMock as M
    from framework.task_store import InMemoryTaskStore
    services = AgentServices(
        session_service=M(), event_store=M(), memory_service=M(),
        skills_registry=M(), plugin_manager=M(), checkpoint_service=M(),
        runtime=mock_runtime, registry_client=None,
        task_store=InMemoryTaskStore(),
    )
    agent = TeamLeadAgent(definition=team_lead_definition, services=services)
    return agent


def _mock_runtime(summary="PR created.", success=True):
    result = MagicMock()
    result.success = success
    result.summary = summary
    runtime = MagicMock()
    runtime.run_agentic.return_value = result
    runtime.run.return_value = {"raw_response": "{}"}
    return runtime


@pytest.fixture(autouse=True)
def _clear_child_session_cache():
    from agents.team_lead import nodes as tl_nodes

    with tl_nodes._CHILD_SESSION_CACHE_LOCK:
        tl_nodes._CHILD_SESSION_CACHE.clear()
    yield
    with tl_nodes._CHILD_SESSION_CACHE_LOCK:
        tl_nodes._CHILD_SESSION_CACHE.clear()


class TestTeamLeadDefinition:
    def test_agent_id(self):
        assert team_lead_definition.agent_id == "team-lead"

    def test_mode(self):
        assert team_lead_definition.mode == AgentMode.TASK

    def test_execution_mode(self):
        assert team_lead_definition.execution_mode == ExecutionMode.PERSISTENT

    def test_has_workflow(self):
        """Team Lead is now graph-first — it MUST have a workflow."""
        assert team_lead_definition.workflow is not None

    def test_has_tools(self):
        assert len(team_lead_definition.tools) > 0

    def test_declared_tools_are_allowed_by_development_profile(self):
        from pathlib import Path

        from framework.permissions import PermissionEngine

        permissions_path = (
            Path(__file__).resolve().parents[3]
            / "config"
            / "permissions"
            / "development.yaml"
        )
        engine = PermissionEngine.from_yaml(str(permissions_path))

        for tool_name in team_lead_definition.tools:
            assert engine.check_tool(tool_name), f"development.yaml must allow Team Lead tool: {tool_name}"


class TestTeamLeadAgent:
    async def test_handle_message_returns_working(self):
        """handle_message returns immediately with WORKING state (async workflow)."""
        runtime = _mock_runtime()
        agent = _make_agent(runtime)
        await agent.start()

        message = {
            "parts": [{"text": "Implement feature ABC-123"}],
            "metadata": {"jiraKey": "ABC-123"},
        }
        result = await agent.handle_message(message)

        assert result["task"]["status"]["state"] == "TASK_STATE_WORKING"
        assert result["task"]["id"]

    async def test_get_task_returns_real_state(self):
        """get_task returns real state from TaskStore."""
        runtime = _mock_runtime()
        agent = _make_agent(runtime)
        await agent.start()

        message = {
            "parts": [{"text": "Fix bug"}],
            "metadata": {},
        }
        result = await agent.handle_message(message)
        task_id = result["task"]["id"]

        # Give the worker thread a moment to complete
        await asyncio.sleep(0.5)

        poll = await agent.get_task(task_id)
        # Should be either COMPLETED or FAILED (not the old hardcoded WORKING)
        state = poll["task"]["status"]["state"]
        assert state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED")

    async def test_workflow_produces_report_summary(self):
        """Completed workflow should produce a report_summary in artifacts."""
        runtime = _mock_runtime()
        agent = _make_agent(runtime)
        await agent.start()

        message = {
            "parts": [{"text": "Build login page"}],
            "metadata": {},
        }
        result = await agent.handle_message(message)
        task_id = result["task"]["id"]

        await asyncio.sleep(0.5)

        poll = await agent.get_task(task_id)
        if poll["task"]["status"]["state"] == "TASK_STATE_COMPLETED":
            artifacts = poll["task"]["artifacts"]
            assert len(artifacts) > 0


class TestGatherContextFailures:
    def test_jira_payload_validation_accepts_fetched_status(self):
        from agents.team_lead.nodes import _validate_jira_payload

        ticket = _validate_jira_payload(
            {"ticket": {"key": "PROJ-123", "fields": {"summary": "Ready"}}, "status": "fetched"},
            "PROJ-123",
        )

        assert ticket["key"] == "PROJ-123"

    async def test_gather_context_raises_when_jira_fetch_fails(self, monkeypatch, tmp_path):
        """Jira fetch failure is fatal so invalid tickets do not drift to a default repo."""
        from agents.team_lead.nodes import gather_context

        class StubRegistry:
            def execute_sync(self, name, args):
                if name == "fetch_jira_ticket":
                    return json.dumps({"ticket": None, "status": "HTTP 404"})
                return json.dumps({})

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        with pytest.raises(RuntimeError, match="Jira fetch failed"):
            await gather_context({
                "jira_key": "PROJ-123",
                "workspace_path": str(tmp_path),
            })

    async def test_gather_context_raises_when_jira_ticket_missing_key(self, monkeypatch, tmp_path):
        """A successful-looking Jira response still must contain the fetched ticket key."""
        from agents.team_lead.nodes import gather_context

        class StubRegistry:
            def execute_sync(self, name, args):
                if name == "fetch_jira_ticket":
                    return json.dumps({"ticket": {"fields": {"summary": "Missing key"}}, "status": "ok"})
                return json.dumps({})

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        with pytest.raises(RuntimeError, match="ticket payload missing key"):
            await gather_context({
                "jira_key": "PROJ-123",
                "workspace_path": str(tmp_path),
            })

    async def test_gather_context_requires_repo_url_after_jira_fetch(self, monkeypatch, tmp_path):
        """Repo URL must come from the request or Jira context before dev dispatch."""
        from agents.team_lead.nodes import gather_context

        class StubRegistry:
            def execute_sync(self, name, args):
                if name == "fetch_jira_ticket":
                    return json.dumps({
                        "ticket": {"key": "PROJ-123", "fields": {"summary": "No repo URL"}},
                        "status": "ok",
                    })
                return json.dumps({})

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        with pytest.raises(RuntimeError, match="No SCM repository URL"):
            await gather_context({
                "jira_key": "PROJ-123",
                "workspace_path": str(tmp_path),
            })

    async def test_gather_context_raises_when_repo_clone_fails(self, monkeypatch, tmp_path):
        """Repo clone failure is fatal because Web Dev must receive a real cloned repo."""
        from agents.team_lead.nodes import gather_context

        class StubRegistry:
            def execute_sync(self, name, args):
                if name == "clone_repo":
                    return json.dumps({"error": "clone failed"})
                return json.dumps({})

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        with pytest.raises(RuntimeError, match="Repo clone FAILED"):
            await gather_context({
                "repo_url": "https://github.com/org/repo.git",
                "workspace_path": str(tmp_path),
            })

    async def test_gather_context_requires_ui_design_workspace_outputs(self, monkeypatch, tmp_path):
        from agents.team_lead.nodes import gather_context

        class StubRegistry:
            def execute_sync(self, name, args):
                if name == "fetch_design":
                    return json.dumps({
                        "screen": {"projectId": "p1", "screenId": "s1", "text": "{}"},
                        "status": "ok",
                    })
                if name == "clone_repo":
                    return json.dumps({"repo_path": str(tmp_path / "scm" / "repo"), "status": "ok"})
                return json.dumps({})

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        with pytest.raises(RuntimeError, match="UI Design files missing from workspace"):
            await gather_context({
                "repo_url": "https://github.com/org/repo.git",
                "stitch_project_id": "13629074018280446337",
                "stitch_screen_id": "screen-1",
                "workspace_path": str(tmp_path),
            })

    async def test_gather_context_keeps_ui_design_paths_when_present(self, monkeypatch, tmp_path):
        from agents.team_lead.nodes import gather_context

        stitch_dir = tmp_path / "ui-design" / "stitch"
        stitch_dir.mkdir(parents=True)
        code_path = stitch_dir / "code.html"
        md_path = stitch_dir / "DESIGN.md"
        code_path.write_text("<html><body>Design</body></html>", encoding="utf-8")
        md_path.write_text("# Design\n\ncolors:\n  primary: '#000'\n", encoding="utf-8")

        class StubRegistry:
            def execute_sync(self, name, args):
                if name == "fetch_design":
                    return json.dumps({
                        "screen": {"projectId": "p1", "screenId": "s1", "text": "{}"},
                        "status": "ok",
                        "local_folder": str(stitch_dir),
                        "files": ["ui-design/stitch/code.html", "ui-design/stitch/DESIGN.md"],
                        "design_code_path": str(code_path),
                        "design_md_path": str(md_path),
                    })
                if name == "clone_repo":
                    repo_path = tmp_path / "scm" / "repo.git"
                    repo_path.mkdir(parents=True, exist_ok=True)
                    (repo_path / "README.md").write_text("ok", encoding="utf-8")
                    return json.dumps({"repo_path": str(repo_path), "status": "ok"})
                return json.dumps({})

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        result = await gather_context({
            "repo_url": "https://github.com/org/repo.git",
            "stitch_project_id": "13629074018280446337",
            "stitch_screen_id": "screen-1",
            "workspace_path": str(tmp_path),
        })

        assert result["design_code_path"] == str(code_path)
        assert result["design_md_path"] == str(md_path)
        assert "ui-design/stitch/DESIGN.md" in result["design_files"]
        assert not (tmp_path / "team-lead" / "design-spec.md").exists()


class TestDispatchDevAgentValidation:
    async def test_dispatch_dev_agent_raises_when_web_dev_reports_error(self, monkeypatch):
        from agents.team_lead.nodes import dispatch_dev_agent

        class StubPermissionEngine:
            def require_agent_launching(self, agent_id):
                assert agent_id == "web-dev"

        class StubRegistry:
            _permission_engine = StubPermissionEngine()

            def execute_sync(self, name, args):
                assert name == "dispatch_web_dev"
                return json.dumps({"status": "error", "message": "Web Dev task failed"})

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        with pytest.raises(RuntimeError, match="Web Dev task failed"):
            await dispatch_dev_agent(
                {
                    "_task_id": "task-123",
                    "user_request": "Implement CSTL-2",
                    "analysis_summary": "Implement the ticket",
                    "workspace_path": "/tmp/workspace",
                }
            )

    async def test_dispatch_dev_agent_requires_pr_and_jira_evidence(self, monkeypatch):
        from agents.team_lead.nodes import dispatch_dev_agent

        class StubPermissionEngine:
            def require_agent_launching(self, agent_id):
                assert agent_id == "web-dev"

        class StubRegistry:
            _permission_engine = StubPermissionEngine()

            def execute_sync(self, name, args):
                assert name == "dispatch_web_dev"
                return json.dumps({"status": "completed", "summary": "done"})

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        with pytest.raises(RuntimeError, match="prUrl, jiraInReview"):
            await dispatch_dev_agent(
                {
                    "_task_id": "task-123",
                    "user_request": "Implement CSTL-2",
                    "analysis_summary": "Implement the ticket",
                    "jira_key": "CSTL-2",
                    "workspace_path": "/tmp/workspace",
                    "plan": {
                        "definition_of_done": {
                            "pr_required": True,
                            "jira_state_management": True,
                        }
                    },
                }
            )

    async def test_dispatch_dev_agent_requires_screenshot_evidence_for_ui_tasks(self, monkeypatch):
        from agents.team_lead.nodes import dispatch_dev_agent

        class StubPermissionEngine:
            def require_agent_launching(self, agent_id):
                assert agent_id == "web-dev"

        class StubRegistry:
            _permission_engine = StubPermissionEngine()

            def execute_sync(self, name, args):
                assert name == "dispatch_web_dev"
                assert args["definition_of_done"]["screenshot_required"] is True
                return json.dumps({
                    "status": "completed",
                    "summary": "done",
                    "prUrl": "https://github.com/org/repo/pull/1",
                    "branch": "feature/ui",
                    "jiraInReview": True,
                })

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        with pytest.raises(RuntimeError, match="screenshotIncluded"):
            await dispatch_dev_agent(
                {
                    "_task_id": "task-123",
                    "user_request": "Implement UI",
                    "analysis_summary": "Implement a UI page",
                    "jira_key": "PROJ-123",
                    "workspace_path": "/tmp/workspace",
                    "design_context": {"screen": {"id": "screen-1"}},
                    "plan": {
                        "definition_of_done": {
                            "pr_required": True,
                            "jira_state_management": True,
                            "screenshot_required": True,
                        }
                    },
                }
            )

    async def test_dispatch_dev_agent_passes_child_permissions_not_parent_snapshot(self, monkeypatch):
        from agents.team_lead.nodes import dispatch_dev_agent

        captured = {}

        class StubPermissionEngine:
            def require_agent_launching(self, agent_id):
                assert agent_id == "web-dev"

        class StubRegistry:
            _permission_engine = StubPermissionEngine()

            def execute_sync(self, name, args):
                captured["name"] = name
                captured["args"] = args
                return json.dumps({
                    "status": "completed",
                    "summary": "done",
                    "prUrl": "https://github.com/org/repo/pull/1",
                    "branch": "feature/ui",
                    "jiraInReview": True,
                    "screenshotIncluded": True,
                })

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        await dispatch_dev_agent(
            {
                "_task_id": "task-123",
                "user_request": "Implement UI",
                "analysis_summary": "Implement a UI page",
                "jira_key": "PROJ-123",
                "workspace_path": "/tmp/workspace",
                "design_context": {"screen": {"id": "screen-1"}},
                "metadata": {
                    "permissions": {
                        "allowedTools": ["dispatch_web_dev"],
                        "deniedTools": [],
                        "scm": "read",
                        "filesystem": "workspace-only",
                        "custom": {},
                    }
                },
                "plan": {
                    "definition_of_done": {
                        "pr_required": True,
                        "jira_state_management": True,
                        "screenshot_required": True,
                    }
                },
            }
        )

        assert captured["name"] == "dispatch_web_dev"
        assert "scm_push" in captured["args"]["permissions"]["allowedTools"]
        assert "dispatch_web_dev" not in captured["args"]["permissions"]["allowedTools"]

    async def test_dispatch_dev_agent_reuses_existing_branch_name(self, monkeypatch):
        from agents.team_lead.nodes import dispatch_dev_agent

        captured = {}

        class StubPermissionEngine:
            def require_agent_launching(self, agent_id):
                assert agent_id == "web-dev"

        class StubRegistry:
            _permission_engine = StubPermissionEngine()

            def execute_sync(self, name, args):
                captured["name"] = name
                captured["args"] = args
                return json.dumps({
                    "status": "completed",
                    "summary": "done",
                    "prUrl": "https://github.com/org/repo/pull/1",
                    "branch": "feature/cstl-3-practice-quiz-page_3",
                    "jiraInReview": True,
                })

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        await dispatch_dev_agent(
            {
                "_task_id": "task-123",
                "user_request": "Implement UI",
                "analysis_summary": "Implement a UI page",
                "jira_key": "PROJ-123",
                "workspace_path": "/tmp/workspace",
                "repo_url": "https://github.com/org/repo",
                "repo_path": "/tmp/workspace/repo",
                "branch_name": "feature/cstl-3-practice-quiz-page_3",
                "plan": {
                    "definition_of_done": {
                        "pr_required": True,
                        "jira_state_management": True,
                    }
                },
            }
        )

        assert captured["name"] == "dispatch_web_dev"
        assert captured["args"]["branch_name"] == "feature/cstl-3-practice-quiz-page_3"

    async def test_dispatch_dev_agent_propagates_screenshot_evidence(self, monkeypatch):
        from agents.team_lead.nodes import dispatch_dev_agent

        class StubPermissionEngine:
            def require_agent_launching(self, agent_id):
                assert agent_id == "web-dev"

        class StubRegistry:
            _permission_engine = StubPermissionEngine()

            def execute_sync(self, name, args):
                assert name == "dispatch_web_dev"
                return json.dumps({
                    "status": "completed",
                    "summary": "done",
                    "prUrl": "https://github.com/org/repo/pull/1",
                    "branch": "feature/ui",
                    "jiraInReview": True,
                    "screenshotIncluded": True,
                    "screenshotUploaded": True,
                })

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        result = await dispatch_dev_agent(
            {
                "_task_id": "task-123",
                "user_request": "Implement UI",
                "analysis_summary": "Implement a UI page",
                "jira_key": "PROJ-123",
                "workspace_path": "/tmp/workspace",
                "design_context": {"screen": {"id": "screen-1"}},
                "plan": {
                    "definition_of_done": {
                        "pr_required": True,
                        "jira_state_management": True,
                        "screenshot_required": True,
                    }
                },
            }
        )

        assert result["screenshot_included"] is True
        assert result["screenshot_uploaded"] is True

    async def test_dispatch_dev_agent_tracks_child_session_metadata(self, monkeypatch):
        from agents.team_lead.nodes import dispatch_dev_agent

        class StubPermissionEngine:
            def require_agent_launching(self, agent_id):
                assert agent_id == "web-dev"

        class StubRegistry:
            _permission_engine = StubPermissionEngine()

            def execute_sync(self, name, args):
                assert name == "dispatch_web_dev"
                return json.dumps({
                    "status": "completed",
                    "summary": "done",
                    "prUrl": "https://github.com/org/repo/pull/1",
                    "branch": "feature/ui",
                    "jiraInReview": True,
                    "screenshotIncluded": True,
                    "screenshotUploaded": True,
                    "childTaskId": "task-web-dev-1",
                    "childServiceUrl": "http://web-dev-task-1:8050",
                    "childContainerName": "web-dev-task-1",
                    "childAgentId": "web-dev",
                })

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        result = await dispatch_dev_agent(
            {
                "_task_id": "task-123",
                "user_request": "Implement UI",
                "analysis_summary": "Implement a UI page",
                "jira_key": "PROJ-123",
                "workspace_path": "/tmp/workspace",
                "design_context": {"screen": {"id": "screen-1"}},
                "plan": {
                    "definition_of_done": {
                        "pr_required": True,
                        "jira_state_management": True,
                        "screenshot_required": True,
                    }
                },
            }
        )

        assert result["dev_agent_session"] == {
            "task_id": "task-web-dev-1",
            "service_url": "http://web-dev-task-1:8050",
            "container_name": "web-dev-task-1",
            "agent_id": "web-dev",
        }

    async def test_dispatch_dev_agent_reuses_detected_live_instance_with_launch_spec_port(self, monkeypatch):
        from agents.team_lead.nodes import dispatch_dev_agent

        captured: dict[str, object] = {}

        class StubPermissionEngine:
            def require_agent_launching(self, agent_id):
                assert agent_id == "web-dev"

        class StubRegistry:
            _permission_engine = StubPermissionEngine()

            def execute_sync(self, name, args):
                assert name == "dispatch_web_dev"
                captured.update(args)
                return json.dumps({
                    "status": "completed",
                    "summary": "done",
                    "prUrl": "https://github.com/org/repo/pull/1",
                    "branch": "feature/ui",
                    "jiraInReview": True,
                    "screenshotIncluded": True,
                    "screenshotUploaded": True,
                })

        class StubLauncher:
            def find_live_instances(self, agent_id, task_id):
                assert agent_id == "web-dev"
                assert task_id == "task-123"
                return [{
                    "container_name": "web-dev-task-123-live",
                    "task_id": "task-123",
                    "agent_id": "web-dev",
                }]

        class StubRegistryClient:
            def get_capability_definition(self, capability):
                assert capability == "web-dev.task.execute"
                return {"launch_spec": {"port": 8050}}

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())
        monkeypatch.setattr("framework.launcher.get_launcher", lambda: StubLauncher())
        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )

        await dispatch_dev_agent(
            {
                "_task_id": "task-123",
                "user_request": "Implement UI",
                "analysis_summary": "Implement a UI page",
                "workspace_path": "/tmp/workspace",
                "plan": {
                    "definition_of_done": {
                        "pr_required": True,
                        "jira_state_management": False,
                        "screenshot_required": False,
                    }
                },
            }
        )

        assert captured["child_service_url"] == "http://web-dev-task-123-live:8050"
        assert captured["child_container_name"] == "web-dev-task-123-live"

    async def test_dispatch_dev_agent_reuses_cached_child_session_without_live_lookup(self, monkeypatch):
        from agents.team_lead import nodes as tl_nodes

        captured: dict[str, object] = {}
        tl_nodes._clear_cached_child_sessions("task-123")
        tl_nodes._cache_child_session(
            "task-123",
            "web-dev",
            {
                "task_id": "task-web-dev-1",
                "service_url": "http://web-dev-task-1:8050",
                "container_name": "web-dev-task-1",
                "agent_id": "web-dev",
            },
        )

        class StubPermissionEngine:
            def require_agent_launching(self, agent_id):
                assert agent_id == "web-dev"

        class StubRegistry:
            _permission_engine = StubPermissionEngine()

            def execute_sync(self, name, args):
                assert name == "dispatch_web_dev"
                captured.update(args)
                return json.dumps({
                    "status": "completed",
                    "summary": "done",
                    "prUrl": "https://github.com/org/repo/pull/1",
                    "branch": "feature/ui",
                    "jiraInReview": True,
                    "screenshotIncluded": False,
                    "screenshotUploaded": False,
                    "childTaskId": "task-web-dev-1",
                    "childServiceUrl": "http://web-dev-task-1:8050",
                    "childContainerName": "web-dev-task-1",
                    "childAgentId": "web-dev",
                })

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())
        monkeypatch.setattr(
            "agents.team_lead.nodes._reuse_live_child_session",
            lambda **_: (_ for _ in ()).throw(AssertionError("live lookup should not run")),
        )

        result = await tl_nodes.dispatch_dev_agent(
            {
                "_task_id": "task-123",
                "user_request": "Implement UI",
                "analysis_summary": "Implement a UI page",
                "workspace_path": "/tmp/workspace",
                "plan": {
                    "definition_of_done": {
                        "pr_required": True,
                        "jira_state_management": False,
                        "screenshot_required": False,
                    }
                },
            }
        )

        assert captured["child_service_url"] == "http://web-dev-task-1:8050"
        assert result["dev_agent_session"]["service_url"] == "http://web-dev-task-1:8050"
        tl_nodes._clear_cached_child_sessions("task-123")

    async def test_dispatch_dev_agent_sends_keepalive_to_waiting_cr_session(self, monkeypatch):
        from agents.team_lead.nodes import dispatch_dev_agent

        ping_calls: list[tuple[str, str, int]] = []

        class StubPermissionEngine:
            def require_agent_launching(self, agent_id):
                assert agent_id == "web-dev"

        class StubRegistry:
            _permission_engine = StubPermissionEngine()

            def execute_sync(self, name, args):
                assert name == "dispatch_web_dev"
                time.sleep(0.08)
                return json.dumps({
                    "status": "completed",
                    "summary": "done",
                    "prUrl": "https://github.com/org/repo/pull/1",
                    "branch": "feature/ui",
                    "jiraInReview": True,
                    "screenshotIncluded": True,
                    "screenshotUploaded": True,
                })

        async def fake_send_ping(self, base_url, task_id, estimated_remaining_wait_seconds=0):
            ping_calls.append((base_url, task_id, estimated_remaining_wait_seconds))

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())
        monkeypatch.setattr("framework.a2a.client.A2AClient.send_ping", fake_send_ping)
        monkeypatch.setenv("TEAM_LEAD_CHILD_KEEPALIVE_INTERVAL_SECONDS", "0.02")

        await dispatch_dev_agent(
            {
                "_task_id": "task-123",
                "user_request": "Implement UI",
                "analysis_summary": "Implement a UI page",
                "workspace_path": "/tmp/workspace",
                "cr_agent_session": {
                    "task_id": "cr-task-1",
                    "service_url": "http://code-review-task-1:8060",
                    "container_name": "code-review-task-1",
                    "agent_id": "code-review",
                },
                "plan": {
                    "definition_of_done": {
                        "pr_required": True,
                        "jira_state_management": False,
                        "screenshot_required": False,
                    }
                },
            }
        )

        assert any(call[:2] == ("http://code-review-task-1:8060", "cr-task-1") for call in ping_calls)

    async def test_review_result_passes_master_task_id_to_code_review(self, monkeypatch):
        from agents.team_lead.nodes import review_result

        captured: dict[str, object] = {}

        class StubRegistry:
            def execute_sync(self, name, args):
                assert name == "dispatch_code_review"
                captured.update(args)
                return json.dumps({"verdict": "approved", "summary": "ok"})

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        result = await review_result(
            {
                "_task_id": "task-123",
                "pr_url": "https://github.com/org/repo/pull/1",
                "pr_number": 1,
                "repo_url": "https://github.com/org/repo",
                "dev_result": {"summary": "done", "prNumber": 1},
                "analysis_summary": "Implement CSTL-1",
                "workspace_path": "/tmp/workspace",
                "context_manifest_path": "team-lead/context-manifest.json",
            }
        )

        assert captured["orchestrator_task_id"] == "task-123"
        assert captured["task_id"] == "task-123"
        assert captured["repo_url"] == "https://github.com/org/repo"
        assert captured["pr_number"] == 1
        assert result["route"] == "approved"

    async def test_review_result_reuses_detected_live_cr_instance_with_launch_spec_port(self, monkeypatch):
        from agents.team_lead.nodes import review_result

        captured: dict[str, object] = {}

        class StubRegistry:
            def execute_sync(self, name, args):
                assert name == "dispatch_code_review"
                captured.update(args)
                return json.dumps({"verdict": "approved", "summary": "ok"})

        class StubLauncher:
            def find_live_instances(self, agent_id, task_id):
                assert agent_id == "code-review"
                assert task_id == "task-123"
                return [{
                    "container_name": "code-review-task-123-live",
                    "task_id": "task-123",
                    "agent_id": "code-review",
                }]

        class StubRegistryClient:
            def get_capability_definition(self, capability):
                assert capability == "review.code.check"
                return {"launch_spec": {"port": 8060}}

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())
        monkeypatch.setattr("framework.launcher.get_launcher", lambda: StubLauncher())
        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )

        result = await review_result(
            {
                "_task_id": "task-123",
                "pr_url": "https://github.com/org/repo/pull/1",
                "pr_number": 1,
                "repo_url": "https://github.com/org/repo",
                "dev_result": {"summary": "done", "prNumber": 1},
                "analysis_summary": "Implement CSTL-1",
                "workspace_path": "/tmp/workspace",
            }
        )

        assert captured["child_service_url"] == "http://code-review-task-123-live:8060"
        assert captured["child_container_name"] == "code-review-task-123-live"
        assert result["route"] == "approved"

    async def test_review_result_reuses_cached_cr_session_without_live_lookup(self, monkeypatch):
        from agents.team_lead import nodes as tl_nodes

        captured: dict[str, object] = {}
        tl_nodes._clear_cached_child_sessions("task-123")
        tl_nodes._cache_child_session(
            "task-123",
            "code-review",
            {
                "task_id": "task-code-review-1",
                "service_url": "http://code-review-task-1:8060",
                "container_name": "code-review-task-1",
                "agent_id": "code-review",
            },
        )

        class StubRegistry:
            def execute_sync(self, name, args):
                assert name == "dispatch_code_review"
                captured.update(args)
                return json.dumps({
                    "verdict": "approved",
                    "summary": "ok",
                    "_crSession": {
                        "service_url": "http://code-review-task-1:8060",
                        "container_name": "code-review-task-1",
                        "agent_id": "code-review",
                    },
                })

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())
        monkeypatch.setattr(
            "agents.team_lead.nodes._reuse_live_child_session",
            lambda **_: (_ for _ in ()).throw(AssertionError("live lookup should not run")),
        )

        result = await tl_nodes.review_result(
            {
                "_task_id": "task-123",
                "pr_url": "https://github.com/org/repo/pull/1",
                "pr_number": 1,
                "repo_url": "https://github.com/org/repo",
                "dev_result": {"summary": "done", "prNumber": 1},
                "analysis_summary": "Implement CSTL-1",
                "workspace_path": "/tmp/workspace",
            }
        )

        assert captured["child_service_url"] == "http://code-review-task-1:8060"
        assert result["cr_agent_session"]["service_url"] == "http://code-review-task-1:8060"
        tl_nodes._clear_cached_child_sessions("task-123")

    async def test_review_result_sends_keepalive_to_waiting_dev_session(self, monkeypatch):
        from agents.team_lead.nodes import review_result

        ping_calls: list[tuple[str, str, int]] = []

        class StubRegistry:
            def execute_sync(self, name, args):
                assert name == "dispatch_code_review"
                time.sleep(0.08)
                return json.dumps({"verdict": "approved", "summary": "ok"})

        async def fake_send_ping(self, base_url, task_id, estimated_remaining_wait_seconds=0):
            ping_calls.append((base_url, task_id, estimated_remaining_wait_seconds))

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())
        monkeypatch.setattr("framework.a2a.client.A2AClient.send_ping", fake_send_ping)
        monkeypatch.setenv("TEAM_LEAD_CHILD_KEEPALIVE_INTERVAL_SECONDS", "0.02")

        result = await review_result(
            {
                "_task_id": "task-123",
                "pr_url": "https://github.com/org/repo/pull/1",
                "pr_number": 1,
                "repo_url": "https://github.com/org/repo",
                "dev_result": {"summary": "done", "prNumber": 1},
                "dev_agent_session": {
                    "task_id": "web-dev-task-1",
                    "service_url": "http://web-dev-task-1:8050",
                    "container_name": "web-dev-task-1",
                    "agent_id": "web-dev",
                },
                "analysis_summary": "Implement CSTL-1",
                "workspace_path": "/tmp/workspace",
            }
        )

        assert result["route"] == "approved"
        assert any(call[:2] == ("http://web-dev-task-1:8050", "web-dev-task-1") for call in ping_calls)

    async def test_review_result_passes_child_permissions_not_parent_snapshot(self, monkeypatch):
        from agents.team_lead.nodes import review_result

        captured: dict[str, object] = {}

        class StubRegistry:
            def execute_sync(self, name, args):
                captured.update({"name": name, "args": args})
                return json.dumps({"verdict": "approved", "summary": "ok"})

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        result = await review_result(
            {
                "_task_id": "task-123",
                "pr_url": "https://github.com/org/repo/pull/1",
                "pr_number": 1,
                "repo_url": "https://github.com/org/repo",
                "dev_result": {"summary": "done", "prNumber": 1},
                "analysis_summary": "Implement CSTL-1",
                "workspace_path": "/tmp/workspace",
                "metadata": {
                    "permissions": {
                        "allowedTools": ["dispatch_code_review"],
                        "deniedTools": [],
                        "scm": "read",
                        "filesystem": "workspace-only",
                        "custom": {},
                    }
                },
                "revision_count": 0,
                "max_revisions": 3,
            }
        )

        assert result["route"] == "approved"
        assert captured["name"] == "dispatch_code_review"
        assert "scm_get_pr_diff" in captured["args"]["permissions"]["allowedTools"]
        assert "dispatch_code_review" not in captured["args"]["permissions"]["allowedTools"]

    async def test_review_result_uses_dev_result_repo_inputs_when_state_missing(self, monkeypatch):
        from agents.team_lead.nodes import review_result

        captured: dict[str, object] = {}

        class StubRegistry:
            def execute_sync(self, name, args):
                captured.update(args)
                return json.dumps({"verdict": "approved", "summary": "ok"})

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        result = await review_result(
            {
                "_task_id": "task-123",
                "pr_url": "https://github.com/org/repo/pull/85",
                "pr_number": 0,
                "repo_url": "",
                "dev_result": {
                    "summary": "done",
                    "prNumber": 85,
                    "repoUrl": "https://github.com/org/repo",
                    "changedFiles": ["src/App.jsx"],
                },
                "analysis_summary": "Implement CSTL-3",
                "workspace_path": "/tmp/workspace",
            }
        )

        assert captured["repo_url"] == "https://github.com/org/repo"
        assert captured["pr_number"] == 85
        assert captured["changed_files"] == ["src/App.jsx"]
        assert result["route"] == "approved"

    async def test_review_result_escalates_manual_review_required(self, monkeypatch):
        from agents.team_lead.nodes import review_result

        class StubRegistry:
            def execute_sync(self, name, args):
                assert name == "dispatch_code_review"
                return json.dumps({
                    "verdict": "rejected",
                    "summary": "Manual review required for large PR",
                    "manual_review_required": True,
                })

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        result = await review_result(
            {
                "_task_id": "task-123",
                "pr_url": "https://github.com/org/repo/pull/85",
                "pr_number": 85,
                "repo_url": "https://github.com/org/repo",
                "dev_result": {
                    "summary": "done",
                    "prNumber": 85,
                    "repoUrl": "https://github.com/org/repo",
                },
                "analysis_summary": "Implement a large refactor",
                "workspace_path": "/tmp/workspace",
                "revision_count": 0,
                "max_revisions": 3,
            }
        )

        assert result["manual_review_required"] is True
        assert result["route"] == "need_user_input"

    async def test_validate_readiness_routes_to_missing_info_for_retryable_context(self, tmp_path):
        from agents.team_lead.nodes import validate_readiness

        repo_path = tmp_path / "repo"
        repo_path.mkdir()
        (repo_path / "README.md").write_text("ok", encoding="utf-8")

        result = await validate_readiness({
            "repo_cloned": True,
            "repo_path": str(repo_path),
            "jira_key": "",
            "analysis_summary": "Implement a UI task",
            "design_context": {"source": "fixture"},
            "tech_stack": [],
            "readiness_attempts": 0,
        })

        assert result["route"] == "missing_info"
        assert result["readiness_validated"] is False

    async def test_report_success_propagates_screenshot_evidence(self):
        from agents.team_lead.nodes import report_success

        result = await report_success({
            "pr_url": "https://github.com/org/repo/pull/1",
            "branch_name": "feature/ui",
            "analysis_summary": "Implemented UI",
            "review_verdict": "approved",
            "revision_count": 0,
            "jira_in_review": True,
            "screenshot_included": True,
            "screenshot_uploaded": True,
        })

        assert result["screenshot_included"] is True
        assert result["screenshot_uploaded"] is True

    async def test_report_success_acknowledges_and_cleans_dev_agent(self, monkeypatch):
        from agents.team_lead.nodes import report_success

        captured = {"ack": [], "destroy": []}

        async def fake_send_ack(self, base_url, task_id, exit_reason="task_completed_success", orchestrator_task_id=""):
            captured["ack"].append((base_url, task_id))

        class StubLauncher:
            def destroy_instance(self, agent_id, container_name):
                captured["destroy"].append((agent_id, container_name))

        monkeypatch.setattr("framework.a2a.client.A2AClient.send_ack", fake_send_ack)
        monkeypatch.setattr("framework.launcher.get_launcher", lambda: StubLauncher())

        result = await report_success({
            "_task_id": "task-team-lead",
            "pr_url": "https://github.com/org/repo/pull/1",
            "branch_name": "feature/ui",
            "analysis_summary": "Implemented UI",
            "review_verdict": "approved",
            "revision_count": 0,
            "jira_in_review": True,
            "screenshot_included": True,
            "screenshot_uploaded": True,
            "dev_agent_session": {
                "task_id": "task-web-dev-1",
                "service_url": "http://web-dev-task-1:8050",
                "container_name": "web-dev-task-1",
                "agent_id": "web-dev",
            },
            "cr_agent_session": {
                "task_id": "task-code-review-1",
                "service_url": "http://code-review-task-1:8060",
                "container_name": "code-review-task-1",
                "agent_id": "code-review",
            },
        })

        assert captured["ack"] == [
            ("http://web-dev-task-1:8050", "task-web-dev-1"),
            ("http://code-review-task-1:8060", "task-code-review-1"),
        ]
        assert captured["destroy"] == [
            ("web-dev", "web-dev-task-1"),
            ("code-review", "code-review-task-1"),
        ]
        assert result["dev_agent_acknowledged"] is True
        assert result["dev_agent_cleaned_up"] is True
        assert result["cr_agent_acknowledged"] is True
        assert result["cr_agent_cleaned_up"] is True
        assert result["dev_agent_session"] == {}
        assert result["cr_agent_session"] == {}

    async def test_request_revision_acknowledges_and_cleans_dev_agent(self, monkeypatch):
        """request_revision should NOT ACK or destroy the dev agent (container reuse)."""
        from agents.team_lead.nodes import request_revision

        captured = {"ack": [], "destroy": []}

        async def fake_send_ack(self, base_url, task_id, exit_reason="task_completed_success", orchestrator_task_id=""):
            captured["ack"].append((base_url, task_id))

        class StubLauncher:
            def destroy_instance(self, agent_id, container_name):
                captured["destroy"].append((agent_id, container_name))

        monkeypatch.setattr("framework.a2a.client.A2AClient.send_ack", fake_send_ack)
        monkeypatch.setattr("framework.launcher.get_launcher", lambda: StubLauncher())

        result = await request_revision({
            "_task_id": "task-team-lead",
            "review_result": {
                "summary": "Needs spacing fix",
                "comments": [{"severity": "medium", "message": "Fix spacing"}],
            },
            "dev_agent_session": {
                "task_id": "task-web-dev-1",
                "service_url": "http://web-dev-task-1:8050",
                "container_name": "web-dev-task-1",
                "agent_id": "web-dev",
            },
        })

        # No ACK or cleanup during revision — container is reused
        assert captured["ack"] == []
        assert captured["destroy"] == []
        assert "dev_agent_acknowledged" not in result
        assert "dev_agent_cleaned_up" not in result

    async def test_request_revision_posts_jira_and_inline_pr_comments(self, monkeypatch):
        from agents.team_lead.nodes import request_revision

        calls = []

        class StubRegistry:
            def execute_sync(self, name, args):
                calls.append((name, args))
                return json.dumps({"ok": True})

        monkeypatch.setattr("framework.tools.registry.get_registry", lambda: StubRegistry())

        await request_revision({
            "_task_id": "task-team-lead",
            "jira_key": "CSTL-1",
            "pr_url": "https://github.com/fihtony/english-study-hub/pull/93",
            "pr_number": 93,
            "repo_url": "https://github.com/fihtony/english-study-hub",
            "review_result": {
                "summary": "Fix review findings",
                "comments": [
                    {
                        "severity": "high",
                        "message": "Use dynamic year.",
                        "file": "src/components/Footer.jsx",
                        "line": 5,
                    },
                    {
                        "severity": "medium",
                        "message": "Replace dead links.",
                        "file": "src/components/Hero.jsx",
                        "line": 22,
                    },
                ],
            },
        })

        assert calls[0][0] == "jira_comment"
        assert calls[0][1]["ticket_key"] == "CSTL-1"
        assert calls[1][0] == "scm_add_pr_inline_comment"
        assert calls[1][1]["repo_url"] == "https://github.com/fihtony/english-study-hub"
        assert calls[1][1]["pr_number"] == 93
        assert calls[1][1]["file_path"] == "src/components/Footer.jsx"
        assert calls[2][0] == "scm_add_pr_inline_comment"
        assert calls[2][1]["file_path"] == "src/components/Hero.jsx"

    def test_team_lead_inline_comment_tool_dispatches_via_a2a(self, monkeypatch):
        from agents.team_lead.tools import SCMAddPRInlineComment

        captured = {}

        def fake_dispatch_sync(url, capability, message_parts, metadata, timeout=120):
            captured.update({
                "url": url,
                "capability": capability,
                "message_parts": message_parts,
                "metadata": metadata,
                "timeout": timeout,
            })
            return {
                "task": {
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [{"parts": [{"text": json.dumps({"ok": True, "fallback": False})}]}],
                }
            }

        monkeypatch.setattr("agents.team_lead.tools._resolve_agent_url", lambda *args: "http://scm:8020")
        monkeypatch.setattr("framework.a2a.client.dispatch_sync", fake_dispatch_sync)

        result = SCMAddPRInlineComment().execute_sync(
            repo_url="https://github.com/fihtony/english-study-hub",
            pr_number=93,
            file_path="src/App.jsx",
            line=17,
            comment="[HIGH] Fix this.",
            commit_id="abc123",
            task_id="task-team-lead",
        )

        assert json.loads(result.output) == {"ok": True, "fallback": False}
        assert captured["url"] == "http://scm:8020"
        assert captured["capability"] == "scm.pr.comment.inline"
        assert captured["message_parts"] == [{"text": "[HIGH] Fix this."}]
        assert captured["metadata"] == {
            "repoUrl": "https://github.com/fihtony/english-study-hub",
            "prNumber": 93,
            "filePath": "src/App.jsx",
            "line": 17,
            "comment": "[HIGH] Fix this.",
            "commitId": "abc123",
            "taskId": "task-team-lead",
        }

    def test_callback_propagates_screenshot_evidence(self, monkeypatch):
        from agents.team_lead.agent import _send_callback

        captured = {}

        class StubResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

        def fake_urlopen(request, timeout):
            captured["timeout"] = timeout
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return StubResponse()

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

        _send_callback(
            "http://compass/tasks/task-123/callbacks",
            "task-team-lead",
            {
                "report_summary": "done",
                "pr_url": "https://github.com/org/repo/pull/1",
                "branch_name": "feature/ui",
                "jira_in_review": True,
                "screenshot_included": True,
                "screenshot_uploaded": True,
            },
            "team-lead",
        )

        metadata = captured["payload"]["artifacts"][0]["metadata"]
        assert metadata["screenshotIncluded"] is True
        assert metadata["screenshotUploaded"] is True


class TestTeamLeadTools:
    def test_cross_agent_tools_accept_workflow_metadata(self, monkeypatch):
        from agents.team_lead.tools import (
            CloneRepo,
            DispatchCodeReview,
            DispatchWebDev,
            FetchDesign,
            FetchJiraTicket,
        )

        calls = []

        class StubRegistryClient:
            def discover(self, capability):
                return f"http://stub/{capability}"

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )

        def _dispatch_sync(**kwargs):
            calls.append(kwargs)
            return {
                "task": {
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [
                        {
                            "parts": [{"text": json.dumps({"status": "ok"})}],
                            "metadata": {
                                "prUrl": "https://example.test/pr/1",
                                "branch": "feature/cstl-2",
                                "jiraInReview": True,
                            },
                        }
                    ],
                }
            }

        monkeypatch.setattr("framework.a2a.client.dispatch_sync", _dispatch_sync)

        FetchJiraTicket().execute_sync(
            ticket_key="CSTL-2",
            task_id="task-123",
            workspace_path="/tmp/workspace",
        )
        FetchDesign().execute_sync(
            stitch_project_id="proj-1",
            stitch_screen_id="screen-2",
            task_id="task-123",
            workspace_path="/tmp/workspace",
        )
        CloneRepo().execute_sync(
            repo_url="https://example.test/org/repo.git",
            target_path="/tmp/workspace/scm/repo",
            task_id="task-123",
        )
        DispatchWebDev().execute_sync(
            task_description="Implement CSTL-2",
            workspace_path="/tmp/workspace",
            orchestrator_task_id="task-123",
        )
        DispatchCodeReview().execute_sync(
            pr_url="https://example.test/pr/1",
            workspace_path="/tmp/workspace",
            orchestrator_task_id="task-123",
        )

        assert calls[0]["metadata"] == {
            "ticketKey": "CSTL-2",
            "taskId": "task-123",
            "workspacePath": "/tmp/workspace",
        }
        assert calls[1]["metadata"] == {
            "stitchProjectId": "proj-1",
            "stitchScreenId": "screen-2",
            "screenName": "",
            "taskId": "task-123",
            "workspacePath": "/tmp/workspace",
        }
        assert calls[2]["metadata"] == {
            "repoUrl": "https://example.test/org/repo.git",
            "targetPath": "/tmp/workspace/scm/repo",
            "taskId": "task-123",
        }
        assert calls[3]["metadata"]["orchestratorTaskId"] == "task-123"
        assert calls[3]["metadata"]["workspacePath"] == "/tmp/workspace"
        assert calls[4]["metadata"]["orchestratorTaskId"] == "task-123"
        assert calls[4]["metadata"]["workspacePath"] == "/tmp/workspace"

    def test_dispatch_web_dev_propagates_failed_task_state(self, monkeypatch):
        from agents.team_lead.tools import DispatchWebDev

        class StubRegistryClient:
            def discover(self, capability):
                return "http://web-dev:8050"

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )

        monkeypatch.setattr(
            "framework.a2a.client.dispatch_sync",
            lambda **kwargs: {
                "task": {
                    "status": {
                        "state": "TASK_STATE_FAILED",
                        "message": {"parts": [{"text": "Web Dev task failed"}]},
                    },
                    "artifacts": [],
                }
            },
        )

        result = DispatchWebDev().execute_sync(task_description="Implement live e2e change")
        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert payload["state"] == "TASK_STATE_FAILED"

    def test_dispatch_web_dev_launches_per_task_agent(self, monkeypatch):
        from agents.team_lead.tools import DispatchWebDev

        calls = {"dispatch": [], "launch": [], "destroy": []}

        class StubRegistryClient:
            def discover(self, capability):
                return ""

            def get_capability_definition(self, capability):
                return {
                    "agent_id": "web-dev",
                    "name": "Web Dev Agent",
                    "execution_mode": "per-task",
                    "launch_spec": {
                        "image": "constellation-v2-web-dev:latest",
                        "port": 8050,
                    },
                }

        class StubLauncher:
            def launch_instance(self, definition, task_id, launch_overrides=None):
                calls["launch"].append((definition, task_id, launch_overrides))
                return {
                    "service_url": "http://launched-web-dev:8050",
                    "container_name": "web-dev-task-1234",
                }

            def destroy_instance(self, agent_id, container_name):
                calls["destroy"].append((agent_id, container_name))

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )
        monkeypatch.setattr("agents.team_lead.tools.get_launcher", lambda: StubLauncher())
        monkeypatch.setattr("agents.team_lead.tools._wait_for_agent_ready", lambda *args, **kwargs: None)

        def _dispatch_sync(**kwargs):
            calls["dispatch"].append(kwargs)
            return {
                "task": {
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [
                        {
                            "parts": [{"text": "Dev task completed."}],
                            "metadata": {
                                "prUrl": "https://example.test/pr/2",
                                "prNumber": 2,
                                "repoUrl": "https://example.test/org/repo.git",
                                "branch": "feature/cstl-2",
                                "changedFiles": ["src/App.jsx"],
                                "jiraInReview": True,
                                "screenshotIncluded": True,
                                "screenshotUploaded": True,
                            },
                        }
                    ],
                }
            }

        monkeypatch.setattr("framework.a2a.client.dispatch_sync", _dispatch_sync)

        result = DispatchWebDev().execute_sync(
            task_description="Implement CSTL-2",
            workspace_path="/tmp/workspace",
            orchestrator_task_id="task-123",
        )

        payload = json.loads(result.output)
        assert payload["status"] == "completed"

    def test_dispatch_web_dev_propagates_revision_metadata(self, monkeypatch):
        from agents.team_lead.tools import DispatchWebDev

        calls = []

        class StubRegistryClient:
            def discover(self, capability):
                return "http://web-dev:8050"

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )

        def _dispatch_sync(**kwargs):
            calls.append(kwargs)
            return {
                "task": {
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [{"parts": [{"text": "done"}], "metadata": {}}],
                }
            }

        monkeypatch.setattr("framework.a2a.client.dispatch_sync", _dispatch_sync)

        DispatchWebDev().execute_sync(
            task_description="Apply requested revision",
            repo_url="https://github.com/org/repo",
            repo_path="/tmp/workspace/scm/repo",
            branch_name="feature/proj-1-task",
            workspace_path="/tmp/workspace",
            revision_feedback="Fix the review findings",
            review_report_path="code-review/review-report-1.json",
            revision_mode=True,
            revision_round=2,
            existing_pr_url="https://github.com/org/repo/pull/42",
            existing_pr_number=42,
            existing_branch="feature/proj-1-task",
            orchestrator_task_id="task-123",
        )

        metadata = calls[0]["metadata"]
        assert metadata["revisionFeedback"] == "Fix the review findings"
        assert metadata["reviewReportPath"] == "code-review/review-report-1.json"
        assert metadata["revisionMode"] is True
        assert metadata["revisionRound"] == 2
        assert metadata["existingPrUrl"] == "https://github.com/org/repo/pull/42"
        assert metadata["existingPrNumber"] == 42
        assert metadata["existingBranch"] == "feature/proj-1-task"

    def test_dispatch_web_dev_uses_configurable_timeout(self, monkeypatch):
        from agents.team_lead.tools import DispatchWebDev

        class StubRegistryClient:
            def discover(self, capability):
                return "http://web-dev:8050"

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )
        monkeypatch.setenv("TEAM_LEAD_WEB_DEV_TIMEOUT_SECONDS", "5400")

        captured: dict[str, Any] = {}

        def _dispatch_sync(**kwargs):
            captured.update(kwargs)
            return {
                "task": {
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [],
                }
            }

        monkeypatch.setattr("framework.a2a.client.dispatch_sync", _dispatch_sync)

        DispatchWebDev().execute_sync(task_description="Implement CSTL-1")

        assert captured["timeout"] == 5400

    def test_dispatch_code_review_uses_configurable_timeout(self, monkeypatch):
        from agents.team_lead.tools import DispatchCodeReview

        class StubRegistryClient:
            def discover(self, capability):
                return "http://code-review:8050"

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )
        monkeypatch.setenv("TEAM_LEAD_CODE_REVIEW_TIMEOUT_SECONDS", "1800")

        captured: dict[str, Any] = {}

        def _dispatch_sync(**kwargs):
            captured.update(kwargs)
            return {
                "task": {
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [],
                }
            }

        monkeypatch.setattr("framework.a2a.client.dispatch_sync", _dispatch_sync)

        DispatchCodeReview().execute_sync(pr_url="https://example.test/pr/1")

        assert captured["timeout"] == 1800

    def test_dispatch_code_review_launches_per_task_agent(self, monkeypatch):
        from agents.team_lead.tools import DispatchCodeReview

        calls = {"dispatch": [], "launch": [], "destroy": []}

        class StubRegistryClient:
            def discover(self, capability):
                return ""

            def get_capability_definition(self, capability):
                return {
                    "agent_id": "code-review",
                    "name": "Code Review Agent",
                    "execution_mode": "per-task",
                    "launch_spec": {
                        "image": "constellation-v2-code-review:latest",
                        "port": 8060,
                    },
                }

        class StubLauncher:
            def launch_instance(self, definition, task_id, launch_overrides=None):
                calls["launch"].append((definition, task_id, launch_overrides))
                return {
                    "service_url": "http://launched-code-review:8060",
                    "container_name": "code-review-task-1234",
                }

            def destroy_instance(self, agent_id, container_name):
                calls["destroy"].append((agent_id, container_name))

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )
        monkeypatch.setattr("agents.team_lead.tools.get_launcher", lambda: StubLauncher())
        monkeypatch.setattr("agents.team_lead.tools._wait_for_agent_ready", lambda *args, **kwargs: None)

        def _dispatch_sync(**kwargs):
            calls["dispatch"].append(kwargs)
            return {
                "task": {
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [
                        {
                            "parts": [{"text": json.dumps({"verdict": "approved", "summary": "ok"})}],
                            "metadata": {"agentId": "code-review"},
                        }
                    ],
                }
            }

        monkeypatch.setattr("framework.a2a.client.dispatch_sync", _dispatch_sync)

        result = DispatchCodeReview().execute_sync(
            pr_url="https://example.test/pr/1",
            workspace_path="/tmp/workspace",
            orchestrator_task_id="task-123",
            task_id="task-123",
        )

        payload = json.loads(result.output)
        assert payload["verdict"] == "approved"
        assert calls["launch"][0][1] == "task-123"
        assert calls["dispatch"][0]["url"] == "http://launched-code-review:8060"
        assert calls["dispatch"][0]["metadata"]["orchestratorTaskId"] == "task-123"
        assert calls["dispatch"][0]["metadata"]["workspacePath"] == "/tmp/workspace"
        # Container is preserved (not destroyed) — lifecycle manager handles exit
        assert calls["destroy"] == []
        # Container info is embedded so Team Lead can track the CR session
        assert payload["_crSession"]["task_id"] == "task-review-1"
        assert payload["_crSession"]["service_url"] == "http://launched-code-review:8060"

    def test_dispatch_code_review_derives_launch_task_id_from_workspace_path(self, monkeypatch):
        from agents.team_lead.tools import DispatchCodeReview

        calls = {"launch": []}

        class StubRegistryClient:
            def discover(self, capability):
                return ""

            def get_capability_definition(self, capability):
                return {
                    "agent_id": "code-review",
                    "name": "Code Review Agent",
                    "execution_mode": "per-task",
                    "launch_spec": {
                        "image": "constellation-v2-code-review:latest",
                        "port": 8060,
                    },
                }

        class StubLauncher:
            def launch_instance(self, definition, task_id, launch_overrides=None):
                calls["launch"].append(task_id)
                return {
                    "service_url": "http://launched-code-review:8060",
                    "container_name": "code-review-task-derived",
                }

            def destroy_instance(self, agent_id, container_name):
                return None

        monkeypatch.setattr(
            "framework.registry_client.RegistryClient.from_config",
            classmethod(lambda cls: StubRegistryClient()),
        )
        monkeypatch.setattr("agents.team_lead.tools.get_launcher", lambda: StubLauncher())
        monkeypatch.setattr("agents.team_lead.tools._wait_for_agent_ready", lambda *args, **kwargs: None)
        monkeypatch.setattr(
            "framework.a2a.client.dispatch_sync",
            lambda **kwargs: {
                "task": {
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [{"parts": [{"text": json.dumps({"verdict": "approved"})}]}],
                }
            },
        )

        DispatchCodeReview().execute_sync(
            pr_url="https://example.test/pr/1",
            workspace_path="/app/artifacts/task-39c4f352bb20",
        )

        assert calls["launch"] == ["task-39c4f352bb20"]

    def test_dispatch_code_review_rejects_artifact_root_workspace(self, monkeypatch):
        from agents.team_lead.tools import DispatchCodeReview

        monkeypatch.setenv("ARTIFACT_ROOT", "/app/artifacts")

        with pytest.raises(ValueError, match="single task workspace root"):
            DispatchCodeReview().execute_sync(
                pr_url="https://example.test/pr/1",
                workspace_path="/app/artifacts",
            )
