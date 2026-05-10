"""Cross-cutting concerns via before/after callbacks (inspired by ADK Plugins).

A Plugin can intercept agent, tool, LLM, and workflow-node lifecycle events.
Returning a non-None value from a ``before_*`` handler short-circuits subsequent
plugins and the real call.
"""
from __future__ import annotations

import logging
from abc import ABC
from typing import Any

logger = logging.getLogger(__name__)


class BasePlugin(ABC):
    """Override any callback you need.

    Return ``None`` to let execution continue, or a non-None value to
    short-circuit.
    """

    async def before_agent_run(self, agent_id: str, state: dict, ctx: dict) -> dict | None:
        return None

    async def after_agent_run(self, agent_id: str, result: dict, ctx: dict) -> dict | None:
        return None

    async def before_tool_call(self, tool_name: str, args: dict, ctx: dict) -> dict | None:
        return None

    async def after_tool_call(self, tool_name: str, result: Any, ctx: dict) -> Any | None:
        return None

    async def before_llm_call(self, prompt: str, ctx: dict) -> str | None:
        return None

    async def after_llm_response(self, response: str, ctx: dict) -> str | None:
        return None

    async def before_node(self, node_name: str, state: dict) -> dict | None:
        return None

    async def after_node(self, node_name: str, state: dict) -> dict | None:
        return None


class PluginManager:
    """Manages plugin registration and callback dispatch."""

    def __init__(self) -> None:
        self._plugins: list[BasePlugin] = []

    def register(self, plugin: BasePlugin) -> None:
        """Add a plugin to the chain."""
        self._plugins.append(plugin)

    async def fire(self, event: str, *args: Any, **kwargs: Any) -> Any:
        """Fire a plugin event.

        Calls each plugin's handler in registration order.  The first non-None
        return short-circuits and becomes the return value.
        """
        for plugin in self._plugins:
            handler = getattr(plugin, event, None)
            if handler is None:
                continue
            try:
                result = await handler(*args, **kwargs)
                if result is not None:
                    return result
            except Exception:
                logger.warning(
                    "Plugin %s.%s raised an exception",
                    type(plugin).__name__, event,
                    exc_info=True,
                )
        return None
