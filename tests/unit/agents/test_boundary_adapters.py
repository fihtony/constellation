"""Tests for boundary agent adapters."""
import pytest
from framework.agent import AgentMode, ExecutionMode
from agents.jira.adapter import jira_definition, JiraAgentAdapter
from agents.scm.adapter import scm_definition, SCMAgentAdapter
from agents.ui_design.adapter import ui_design_definition, UIDesignAgentAdapter


class TestBoundaryAdapterDefinitions:

    def test_jira_definition(self):
        assert jira_definition.agent_id == "jira"
        assert jira_definition.mode == AgentMode.SINGLE_TURN
        assert jira_definition.execution_mode == ExecutionMode.PERSISTENT
        assert jira_definition.workflow is None

    def test_scm_definition(self):
        assert scm_definition.agent_id == "scm"
        assert scm_definition.mode == AgentMode.SINGLE_TURN
        assert scm_definition.execution_mode == ExecutionMode.PERSISTENT
        assert scm_definition.workflow is None

    def test_ui_design_definition(self):
        assert ui_design_definition.agent_id == "ui-design"
        assert ui_design_definition.mode == AgentMode.SINGLE_TURN
        assert ui_design_definition.execution_mode == ExecutionMode.PERSISTENT
        assert ui_design_definition.workflow is None
