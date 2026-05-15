#!/usr/bin/env python3
"""Run a quick smoke test of the v2 agent workflows locally.

Tests Compass (ReAct-first) and Team Lead (graph-first) without LLM
by using runtime=None mode.

Usage:
    python scripts/run_test_workflow.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _make_services(runtime=None):
    from framework.agent import AgentServices
    from framework.checkpoint import InMemoryCheckpointer
    from framework.event_store import InMemoryEventStore
    from framework.memory import InMemoryMemoryService
    from framework.plugin import PluginManager
    from framework.session import InMemorySessionService
    from framework.skills import SkillsRegistry
    from framework.task_store import InMemoryTaskStore

    return AgentServices(
        session_service=InMemorySessionService(),
        event_store=InMemoryEventStore(),
        memory_service=InMemoryMemoryService(),
        skills_registry=SkillsRegistry(),
        plugin_manager=PluginManager(),
        checkpoint_service=InMemoryCheckpointer(),
        runtime=runtime,
        registry_client=None,
        task_store=InMemoryTaskStore(),
    )


async def test_team_lead_workflow():
    """Test the Team Lead graph workflow (no LLM, nodes return defaults)."""
    from unittest.mock import MagicMock
    from agents.team_lead.agent import TeamLeadAgent, team_lead_definition
    from agents.team_lead.tools import register_team_lead_tools
    from framework.tools.base import BaseTool, ToolResult
    from framework.tools.registry import get_registry

    print("[Test 1] Team Lead graph workflow (no LLM)")

    # Register mock boundary tools
    register_team_lead_tools()
    registry = get_registry()

    mock_web_dev = MagicMock(spec=BaseTool)
    mock_web_dev.name = "dispatch_web_dev"
    mock_web_dev.execute_sync = MagicMock(return_value=ToolResult(
        output=json.dumps({
            "status": "completed",
            "summary": "Changes implemented",
            "prUrl": "https://github.com/org/repo/pull/1",
            "branch": "feature/test",
        })
    ))
    mock_web_dev.to_openai_schema = MagicMock(return_value={
        "type": "function",
        "function": {"name": "dispatch_web_dev", "parameters": {"type": "object", "properties": {}}},
    })

    mock_review = MagicMock(spec=BaseTool)
    mock_review.name = "dispatch_code_review"
    mock_review.execute_sync = MagicMock(return_value=ToolResult(
        output=json.dumps({"verdict": "approved", "comments": [], "summary": "LGTM"})
    ))
    mock_review.to_openai_schema = MagicMock(return_value={
        "type": "function",
        "function": {"name": "dispatch_code_review", "parameters": {"type": "object", "properties": {}}},
    })

    registry.register(mock_web_dev)
    registry.register(mock_review)

    services = _make_services()
    agent = TeamLeadAgent(team_lead_definition, services)
    await agent.start()

    message = {
        "parts": [{"text": "Implement the login page"}],
        "metadata": {"jiraKey": "TEST-1"},
    }
    response = await agent.handle_message(message)
    task_id = response.get("task", response).get("id", "")

    # Poll until done
    import time
    deadline = time.time() + 10
    while time.time() < deadline:
        task_dict = await agent.get_task(task_id)
        state = task_dict.get("task", task_dict).get("status", {}).get("state", "")
        if state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED"):
            break
        await asyncio.sleep(0.2)

    final = await agent.get_task(task_id)
    final_state = final.get("task", final).get("status", {}).get("state", "")
    print(f"  Final state: {final_state}")
    assert final_state == "TASK_STATE_COMPLETED", f"Expected COMPLETED, got {final_state}"
    print("  ✓ Team Lead workflow passed\n")


async def main():
    print("=== Constellation v2 Workflow Smoke Test ===\n")

    await test_team_lead_workflow()

    print("=== All smoke tests passed ===")


if __name__ == "__main__":
    asyncio.run(main())
