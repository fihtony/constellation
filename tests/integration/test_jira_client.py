"""Integration tests for JiraClient against Jira Cloud REST API.

All tests call the real Jira Cloud instance configured in tests/.env.
They are automatically skipped when TEST_JIRA_TOKEN / TEST_JIRA_EMAIL /
TEST_JIRA_TICKET_URL are absent.

Run:
    pytest tests/integration/test_jira_client.py -v
"""
from __future__ import annotations

import pytest


pytestmark = pytest.mark.live  # tag so they can be filtered with -m


# ---------------------------------------------------------------------------
# TC-01: authenticated user
# ---------------------------------------------------------------------------

def test_jira_get_myself(jira_client):
    """JiraClient.get_myself() returns the authenticated user dict."""
    user, status = jira_client.get_myself()
    assert status == "ok", f"Expected 'ok' but got {status!r}"
    assert isinstance(user, dict), "Expected a dict from get_myself()"
    assert "accountId" in user or "displayName" in user, (
        f"Unexpected user dict: {user}"
    )
    print(f"[jira] authenticated as: {user.get('displayName', user.get('accountId'))}")


# ---------------------------------------------------------------------------
# TC-02: ticket fetch
# ---------------------------------------------------------------------------

def test_jira_fetch_ticket(jira_client, jira_ticket_key):
    """JiraClient.fetch_ticket() returns the expected issue."""
    ticket, status = jira_client.fetch_ticket(jira_ticket_key)
    assert status == "ok", f"Expected 'ok' but got {status!r}"
    assert ticket is not None, "Expected a ticket dict, got None"
    assert ticket.get("key") == jira_ticket_key, (
        f"Ticket key mismatch: {ticket.get('key')!r} != {jira_ticket_key!r}"
    )
    fields = ticket.get("fields", {})
    assert "summary" in fields, "Ticket missing 'summary' field"
    print(f"[jira] {jira_ticket_key}: {fields.get('summary', '')[:80]}")


# ---------------------------------------------------------------------------
# TC-03: JQL search
# ---------------------------------------------------------------------------

def test_jira_search(jira_client, jira_ticket_key):
    """JiraClient.search() with JQL returns at least the target ticket."""
    results, status = jira_client.search(f"key = {jira_ticket_key}", max_results=5)
    assert status == "ok", f"Expected 'ok' but got {status!r}"
    issues = results.get("issues", [])
    assert len(issues) >= 1, f"JQL search returned no issues for key={jira_ticket_key}"
    keys = [i.get("key") for i in issues]
    assert jira_ticket_key in keys, (
        f"Target ticket {jira_ticket_key} not in search results: {keys}"
    )
    print(f"[jira] search returned {len(issues)} issue(s)")


# ---------------------------------------------------------------------------
# TC-04: ticket transitions
# ---------------------------------------------------------------------------

def test_jira_get_transitions(jira_client, jira_ticket_key):
    """JiraClient.get_transitions() returns a non-empty list."""
    transitions, status = jira_client.get_transitions(jira_ticket_key)
    assert status == "ok", f"Expected 'ok' but got {status!r}"
    assert isinstance(transitions, list), "Expected a list of transitions"
    # Cloud Jira should have at least one transition
    print(f"[jira] {len(transitions)} transition(s) available for {jira_ticket_key}")


# ---------------------------------------------------------------------------
# TC-05: JiraAgentAdapter (direct mode)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_jira_adapter_fetch(jira_client, jira_ticket_key):
    """JiraAgentAdapter in direct mode correctly handles jira.ticket.fetch."""
    from framework.agent import AgentDefinition, AgentMode, AgentServices, ExecutionMode
    from framework.checkpoint import InMemoryCheckpointer
    from framework.event_store import InMemoryEventStore
    from framework.memory import InMemoryMemoryService
    from framework.plugin import PluginManager
    from framework.session import InMemorySessionService
    from framework.skills import SkillsRegistry
    from agents.jira.adapter import JiraAgentAdapter, jira_definition

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
    adapter = JiraAgentAdapter(
        definition=jira_definition,
        services=services,
        dispatch_mode="direct",
        jira_client=jira_client,
    )

    message = {
        "parts": [{"text": jira_ticket_key}],
        "metadata": {
            "requestedCapability": "jira.ticket.fetch",
            "ticketKey": jira_ticket_key,
        },
    }
    response = await adapter.handle_message(message)
    task_data = response.get("task", {})
    assert task_data.get("status", {}).get("state") == "TASK_STATE_COMPLETED"
    artifacts = task_data.get("artifacts", [])
    assert len(artifacts) >= 1
    import json
    result = json.loads(artifacts[0]["parts"][0]["text"])
    assert result.get("status") == "ok"
    ticket = result.get("ticket", {})
    assert ticket.get("key") == jira_ticket_key
    print(f"[jira-adapter] fetched {jira_ticket_key} via adapter OK")
