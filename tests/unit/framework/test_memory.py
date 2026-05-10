"""Tests for framework.memory — Memory service."""
import pytest

from framework.memory import InMemoryMemoryService


class TestInMemoryMemoryService:

    @pytest.fixture
    def svc(self):
        return InMemoryMemoryService()

    @pytest.mark.asyncio
    async def test_add_memory(self, svc):
        mid = await svc.add("React uses JSX", scope="agent", scope_id="web-dev")
        assert mid

    @pytest.mark.asyncio
    async def test_search_keyword(self, svc):
        await svc.add("React uses JSX")
        await svc.add("Python uses indentation")

        results = await svc.search("React")
        assert len(results) == 1
        assert "React" in results[0].content

    @pytest.mark.asyncio
    async def test_search_scope_filter(self, svc):
        await svc.add("Global fact", scope="global")
        await svc.add("Agent fact", scope="agent", scope_id="web-dev")

        results = await svc.search("fact", scope="agent")
        assert len(results) == 1
        assert results[0].scope == "agent"

    @pytest.mark.asyncio
    async def test_delete_memory(self, svc):
        mid = await svc.add("temp")
        await svc.delete(mid)

        results = await svc.search("temp")
        assert len(results) == 0
