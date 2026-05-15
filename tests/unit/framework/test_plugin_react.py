"""Tests for PluginManager.fire_sync() and plugin hooks in the ReAct agentic loop.

Gap 7c: Plugin coverage for the ReAct LLM/tool call path inside
ConnectAgentAdapter.run_agentic().
"""
from __future__ import annotations

import json

import pytest

from framework.plugin import BasePlugin, PluginManager


# ---------------------------------------------------------------------------
# TC-01: fire_sync fires registered plugins synchronously
# ---------------------------------------------------------------------------

def test_fire_sync_calls_registered_plugin():
    """fire_sync() executes plugin handlers and returns the first non-None result."""
    calls: list[str] = []

    class RecorderPlugin(BasePlugin):
        async def before_llm_call(self, prompt: str, ctx: dict):
            calls.append(f"before_llm:{prompt[:10]}")
            return None  # let subsequent plugins run

    pm = PluginManager()
    pm.register(RecorderPlugin())

    result = pm.fire_sync("before_llm_call", "hello world", ctx={})
    assert calls == ["before_llm:hello worl"]
    assert result is None  # plugin returned None


def test_fire_sync_short_circuits_on_first_non_none():
    """fire_sync() stops as soon as a plugin returns a non-None value."""
    calls: list[str] = []

    class StopPlugin(BasePlugin):
        async def before_llm_call(self, prompt: str, ctx: dict):
            calls.append("stop")
            return "BLOCKED"

    class SecondPlugin(BasePlugin):
        async def before_llm_call(self, prompt: str, ctx: dict):
            calls.append("second")
            return None

    pm = PluginManager()
    pm.register(StopPlugin())
    pm.register(SecondPlugin())

    result = pm.fire_sync("before_llm_call", "test", ctx={})
    assert result == "BLOCKED"
    assert calls == ["stop"]  # SecondPlugin was not called


def test_fire_sync_handles_plugin_exception_gracefully():
    """fire_sync() does not propagate exceptions from faulty plugins."""

    class BrokenPlugin(BasePlugin):
        async def before_tool_call(self, tool_name: str, args: dict, ctx: dict):
            raise RuntimeError("Plugin crashed!")

    pm = PluginManager()
    pm.register(BrokenPlugin())

    # Should not raise
    result = pm.fire_sync("before_tool_call", "my_tool", {}, ctx={})
    assert result is None


def test_fire_sync_with_no_plugins_returns_none():
    """fire_sync() on an empty PluginManager returns None without error."""
    pm = PluginManager()
    result = pm.fire_sync("before_llm_call", "prompt", ctx={})
    assert result is None


def test_fire_sync_safe_from_background_thread():
    """fire_sync() works correctly when called from a background thread."""
    import threading

    results: list[str] = []

    class CaptureTool(BasePlugin):
        async def after_tool_call(self, tool_name: str, result, ctx: dict):
            results.append(f"tool:{tool_name}")
            return None

    pm = PluginManager()
    pm.register(CaptureTool())

    def _thread_body():
        pm.fire_sync("after_tool_call", "my_tool", "result_value", ctx={})

    t = threading.Thread(target=_thread_body)
    t.start()
    t.join(timeout=5)

    assert results == ["tool:my_tool"]


# ---------------------------------------------------------------------------
# TC-02: ConnectAgentAdapter.run_agentic fires plugin hooks
# ---------------------------------------------------------------------------

def test_connect_adapter_fires_plugin_hooks_on_llm_and_tool():
    """run_agentic() fires before/after LLM and before/after tool plugins."""
    import framework.runtime.connect_agent.adapter as _adapter_mod
    import framework.runtime.connect_agent.transport as _transport
    from framework.runtime.connect_agent.adapter import ConnectAgentAdapter
    from framework.tools.base import BaseTool, ToolResult
    from framework.tools.registry import ToolRegistry
    import framework.tools.registry as _reg

    # ---- Tool ----------------------------------------------------------------
    class EchoTool(BaseTool):
        name = "echo_tool"
        description = "Echo input."
        parameters_schema = {
            "type": "object",
            "properties": {"msg": {"type": "string"}},
            "required": ["msg"],
        }

        def execute_sync(self, msg: str = "") -> ToolResult:
            return ToolResult(output=json.dumps({"echo": msg}))

    test_registry = ToolRegistry()
    test_registry.register(EchoTool())
    original_registry = _reg._default_registry
    _reg._default_registry = test_registry

    # ---- Mock LLM ------------------------------------------------------------
    call_count = 0

    def _mock_llm(messages, *, model, timeout=120, max_tokens=4096, tools=None, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return {
                "choices": [{
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "tc-x",
                            "type": "function",
                            "function": {
                                "name": "echo_tool",
                                "arguments": '{"msg": "hello"}',
                            },
                        }],
                    },
                }]
            }
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "Done: HELLO"},
            }]
        }

    original_call = _transport.call_chat_completion
    _transport.call_chat_completion = _mock_llm
    _adapter_mod.call_chat_completion = _mock_llm

    # ---- Plugin that records events ------------------------------------------
    plugin_events: list[str] = []

    class RecorderPlugin(BasePlugin):
        async def before_llm_call(self, prompt: str, ctx: dict):
            plugin_events.append(f"before_llm:{prompt[:5]!r}")
            return None

        async def after_llm_response(self, response: str, ctx: dict):
            plugin_events.append(f"after_llm:{response[:5]!r}")
            return None

        async def before_tool_call(self, tool_name: str, args: dict, ctx: dict):
            plugin_events.append(f"before_tool:{tool_name}")
            return None

        async def after_tool_call(self, tool_name: str, result, ctx: dict):
            plugin_events.append(f"after_tool:{tool_name}")
            return None

    pm = PluginManager()
    pm.register(RecorderPlugin())

    try:
        adapter = ConnectAgentAdapter()
        result = adapter.run_agentic(
            task="Echo hello.",
            tools=["echo_tool"],
            max_turns=5,
            timeout=30,
            plugin_manager=pm,
        )
    finally:
        _transport.call_chat_completion = original_call
        _adapter_mod.call_chat_completion = original_call
        _reg._default_registry = original_registry

    assert result.success
    # LLM was called twice (once with tool_calls, once with stop)
    assert any("before_llm" in e for e in plugin_events), f"Missing before_llm in {plugin_events}"
    assert any("after_llm" in e for e in plugin_events), f"Missing after_llm in {plugin_events}"
    # Tool events for the echo_tool call
    assert "before_tool:echo_tool" in plugin_events, f"Missing before_tool in {plugin_events}"
    assert "after_tool:echo_tool" in plugin_events, f"Missing after_tool in {plugin_events}"


def test_connect_adapter_run_agentic_without_plugin_manager():
    """run_agentic() works correctly when no plugin_manager is supplied."""
    import framework.runtime.connect_agent.adapter as _adapter_mod
    import framework.runtime.connect_agent.transport as _transport
    from framework.runtime.connect_agent.adapter import ConnectAgentAdapter

    def _mock_llm(messages, *, model, timeout=120, max_tokens=4096, tools=None, **kwargs):
        return {
            "choices": [{
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "No plugin needed."},
            }]
        }

    original_call = _transport.call_chat_completion
    _transport.call_chat_completion = _mock_llm
    _adapter_mod.call_chat_completion = _mock_llm
    try:
        adapter = ConnectAgentAdapter()
        result = adapter.run_agentic(task="Simple task.", max_turns=2, timeout=10)
    finally:
        _transport.call_chat_completion = original_call
        _adapter_mod.call_chat_completion = original_call

    assert result.success
    assert "No plugin needed." in result.summary
