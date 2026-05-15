"""Tests for framework.plugin — Plugin system."""
import pytest

from framework.plugin import BasePlugin, PluginManager


class TrackingPlugin(BasePlugin):
    """Plugin that records all events it receives."""

    def __init__(self):
        self.events = []

    async def before_node(self, node_name, state):
        self.events.append(("before_node", node_name))
        return None

    async def after_node(self, node_name, state):
        self.events.append(("after_node", node_name))
        return None

    async def before_tool_call(self, tool_name, args, ctx):
        self.events.append(("before_tool_call", tool_name))
        return None


class ShortCircuitPlugin(BasePlugin):
    """Plugin that short-circuits before_tool_call."""

    async def before_tool_call(self, tool_name, args, ctx):
        if tool_name == "blocked":
            return {"error": "Permission denied"}
        return None


class TestPluginManager:

    @pytest.mark.asyncio
    async def test_fire_event_calls_all_plugins(self):
        pm = PluginManager()
        p1 = TrackingPlugin()
        p2 = TrackingPlugin()
        pm.register(p1)
        pm.register(p2)

        await pm.fire("before_node", "step_a", {})

        assert ("before_node", "step_a") in p1.events
        assert ("before_node", "step_a") in p2.events

    @pytest.mark.asyncio
    async def test_short_circuit_on_non_none(self):
        pm = PluginManager()
        blocker = ShortCircuitPlugin()
        tracker = TrackingPlugin()
        pm.register(blocker)
        pm.register(tracker)

        result = await pm.fire("before_tool_call", "blocked", {}, ctx={})

        assert result == {"error": "Permission denied"}
        # tracker should NOT have been called for this event
        assert ("before_tool_call", "blocked") not in tracker.events

    @pytest.mark.asyncio
    async def test_missing_handler_skipped(self):
        pm = PluginManager()
        p = TrackingPlugin()
        pm.register(p)

        # Fire an event that TrackingPlugin doesn't implement
        result = await pm.fire("nonexistent_event", "arg1")
        assert result is None

    @pytest.mark.asyncio
    async def test_plugin_ordering(self):
        pm = PluginManager()
        order = []

        class P1(BasePlugin):
            async def before_node(self, name, state):
                order.append("p1")
                return None

        class P2(BasePlugin):
            async def before_node(self, name, state):
                order.append("p2")
                return None

        pm.register(P1())
        pm.register(P2())
        await pm.fire("before_node", "x", {})

        assert order == ["p1", "p2"]
