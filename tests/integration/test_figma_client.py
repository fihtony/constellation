"""Integration tests for FigmaClient against the Figma REST API.

All tests call the real Figma API with credentials from tests/.env.
They are automatically skipped when TEST_FIGMA_TOKEN / TEST_FIGMA_FILE_URL
are absent.

Run:
    pytest tests/integration/test_figma_client.py -v
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# TC-01: get file metadata
# ---------------------------------------------------------------------------

def test_figma_get_file(figma_client, figma_file_url):
    """FigmaClient.get_file() returns file metadata with at least one page."""
    data, status = figma_client.get_file(figma_file_url)
    assert status == "ok", f"Expected 'ok' but got {status!r}"
    assert isinstance(data, dict), "Expected a dict from get_file()"
    assert "name" in data, "File metadata missing 'name'"
    doc = data.get("document", {})
    pages = doc.get("children", [])
    assert len(pages) >= 1, f"Expected at least 1 page, got {len(pages)}"
    print(f"[figma] file: {data['name']!r}, {len(pages)} page(s)")


# ---------------------------------------------------------------------------
# TC-02: list pages
# ---------------------------------------------------------------------------

def test_figma_list_pages(figma_client, figma_file_url):
    """FigmaClient.list_pages() returns a non-empty list."""
    pages, status = figma_client.list_pages(figma_file_url)
    assert status == "ok", f"Expected 'ok' but got {status!r}"
    assert isinstance(pages, list), "Expected a list of pages"
    assert len(pages) >= 1, "Expected at least one page"
    names = [p.get("name") for p in pages]
    print(f"[figma] pages: {names[:5]}")


# ---------------------------------------------------------------------------
# TC-03: parse file key
# ---------------------------------------------------------------------------

def test_figma_parse_file_key(figma_file_url):
    """FigmaClient.parse_file_key() extracts the key from a URL."""
    from agents.ui_design.clients.figma_rest import FigmaClient
    key = FigmaClient.parse_file_key(figma_file_url)
    assert key, "Expected a non-empty file key"
    assert " " not in key, f"File key contains spaces: {key!r}"
    assert "?" not in key, f"File key contains query string: {key!r}"
    print(f"[figma] parsed file key: {key!r}")


# ---------------------------------------------------------------------------
# TC-04: UIDesignAgentAdapter (direct mode)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ui_design_adapter_fetch(figma_client, figma_file_url):
    """UIDesignAgentAdapter in direct mode handles figma.page.fetch."""
    from framework.agent import AgentServices
    from framework.checkpoint import InMemoryCheckpointer
    from framework.event_store import InMemoryEventStore
    from framework.memory import InMemoryMemoryService
    from framework.plugin import PluginManager
    from framework.session import InMemorySessionService
    from framework.skills import SkillsRegistry
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
    )
    adapter = UIDesignAgentAdapter(
        definition=ui_design_definition,
        services=services,
        figma_client=figma_client,
    )

    message = {
        "parts": [{"text": figma_file_url}],
        "metadata": {
            "requestedCapability": "figma.page.fetch",
            "figmaUrl": figma_file_url,
        },
    }
    response = await adapter.handle_message(message)
    task_data = response.get("task", {})
    assert task_data.get("status", {}).get("state") == "TASK_STATE_COMPLETED"
    artifacts = task_data.get("artifacts", [])
    assert len(artifacts) >= 1
    import json
    result = json.loads(artifacts[0]["parts"][0]["text"])
    assert result.get("status") == "ok", f"Unexpected: {result}"
    assert "pages" in result, "Expected 'pages' in result"
    print(f"[ui-design-adapter] Figma fetch OK: {result.get('name', '')!r}")
