"""Tests for framework.checkpoint — Checkpoint persistence."""
import pytest

from framework.checkpoint import InMemoryCheckpointer


class TestInMemoryCheckpointer:

    @pytest.fixture
    def ckpt(self):
        return InMemoryCheckpointer()

    @pytest.mark.asyncio
    async def test_save_and_load(self, ckpt):
        data = {"state": {"a": 1}, "next_node": "step_b"}
        await ckpt.save("s1", "t1", data)

        loaded = await ckpt.load("s1", "t1")
        assert loaded == data

    @pytest.mark.asyncio
    async def test_overwrite_existing(self, ckpt):
        await ckpt.save("s1", "t1", {"version": 1})
        await ckpt.save("s1", "t1", {"version": 2})

        loaded = await ckpt.load("s1", "t1")
        assert loaded["version"] == 2

    @pytest.mark.asyncio
    async def test_load_nonexistent_returns_none(self, ckpt):
        result = await ckpt.load("nope", "nope")
        assert result is None

    @pytest.mark.asyncio
    async def test_delete_checkpoint(self, ckpt):
        await ckpt.save("s1", "t1", {"data": True})
        await ckpt.delete("s1", "t1")
        assert await ckpt.load("s1", "t1") is None

    @pytest.mark.asyncio
    async def test_json_roundtrip(self, ckpt):
        """Complex dicts should survive save/load."""
        data = {
            "state": {"nested": {"list": [1, 2, 3]}, "flag": False},
            "next_node": "review",
        }
        await ckpt.save("s1", "t1", data)
        loaded = await ckpt.load("s1", "t1")
        assert loaded == data
