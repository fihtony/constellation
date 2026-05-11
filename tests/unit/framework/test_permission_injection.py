"""Tests for unified permission injection via BaseAgent.start().

Verifies that:
- BaseAgent._load_permission_engine() loads from permission_profile
- PermissionEngine is bound to the global ToolRegistry automatically
- _build_run_config() includes the permission_engine
- Agents with no permission_profile skip loading (no error)
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode
from framework.task_store import InMemoryTaskStore
from framework.tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_services(runtime=None):
    return AgentServices(
        session_service=MagicMock(),
        event_store=MagicMock(),
        memory_service=MagicMock(),
        skills_registry=MagicMock(),
        plugin_manager=MagicMock(),
        checkpoint_service=MagicMock(),
        runtime=runtime,
        registry_client=None,
        task_store=InMemoryTaskStore(),
    )


class _DummyAgent(BaseAgent):
    async def handle_message(self, message: dict) -> dict:
        return {}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPermissionAutoLoading:
    """Permission loading from AgentDefinition.permission_profile."""

    def test_load_development_profile(self):
        """Agent with permission_profile='development' loads the YAML."""
        defn = AgentDefinition(
            agent_id="test-dev",
            name="Test Dev",
            description="test",
            permission_profile="development",
        )
        agent = _DummyAgent(defn, _make_services())
        agent._load_permission_engine()
        assert agent._permission_engine is not None
        assert agent._permission_engine.check_scm_write() is True

    def test_load_read_only_profile(self):
        """Agent with permission_profile='read_only' loads the YAML."""
        defn = AgentDefinition(
            agent_id="test-ro",
            name="Test RO",
            description="test",
            permission_profile="read_only",
        )
        agent = _DummyAgent(defn, _make_services())
        agent._load_permission_engine()
        assert agent._permission_engine is not None
        assert agent._permission_engine.check_scm_write() is False

    def test_no_profile_no_error(self):
        """Agent with empty permission_profile does not load anything."""
        defn = AgentDefinition(
            agent_id="test-none",
            name="Test None",
            description="test",
        )
        agent = _DummyAgent(defn, _make_services())
        agent._load_permission_engine()
        assert agent._permission_engine is None

    def test_fallback_to_permissions_dict(self):
        """Agent with permissions dict but no profile uses from_dict."""
        defn = AgentDefinition(
            agent_id="test-dict",
            name="Test Dict",
            description="test",
            permissions={"scm": "read-write", "denied_tools": ["rm_file"]},
        )
        agent = _DummyAgent(defn, _make_services())
        agent._load_permission_engine()
        assert agent._permission_engine is not None
        assert agent._permission_engine.check_scm_write() is True
        assert agent._permission_engine.check_tool("rm_file") is False

    def test_binds_to_tool_registry(self):
        """Permission engine is bound to the global ToolRegistry."""
        from framework.tools.registry import get_registry

        defn = AgentDefinition(
            agent_id="test-bind",
            name="Test Bind",
            description="test",
            permission_profile="development",
        )
        agent = _DummyAgent(defn, _make_services())
        agent._load_permission_engine()

        registry = get_registry()
        assert registry._permission_engine is agent._permission_engine

        # Clean up global state
        registry.set_permission_engine(None)


class TestBuildRunConfig:
    """_build_run_config() centralizes RunConfig creation."""

    def test_includes_permission_engine(self):
        defn = AgentDefinition(
            agent_id="test-rc",
            name="Test RC",
            description="test",
            permission_profile="development",
        )
        agent = _DummyAgent(defn, _make_services())
        agent._load_permission_engine()

        config = agent._build_run_config("task-1", max_steps=10, timeout_seconds=60)
        assert config.permission_engine is agent._permission_engine
        assert config.session_id == "task-1"
        assert config.max_steps == 10

        # Clean up global state
        from framework.tools.registry import get_registry
        get_registry().set_permission_engine(None)

    def test_no_permission_no_error(self):
        defn = AgentDefinition(
            agent_id="test-rc2",
            name="Test RC2",
            description="test",
        )
        agent = _DummyAgent(defn, _make_services())
        config = agent._build_run_config("task-2")
        assert config.permission_engine is None


class TestWebDevPermissionProfile:
    """Verify the web-dev definition includes the development profile."""

    def test_web_dev_has_profile(self):
        from agents.web_dev.agent import web_dev_definition
        assert web_dev_definition.permission_profile == "development"

    def test_code_review_has_profile(self):
        from agents.code_review.agent import code_review_definition
        assert code_review_definition.permission_profile == "read_only"
