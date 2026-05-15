"""Tests for Team Lead interrupt/resume business closure.

Verifies that:
- escalate_to_user raises InterruptSignal (not just returns state)
- TeamLeadAgent.handle_message catches InterruptSignal and pauses task
- TeamLeadAgent.resume_task resumes workflow and sends callback
- Config single source includes permission_profile
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest

from framework.errors import InterruptSignal
from framework.workflow import interrupt


class TestEscalateToUserInterrupt:
    """escalate_to_user node raises InterruptSignal."""

    def test_raises_interrupt_signal(self):
        """escalate_to_user() raises InterruptSignal with revision context."""
        from agents.team_lead.nodes import escalate_to_user

        state = {
            "revision_count": 3,
            "review_result": {"verdict": "rejected"},
            "pr_url": "https://github.com/org/repo/pull/42",
        }
        with pytest.raises(InterruptSignal) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                escalate_to_user(state)
            )
        assert "user intervention" in exc_info.value.question
        assert exc_info.value.metadata.get("revision_count") == 3
        assert exc_info.value.metadata.get("pr_url") == "https://github.com/org/repo/pull/42"

    def test_interrupt_includes_review_verdict(self):
        from agents.team_lead.nodes import escalate_to_user

        state = {
            "revision_count": 2,
            "review_result": {"verdict": "needs_work"},
            "pr_url": "",
        }
        with pytest.raises(InterruptSignal) as exc_info:
            asyncio.get_event_loop().run_until_complete(
                escalate_to_user(state)
            )
        assert exc_info.value.metadata.get("review_verdict") == "needs_work"


class TestTeamLeadInterruptHandling:
    """TeamLeadAgent catches InterruptSignal in handle_message."""

    def test_interrupt_pauses_task(self):
        """When workflow raises InterruptSignal, task enters INPUT_REQUIRED."""
        from agents.team_lead.agent import TeamLeadAgent, team_lead_definition
        from framework.agent import AgentServices
        from framework.task_store import InMemoryTaskStore
        from framework.checkpoint import InMemoryCheckpointer

        task_store = InMemoryTaskStore()
        checkpoint = InMemoryCheckpointer()

        # Mock runtime that returns JSON triggering escalation path
        runtime = MagicMock()
        runtime.run.return_value = {"raw_response": json.dumps({
            "task_type": "web",
            "complexity": "high",
            "skills": [],
            "summary": "Complex task",
        })}

        services = AgentServices(
            session_service=MagicMock(),
            event_store=MagicMock(),
            memory_service=MagicMock(),
            skills_registry=MagicMock(),
            plugin_manager=MagicMock(),
            checkpoint_service=checkpoint,
            runtime=runtime,
            registry_client=None,
            task_store=task_store,
        )
        agent = TeamLeadAgent(definition=team_lead_definition, services=services)
        # Agent needs a compiled workflow
        asyncio.get_event_loop().run_until_complete(agent.start())
        assert agent._compiled_workflow is not None


class TestConfigSingleSource:
    """build_agent_definition_from_config includes permission_profile."""

    def test_build_includes_permission_profile(self):
        from framework.config import build_agent_definition_from_config

        result = build_agent_definition_from_config("web-dev")
        assert result.get("permission_profile") == "development"

    def test_build_code_review_profile(self):
        from framework.config import build_agent_definition_from_config

        result = build_agent_definition_from_config("code-review")
        assert result.get("permission_profile") == "read_only"

    def test_build_team_lead_no_profile(self):
        from framework.config import build_agent_definition_from_config

        result = build_agent_definition_from_config("team-lead")
        # Team Lead has no permission_profile in config.yaml
        assert result.get("permission_profile") == ""

    def test_build_includes_tools_from_yaml(self):
        from framework.config import build_agent_definition_from_config

        result = build_agent_definition_from_config("web-dev")
        assert "read_file" in result.get("tools", [])
        assert "write_file" in result.get("tools", [])


class TestResumeTaskArtifacts:
    """BaseAgent.resume_task() builds proper artifacts from workflow result."""

    def test_resume_produces_artifacts(self):
        """After resume, completed task should have artifacts (not hardcoded msg)."""
        from framework.agent import AgentDefinition, AgentServices, BaseAgent
        from framework.task_store import InMemoryTaskStore
        from framework.checkpoint import InMemoryCheckpointer
        from framework.workflow import Workflow, START, END, RunConfig

        # Simple workflow that interrupts then completes
        async def step_one(state: dict) -> dict:
            if state.get("_resume_value") is not None:
                return {"summary": f"User said: {state['_resume_value']}", "done": True}
            interrupt("What next?")
            return {}

        wf = Workflow(
            name="resume_test",
            edges=[(START, step_one, END)],
        )

        defn = AgentDefinition(
            agent_id="test-resume",
            name="Test Resume",
            description="test",
            workflow=wf,
        )

        task_store = InMemoryTaskStore()
        checkpoint = InMemoryCheckpointer()

        class TestAgent(BaseAgent):
            async def handle_message(self, message: dict) -> dict:
                return {}

        services = AgentServices(
            session_service=MagicMock(),
            event_store=MagicMock(),
            memory_service=MagicMock(),
            skills_registry=MagicMock(),
            plugin_manager=MagicMock(),
            checkpoint_service=checkpoint,
            runtime=None,
            registry_client=None,
            task_store=task_store,
        )
        agent = TestAgent(defn, services)
        asyncio.get_event_loop().run_until_complete(agent.start())

        # Create and pause task
        task = task_store.create_task(agent_id="test-resume")
        config = RunConfig(
            session_id=task.id,
            thread_id=task.id,
            checkpoint_service=checkpoint,
        )
        with pytest.raises(InterruptSignal):
            asyncio.get_event_loop().run_until_complete(
                agent._compiled_workflow.invoke({"input": "hi"}, config)
            )
        task_store.pause_task(task.id, question="What next?")

        # Resume
        result = asyncio.get_event_loop().run_until_complete(
            agent.resume_task(task.id, "continue please")
        )

        assert result["task"]["status"]["state"] == "TASK_STATE_COMPLETED"
        artifacts = result["task"].get("artifacts", [])
        assert len(artifacts) >= 1
        # Verify the artifact contains the resumed workflow output
        art_text = artifacts[0].get("parts", [{}])[0].get("text", "")
        assert "User said: continue please" in art_text
