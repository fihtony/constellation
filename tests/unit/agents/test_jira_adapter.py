"""Tests for the Jira Agent adapter — REST and MCP provider pattern."""
import json

import pytest

from framework.agent import AgentMode, AgentServices, ExecutionMode
from framework.checkpoint import InMemoryCheckpointer
from framework.event_store import InMemoryEventStore
from framework.memory import InMemoryMemoryService
from framework.plugin import PluginManager
from framework.session import InMemorySessionService
from framework.skills import SkillsRegistry
from framework.task_store import InMemoryTaskStore

from agents.jira.adapter import JiraAgentAdapter, jira_definition, _make_provider
from agents.jira.providers.base import JiraProvider


# ---------------------------------------------------------------------------
# Stub provider for unit tests (no network I/O)
# ---------------------------------------------------------------------------

class StubJiraProvider(JiraProvider):
    """In-memory provider that returns canned responses."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def get_myself(self) -> tuple[dict, str]:
        self.calls.append(("get_myself", {}))
        return {"accountId": "test-user", "displayName": "Test"}, "ok"

    def fetch_issue(self, ticket_key: str) -> tuple[dict | None, str]:
        self.calls.append(("fetch_issue", {"ticket_key": ticket_key}))
        if ticket_key == "MISSING-1":
            return None, "HTTP 404"
        return {"key": ticket_key, "fields": {"summary": "stub"}}, "ok"

    def search_issues(
        self, jql: str, max_results: int = 10, fields: list | None = None
    ) -> tuple[dict, str]:
        self.calls.append(("search_issues", {"jql": jql}))
        return {"issues": [{"key": "PROJ-1"}], "total": 1}, "ok"

    def get_transitions(self, ticket_key: str) -> tuple[list, str]:
        self.calls.append(("get_transitions", {"ticket_key": ticket_key}))
        return [{"id": "31", "name": "In Progress"}], "ok"

    def transition_issue(
        self, ticket_key: str, transition_name: str
    ) -> tuple[str | None, str]:
        self.calls.append(("transition_issue", {"ticket_key": ticket_key, "name": transition_name}))
        return "31", "transitioned_to:In Progress"

    def add_comment(
        self, ticket_key: str, text: str, adf_body: dict | None = None
    ) -> tuple[str | None, str]:
        self.calls.append(("add_comment", {"ticket_key": ticket_key, "text": text}))
        return "10001", "ok"

    def update_issue_fields(
        self, ticket_key: str, fields: dict
    ) -> tuple[dict | None, str]:
        self.calls.append(("update_issue_fields", {"ticket_key": ticket_key, "fields": fields}))
        return {"ticketKey": ticket_key}, "updated"

    def list_comments(
        self, ticket_key: str, max_results: int = 50
    ) -> tuple[dict, str]:
        self.calls.append(("list_comments", {"ticket_key": ticket_key}))
        return {"comments": [], "total": 0}, "ok"

    @property
    def backend_name(self) -> str:
        return "stub"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_services() -> AgentServices:
    return AgentServices(
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


# ---------------------------------------------------------------------------
# Definition tests
# ---------------------------------------------------------------------------

class TestJiraDefinition:

    def test_definition_fields(self):
        assert jira_definition.agent_id == "jira"
        assert jira_definition.mode == AgentMode.SINGLE_TURN
        assert jira_definition.execution_mode == ExecutionMode.PERSISTENT
        assert jira_definition.workflow is None


# ---------------------------------------------------------------------------
# Provider factory tests
# ---------------------------------------------------------------------------

class TestProviderFactory:

    def test_default_is_rest(self, monkeypatch):
        monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
        monkeypatch.setenv("JIRA_TOKEN", "tok")
        provider = _make_provider("rest")
        assert provider.backend_name == "rest"

    def test_mcp_backend(self, monkeypatch):
        monkeypatch.setenv("JIRA_BASE_URL", "https://test.atlassian.net")
        monkeypatch.setenv("JIRA_TOKEN", "tok")
        monkeypatch.setenv("JIRA_EMAIL", "test@test.com")
        provider = _make_provider("mcp")
        assert provider.backend_name == "mcp"


# ---------------------------------------------------------------------------
# Adapter dispatch tests (using StubJiraProvider)
# ---------------------------------------------------------------------------

class TestJiraAdapterDispatch:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.provider = StubJiraProvider()
        self.adapter = JiraAgentAdapter(
            jira_definition, _make_services(), jira_provider=self.provider
        )

    @pytest.mark.asyncio
    async def test_fetch_ticket(self):
        msg = {
            "parts": [{"text": "PROJ-42"}],
            "metadata": {"requestedCapability": "jira.ticket.fetch"},
        }
        result = await self.adapter.handle_message(msg)
        task = result.get("task", result)
        assert task["status"]["state"] == "TASK_STATE_COMPLETED"
        art_text = task["artifacts"][0]["parts"][0]["text"]
        data = json.loads(art_text)
        assert data["ticket"]["key"] == "PROJ-42"
        assert self.provider.calls[-1] == ("fetch_issue", {"ticket_key": "PROJ-42"})

    @pytest.mark.asyncio
    async def test_fetch_ticket_via_metadata(self):
        msg = {
            "parts": [],
            "metadata": {
                "requestedCapability": "jira.ticket.fetch",
                "ticketKey": "PROJ-99",
            },
        }
        result = await self.adapter.handle_message(msg)
        task = result.get("task", result)
        art_text = task["artifacts"][0]["parts"][0]["text"]
        data = json.loads(art_text)
        assert data["ticket"]["key"] == "PROJ-99"

    @pytest.mark.asyncio
    async def test_search(self):
        msg = {
            "parts": [{"text": "project = PROJ"}],
            "metadata": {"requestedCapability": "jira.ticket.search"},
        }
        result = await self.adapter.handle_message(msg)
        task = result.get("task", result)
        art_text = task["artifacts"][0]["parts"][0]["text"]
        data = json.loads(art_text)
        assert data["issues"]["total"] == 1

    @pytest.mark.asyncio
    async def test_add_comment(self):
        msg = {
            "parts": [{"text": "hello"}],
            "metadata": {
                "requestedCapability": "jira.ticket.comment",
                "ticketKey": "PROJ-1",
                "comment": "test comment",
            },
        }
        result = await self.adapter.handle_message(msg)
        task = result.get("task", result)
        art_text = task["artifacts"][0]["parts"][0]["text"]
        data = json.loads(art_text)
        assert data["comment"] == "10001"

    @pytest.mark.asyncio
    async def test_get_transitions(self):
        msg = {
            "parts": [{"text": "PROJ-1"}],
            "metadata": {"requestedCapability": "jira.transitions.list"},
        }
        result = await self.adapter.handle_message(msg)
        task = result.get("task", result)
        art_text = task["artifacts"][0]["parts"][0]["text"]
        data = json.loads(art_text)
        assert len(data["transitions"]) == 1
        assert data["transitions"][0]["name"] == "In Progress"

    @pytest.mark.asyncio
    async def test_transition_issue(self):
        msg = {
            "parts": [],
            "metadata": {
                "requestedCapability": "jira.ticket.transition",
                "ticketKey": "PROJ-1",
                "transitionName": "In Progress",
            },
        }
        result = await self.adapter.handle_message(msg)
        task = result.get("task", result)
        art_text = task["artifacts"][0]["parts"][0]["text"]
        data = json.loads(art_text)
        assert data["transitionId"] == "31"

    @pytest.mark.asyncio
    async def test_update_fields(self):
        msg = {
            "parts": [],
            "metadata": {
                "requestedCapability": "jira.ticket.update",
                "ticketKey": "PROJ-1",
                "fields": {"summary": "new title"},
            },
        }
        result = await self.adapter.handle_message(msg)
        task = result.get("task", result)
        art_text = task["artifacts"][0]["parts"][0]["text"]
        data = json.loads(art_text)
        assert data["status"] == "updated"

    @pytest.mark.asyncio
    async def test_unknown_capability(self):
        msg = {
            "parts": [{"text": "x"}],
            "metadata": {"requestedCapability": "jira.unknown.cap"},
        }
        result = await self.adapter.handle_message(msg)
        task = result.get("task", result)
        art_text = task["artifacts"][0]["parts"][0]["text"]
        data = json.loads(art_text)
        assert "error" in data
        assert "Unknown" in data["error"]

    @pytest.mark.asyncio
    async def test_get_task(self):
        msg = {
            "parts": [{"text": "PROJ-1"}],
            "metadata": {"requestedCapability": "jira.ticket.fetch"},
        }
        result = await self.adapter.handle_message(msg)
        task_id = result.get("task", result)["id"]
        task = await self.adapter.get_task(task_id)
        assert task["task"]["id"] == task_id


# ---------------------------------------------------------------------------
# Provider base class tests
# ---------------------------------------------------------------------------

class TestJiraProviderBase:

    def test_abstract_methods(self):
        """JiraProvider cannot be instantiated directly."""
        with pytest.raises(TypeError):
            JiraProvider()  # type: ignore[abstract]

    def test_backend_name_default(self):
        """StubJiraProvider returns a custom backend name."""
        stub = StubJiraProvider()
        assert stub.backend_name == "stub"
