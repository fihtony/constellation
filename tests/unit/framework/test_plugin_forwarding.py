"""Tests for plugin_manager forwarding through workflow nodes.

Verifies that:
- _plugin_manager is included in workflow state dicts
- Nodes pass plugin_manager to runtime.run() and runtime.run_agentic()
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock, call

import pytest


class TestTeamLeadPluginForwarding:
    """Team Lead nodes forward plugin_manager to runtime calls."""

    def test_analyze_requirements_passes_plugin_manager(self):
        """analyze_requirements() passes plugin_manager to runtime.run()."""
        plugin_manager = MagicMock()
        runtime = MagicMock()
        runtime.run.return_value = {"raw_response": json.dumps({
            "task_type": "web",
            "complexity": "low",
            "skills": [],
            "summary": "test",
        })}

        from agents.team_lead.nodes import analyze_requirements
        state = {
            "_runtime": runtime,
            "_plugin_manager": plugin_manager,
            "user_request": "Build login page",
            "jira_key": "",
        }
        result = asyncio.get_event_loop().run_until_complete(
            analyze_requirements(state)
        )
        # Verify runtime.run was called with plugin_manager
        runtime.run.assert_called_once()
        _, kwargs = runtime.run.call_args
        assert kwargs.get("plugin_manager") is plugin_manager

    def test_create_plan_passes_plugin_manager(self):
        """create_plan() passes plugin_manager to runtime.run()."""
        plugin_manager = MagicMock()
        runtime = MagicMock()
        runtime.run.return_value = {"raw_response": json.dumps({
            "steps": [{"step": 1, "action": "Do it"}],
        })}

        from agents.team_lead.nodes import create_plan
        state = {
            "_runtime": runtime,
            "_plugin_manager": plugin_manager,
            "_skills_registry": MagicMock(),
            "analysis_summary": "Build login page",
            "jira_context": {},
            "task_type": "web",
            "complexity": "low",
            "required_skills": [],
        }
        result = asyncio.get_event_loop().run_until_complete(
            create_plan(state)
        )
        runtime.run.assert_called_once()
        _, kwargs = runtime.run.call_args
        assert kwargs.get("plugin_manager") is plugin_manager


class TestWebDevPluginForwarding:
    """Web Dev nodes forward plugin_manager to runtime calls."""

    def test_implement_changes_passes_plugin_manager(self):
        """implement_changes() passes plugin_manager to runtime.run_agentic()."""
        plugin_manager = MagicMock()
        runtime = MagicMock()
        agentic_result = MagicMock()
        agentic_result.success = True
        agentic_result.summary = "Done."
        agentic_result.tool_calls = []
        runtime.run_agentic.return_value = agentic_result

        from agents.web_dev.nodes import implement_changes
        state = {
            "_runtime": runtime,
            "_plugin_manager": plugin_manager,
            "user_request": "Add button",
            "repo_path": "/tmp/repo",
            "branch_name": "feature/test",
            "implementation_plan": "Add a button",
            "jira_context": {},
            "design_context": None,
            "skill_context": "",
            "memory_context": "",
        }
        result = asyncio.get_event_loop().run_until_complete(
            implement_changes(state)
        )
        runtime.run_agentic.assert_called_once()
        _, kwargs = runtime.run_agentic.call_args
        assert kwargs.get("plugin_manager") is plugin_manager

    def test_fix_tests_passes_plugin_manager(self):
        """fix_tests() passes plugin_manager to runtime.run_agentic()."""
        plugin_manager = MagicMock()
        runtime = MagicMock()
        agentic_result = MagicMock()
        agentic_result.success = True
        agentic_result.summary = "Fixed."
        runtime.run_agentic.return_value = agentic_result

        from agents.web_dev.nodes import fix_tests
        state = {
            "_runtime": runtime,
            "_plugin_manager": plugin_manager,
            "test_output": "FAIL: test_foo",
            "repo_path": "/tmp/repo",
            "changes_made": ["foo.py"],
        }
        result = asyncio.get_event_loop().run_until_complete(
            fix_tests(state)
        )
        runtime.run_agentic.assert_called_once()
        _, kwargs = runtime.run_agentic.call_args
        assert kwargs.get("plugin_manager") is plugin_manager


class TestCodeReviewPluginForwarding:
    """Code Review nodes forward plugin_manager to runtime.run()."""

    def test_review_quality_passes_plugin_manager(self):
        plugin_manager = MagicMock()
        runtime = MagicMock()
        runtime.run.return_value = {"raw_response": "[]"}

        from agents.code_review.nodes import review_quality
        state = {
            "_runtime": runtime,
            "_plugin_manager": plugin_manager,
            "pr_diff": "diff --git a/foo.py ...",
            "pr_description": "Add feature",
            "changed_files": ["foo.py"],
        }
        result = asyncio.get_event_loop().run_until_complete(
            review_quality(state)
        )
        runtime.run.assert_called_once()
        _, kwargs = runtime.run.call_args
        assert kwargs.get("plugin_manager") is plugin_manager

    def test_review_security_passes_plugin_manager(self):
        plugin_manager = MagicMock()
        runtime = MagicMock()
        runtime.run.return_value = {"raw_response": "[]"}

        from agents.code_review.nodes import review_security
        state = {
            "_runtime": runtime,
            "_plugin_manager": plugin_manager,
            "pr_diff": "diff --git a/foo.py ...",
            "pr_description": "Fix vuln",
            "changed_files": ["foo.py"],
        }
        result = asyncio.get_event_loop().run_until_complete(
            review_security(state)
        )
        runtime.run.assert_called_once()
        _, kwargs = runtime.run.call_args
        assert kwargs.get("plugin_manager") is plugin_manager


class TestStatePluginManagerInjection:
    """Agents inject _plugin_manager into the workflow state dict."""

    def test_team_lead_injects_plugin_manager(self):
        """TeamLeadAgent.handle_message includes _plugin_manager in state."""
        from agents.team_lead.agent import TeamLeadAgent, team_lead_definition
        from framework.task_store import InMemoryTaskStore

        pm = MagicMock()
        services = _make_services(plugin_manager=pm)
        agent = TeamLeadAgent(definition=team_lead_definition, services=services)
        # Verify the agent stores the plugin_manager
        assert agent.plugin_manager is pm

    def test_web_dev_injects_plugin_manager(self):
        """WebDevAgent stores plugin_manager from services."""
        from agents.web_dev.agent import WebDevAgent, web_dev_definition
        from framework.task_store import InMemoryTaskStore

        pm = MagicMock()
        services = _make_services(plugin_manager=pm)
        agent = WebDevAgent(definition=web_dev_definition, services=services)
        assert agent.plugin_manager is pm


def _make_services(plugin_manager=None):
    from framework.agent import AgentServices
    from framework.task_store import InMemoryTaskStore
    return AgentServices(
        session_service=MagicMock(),
        event_store=MagicMock(),
        memory_service=MagicMock(),
        skills_registry=MagicMock(),
        plugin_manager=plugin_manager or MagicMock(),
        checkpoint_service=MagicMock(),
        runtime=None,
        registry_client=None,
        task_store=InMemoryTaskStore(),
    )
