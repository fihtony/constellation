"""Integration tests for Google Stitch MCP client and UIDesignAgentAdapter.

All tests call the real Stitch MCP server at https://stitch.googleapis.com/mcp
using credentials from tests/.env.
They are automatically skipped when TEST_STITCH_API_KEY / TEST_STITCH_PROJECT_URL
are absent.

Run:
    pytest tests/integration/test_stitch_client.py -v
"""
from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# TC-01: list available MCP tools
# ---------------------------------------------------------------------------

def test_stitch_list_tools(stitch_client):
    """StitchMcpClient.list_tools() discovers available Stitch tools."""
    tools, status = stitch_client.list_tools()
    assert status == "ok", f"Expected 'ok' but got {status!r}"
    assert isinstance(tools, list), "Expected a list of tools"
    assert len(tools) >= 1, "Expected at least one tool from Stitch MCP"
    names = [t.get("name", "") for t in tools]
    print(f"[stitch-mcp] {len(tools)} tool(s): {names[:5]}")


# ---------------------------------------------------------------------------
# TC-02: fetch project metadata
# ---------------------------------------------------------------------------

def test_stitch_get_project(stitch_client, stitch_project_id):
    """StitchMcpClient.get_project() returns project metadata."""
    data, status = stitch_client.get_project(stitch_project_id)
    assert status == "ok", f"Expected 'ok' but got {status!r}"
    assert data is not None, "Expected project data, got None"
    # Project should have some identifiable field
    assert stitch_project_id in json.dumps(data), (
        f"Project ID {stitch_project_id!r} not found in response: {data}"
    )
    print(f"[stitch-mcp] project {stitch_project_id}: {str(data)[:80]}")


# ---------------------------------------------------------------------------
# TC-03: list screens
# ---------------------------------------------------------------------------

def test_stitch_list_screens(stitch_client, stitch_project_id):
    """StitchMcpClient.list_screens() returns a list of screens."""
    screens, status = stitch_client.list_screens(stitch_project_id)
    assert status == "ok", f"Expected 'ok' but got {status!r}"
    assert isinstance(screens, list), "Expected a list of screens"
    assert len(screens) >= 1, f"Expected at least one screen for project {stitch_project_id}"
    names = [s.get("name", s.get("id", "")) for s in screens]
    print(f"[stitch-mcp] {len(screens)} screen(s): {names[:5]}")


# ---------------------------------------------------------------------------
# TC-04: fetch single screen by ID
# ---------------------------------------------------------------------------

def test_stitch_get_screen(stitch_client, stitch_project_id, stitch_screen_id):
    """StitchMcpClient.get_screen() returns screen data."""
    if not stitch_screen_id:
        # Fall back to first screen from list
        screens, status = stitch_client.list_screens(stitch_project_id)
        if not screens:
            pytest.skip("No screens found in project and TEST_STITCH_SCREEN_ID not set")
        stitch_screen_id = screens[0].get("id") or screens[0].get("name", "")

    data, status = stitch_client.get_screen(stitch_project_id, stitch_screen_id)
    assert status == "ok", f"Expected 'ok' but got {status!r}"
    assert data is not None, "Expected screen data, got None"
    print(f"[stitch-mcp] screen {stitch_screen_id}: {str(data)[:80]}")


