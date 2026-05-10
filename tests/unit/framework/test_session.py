"""Tests for framework.session — Session management."""
import pytest

from framework.session import InMemorySessionService, Session


class TestInMemorySessionService:
    """Test InMemorySessionService."""

    @pytest.fixture
    def svc(self):
        return InMemorySessionService()

    @pytest.mark.asyncio
    async def test_create_session(self, svc):
        """Create a session and get it back."""
        session = await svc.create("agent-1", "user-1")
        assert isinstance(session, Session)
        assert session.agent_id == "agent-1"
        assert session.user_id == "user-1"
        assert session.id

    @pytest.mark.asyncio
    async def test_get_session(self, svc):
        """Retrieve a created session by ID."""
        created = await svc.create("agent-1", "user-1")
        fetched = await svc.get(created.id)
        assert fetched is not None
        assert fetched.id == created.id

    @pytest.mark.asyncio
    async def test_get_nonexistent_returns_none(self, svc):
        result = await svc.get("does-not-exist")
        assert result is None

    @pytest.mark.asyncio
    async def test_update_state_merges(self, svc):
        """update_state should merge, not replace."""
        session = await svc.create("agent-1", "user-1")
        await svc.update_state(session.id, {"key1": "val1"})
        await svc.update_state(session.id, {"key2": "val2"})

        updated = await svc.get(session.id)
        assert updated.state == {"key1": "val1", "key2": "val2"}

    @pytest.mark.asyncio
    async def test_list_sessions_by_agent(self, svc):
        await svc.create("agent-1", "user-1")
        await svc.create("agent-2", "user-1")
        await svc.create("agent-1", "user-2")

        results = await svc.list_sessions(agent_id="agent-1")
        assert len(results) == 2
        assert all(s.agent_id == "agent-1" for s in results)

    @pytest.mark.asyncio
    async def test_list_sessions_by_user(self, svc):
        await svc.create("agent-1", "user-1")
        await svc.create("agent-1", "user-2")

        results = await svc.list_sessions(user_id="user-1")
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_delete_session(self, svc):
        session = await svc.create("agent-1", "user-1")
        await svc.delete(session.id)
        assert await svc.get(session.id) is None

    @pytest.mark.asyncio
    async def test_iso8601_timestamps(self, svc):
        """created_at and updated_at should use ISO 8601."""
        session = await svc.create("agent-1", "user-1")
        assert "T" in session.created_at
        assert "+" in session.created_at or "Z" in session.created_at
