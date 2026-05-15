"""Tests for framework.event_store — Event sourcing."""
import pytest

from framework.event_store import InMemoryEventStore


class TestInMemoryEventStore:
    """Test InMemoryEventStore."""

    @pytest.fixture
    def store(self):
        return InMemoryEventStore()

    @pytest.mark.asyncio
    async def test_append_event(self, store):
        event_id = await store.append(
            session_id="s1",
            event_type="tool_call",
            content={"tool": "read_file", "path": "/app/main.py"},
        )
        assert event_id

    @pytest.mark.asyncio
    async def test_list_events_by_session(self, store):
        await store.append("s1", "tool_call", {"tool": "a"})
        await store.append("s2", "tool_call", {"tool": "b"})
        await store.append("s1", "llm_call", {"model": "gpt-5-mini"})

        events = await store.list_events("s1")
        assert len(events) == 2
        assert all(e.session_id == "s1" for e in events)

    @pytest.mark.asyncio
    async def test_list_events_by_type(self, store):
        await store.append("s1", "tool_call", {"tool": "a"})
        await store.append("s1", "llm_call", {"model": "x"})

        events = await store.list_events("s1", event_type="tool_call")
        assert len(events) == 1
        assert events[0].event_type == "tool_call"

    @pytest.mark.asyncio
    async def test_event_ordering(self, store):
        """Events should be returned in chronological order."""
        await store.append("s1", "step_1", {"order": 1})
        await store.append("s1", "step_2", {"order": 2})
        await store.append("s1", "step_3", {"order": 3})

        events = await store.list_events("s1")
        assert [e.content["order"] for e in events] == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_content_json_roundtrip(self, store):
        """Complex content dicts should survive serialization."""
        content = {"nested": {"key": [1, 2, 3]}, "flag": True}
        await store.append("s1", "test", content)

        events = await store.list_events("s1")
        assert events[0].content == content

    @pytest.mark.asyncio
    async def test_unicode_content(self, store):
        """Should support Chinese and other Unicode content."""
        content = {"message": "你好世界", "emoji": "🌟"}
        await store.append("s1", "test", content)

        events = await store.list_events("s1")
        assert events[0].content["message"] == "你好世界"