# ---------------------------------------------------------------------------
# TC-05: UIDesignAgentAdapter — stitch.screens.list
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ui_design_adapter_stitch_list_screens(stitch_client, stitch_project_id):
    """UIDesignAgentAdapter handles stitch.screens.list via Stitch MCP."""
    from framework.agent import AgentServices
    from framework.checkpoint import InMemoryCheckpointer
    from framework.event_store import InMemoryEventStore
    from framework.memory import InMemoryMemoryService
    from framework.plugin import PluginManager
    from framework.session import InMemorySessionService
    from framework.skills import SkillsRegistry
    from framework.task_store import InMemoryTaskStore
    from agents.ui_design.adapter import UIDesignAgentAdapter, ui_design_definition

    services = AgentServices(
        session_service=InMemorySessionService(),
        event_store=InMemoryEventStore(),
        memory_service=InMemoryMemoryService(),
        skills_registry=SkillsRegistry(),
        plugin_manager=PluginManager(),
        checkpoint_service=InMemoryCheckpointer(),
        runtime=None,
        registry_client=None,
        task_store=InMemoryTaskStore(),
    )
    adapter = UIDesignAgentAdapter(
        definition=ui_design_definition,
        services=services,
        stitch_client=stitch_client,
    )

    message = {
        "parts": [{"text": stitch_project_id}],
        "metadata": {
            "requestedCapability": "stitch.screens.list",
            "stitchProjectId": stitch_project_id,
        },
    }
    response = await adapter.handle_message(message)
    task_data = response.get("task", {})
    assert task_data.get("status", {}).get("state") == "TASK_STATE_COMPLETED"
    artifacts = task_data.get("artifacts", [])
    assert len(artifacts) >= 1
    result = json.loads(artifacts[0]["parts"][0]["text"])
    assert result.get("status") == "ok", f"Unexpected: {result}"
    screens = result.get("screens", [])
    assert isinstance(screens, list), "Expected 'screens' list in result"
    assert len(screens) >= 1, "Expected at least one screen"
    print(f"[stitch-mcp-adapter] screens list OK: {len(screens)} screen(s)")


# ---------------------------------------------------------------------------
# TC-06: UIDesignAgentAdapter — stitch.screen.fetch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ui_design_adapter_stitch_fetch_screen(
    stitch_client, stitch_project_id, stitch_screen_id
):
    """UIDesignAgentAdapter handles stitch.screen.fetch via Stitch MCP."""
    if not stitch_screen_id:
        screens, _ = stitch_client.list_screens(stitch_project_id)
        if not screens:
            pytest.skip("No screens and TEST_STITCH_SCREEN_ID not set")
        stitch_screen_id = screens[0].get("id") or screens[0].get("name", "")

    from framework.agent import AgentServices
    from framework.checkpoint import InMemoryCheckpointer
    from framework.event_store import InMemoryEventStore
    from framework.memory import InMemoryMemoryService
    from framework.plugin import PluginManager
    from framework.session import InMemorySessionService
    from framework.skills import SkillsRegistry
    from framework.task_store import InMemoryTaskStore
    from agents.ui_design.adapter import UIDesignAgentAdapter, ui_design_definition

    services = AgentServices(
        session_service=InMemorySessionService(),
        event_store=InMemoryEventStore(),
        memory_service=InMemoryMemoryService(),
        skills_registry=SkillsRegistry(),
        plugin_manager=PluginManager(),
        checkpoint_service=InMemoryCheckpointer(),
        runtime=None,
        registry_client=None,
        task_store=InMemoryTaskStore(),
    )
    adapter = UIDesignAgentAdapter(
        definition=ui_design_definition,
        services=services,
        stitch_client=stitch_client,
    )

    message = {
        "parts": [{"text": stitch_project_id}],
        "metadata": {
            "requestedCapability": "stitch.screen.fetch",
            "stitchProjectId": stitch_project_id,
            "stitchScreenId": stitch_screen_id,
        },
    }
    response = await adapter.handle_message(message)
    task_data = response.get("task", {})
    assert task_data.get("status", {}).get("state") == "TASK_STATE_COMPLETED"
    artifacts = task_data.get("artifacts", [])
    assert len(artifacts) >= 1
    result = json.loads(artifacts[0]["parts"][0]["text"])
    assert result.get("status") == "ok", f"Unexpected: {result}"
    assert "screen" in result, "Expected 'screen' in result"
    print(f"[stitch-mcp-adapter] screen fetch OK: {stitch_screen_id}")
