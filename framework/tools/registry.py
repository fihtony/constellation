"""Tool registry for the Constellation agent framework.

Every agent populates a ToolRegistry with the BaseTool instances it needs.
The registry provides:
  - Tool discovery (list_schemas) → OpenAI function-calling schemas
  - Tool execution (execute / execute_sync) → ToolResult → JSON string

Usage
-----
from framework.tools.registry import get_registry
registry = get_registry()
registry.register(MyTool())

# In run_agentic, the adapter calls:
schemas = registry.list_schemas(tool_names)
result_str = registry.execute_sync(tool_name, arguments_json)
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from framework.permissions import PermissionEngine
    from framework.tools.base import BaseTool, ToolResult

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Thread-safe registry mapping tool names to BaseTool instances.

    The permission engine is stored in **thread-local** storage so that
    multi-agent in-process setups (tests, connect-agent runtime) can run
    multiple agents concurrently without one agent's permission policy
    blocking another agent's tool calls.

    CompiledWorkflow.run() installs/clears the engine around each workflow
    execution on the calling thread.  Other threads are unaffected.
    """

    def __init__(self) -> None:
        self._tools: dict[str, "BaseTool"] = {}
        self._tl = threading.local()   # thread-local permission engine
        self._plugin_manager: "Any | None" = None

    # ------------------------------------------------------------------
    # Permission gate — thread-local accessors
    # ------------------------------------------------------------------

    @property
    def _permission_engine(self) -> "PermissionEngine | None":
        """Return the permission engine for the *current thread* only."""
        return getattr(self._tl, "engine", None)

    @_permission_engine.setter
    def _permission_engine(self, engine: "PermissionEngine | None") -> None:
        self._tl.engine = engine

    def set_permission_engine(self, engine: "PermissionEngine | None") -> "ToolRegistry":
        """Set the permission engine for the *current thread* only.

        This is called by CompiledWorkflow.run() immediately before and after
        each workflow execution, scoping the permission check to the agent's
        own execution thread.  Concurrent agents running in other threads are
        not affected.
        """
        self._tl.engine = engine
        return self

    def set_plugin_manager(self, pm: "Any") -> "ToolRegistry":
        """Attach a PluginManager for tool lifecycle callbacks."""
        self._plugin_manager = pm
        return self

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, tool: "BaseTool") -> "ToolRegistry":
        """Register a tool.  Overwrites an existing tool with the same name."""
        self._tools[tool.name] = tool
        return self

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> "BaseTool | None":
        return self._tools.get(name)

    def names(self) -> list[str]:
        return list(self._tools.keys())

    # ------------------------------------------------------------------
    # Schema generation (for LLM tool-calling)
    # ------------------------------------------------------------------

    def list_schemas(
        self,
        tool_names: list[str] | None = None,
    ) -> list[dict]:
        """Return OpenAI function-calling schemas.

        If *tool_names* is provided, only include those tools.
        """
        tools = (
            [self._tools[n] for n in tool_names if n in self._tools]
            if tool_names is not None
            else list(self._tools.values())
        )
        return [t.to_openai_schema() for t in tools]

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def execute_sync(self, name: str, arguments: str | dict) -> str:
        """Execute a tool synchronously.  Returns the result as a JSON string.

        Permission check is applied first when a PermissionEngine is set.
        Plugin before_tool_call / after_tool_call hooks are fired when a
        PluginManager is attached.
        If the tool's execute_sync raises, the error is caught and returned
        as ``{"error": "..."}`` so the LLM can react to failures gracefully.
        """
        # Permission gate (fail-closed)
        if self._permission_engine:
            from framework.errors import PermissionDeniedError
            try:
                self._permission_engine.require_tool(name)
            except PermissionDeniedError as exc:
                logger.warning("[registry] Permission denied for tool '%s': %s", name, exc)
                return json.dumps({"error": str(exc)})

        tool = self._tools.get(name)
        if not tool:
            return json.dumps({"error": f"Tool '{name}' is not registered"})

        kwargs = self._parse_args(arguments)

        # Plugin: before_tool_call (may short-circuit)
        if self._plugin_manager:
            override = self._plugin_manager.fire_sync(
                "before_tool_call", name, kwargs, {}
            )
            if override is not None:
                return json.dumps(override) if isinstance(override, dict) else str(override)

        try:
            result = tool.execute_sync(**kwargs)
        except NotImplementedError:
            # Fall back to the async path if execute_sync is not implemented.
            result = self._run_async(tool, kwargs)
        except Exception as exc:
            logger.warning("[registry] Tool '%s' raised %s: %s", name, type(exc).__name__, exc)
            return json.dumps({"error": str(exc)})

        output = result.output if not result.error else json.dumps({"error": result.error})

        # Plugin: after_tool_call
        if self._plugin_manager:
            self._plugin_manager.fire_sync("after_tool_call", name, output, {})

        if result.error:
            return json.dumps({"error": result.error})
        return result.output

    async def execute(self, name: str, arguments: str | dict) -> str:
        """Execute a tool asynchronously.  Returns a JSON string.

        Permission check is applied first when a PermissionEngine is set.
        """
        # Permission gate (fail-closed)
        if self._permission_engine:
            from framework.errors import PermissionDeniedError
            try:
                self._permission_engine.require_tool(name)
            except PermissionDeniedError as exc:
                logger.warning("[registry] Permission denied for tool '%s': %s", name, exc)
                return json.dumps({"error": str(exc)})

        tool = self._tools.get(name)
        if not tool:
            return json.dumps({"error": f"Tool '{name}' is not registered"})

        kwargs = self._parse_args(arguments)

        try:
            result = await tool.execute(**kwargs)
        except Exception as exc:
            logger.warning("[registry] Tool '%s' raised %s: %s", name, type(exc).__name__, exc)
            return json.dumps({"error": str(exc)})

        if result.error:
            return json.dumps({"error": result.error})
        return result.output

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_args(arguments: str | dict) -> dict:
        if isinstance(arguments, dict):
            return arguments
        if not arguments:
            return {}
        try:
            parsed = json.loads(arguments)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    @staticmethod
    def _run_async(tool: "BaseTool", kwargs: dict) -> "ToolResult":
        """Run an async tool in a new event loop (fallback for sync callers)."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We are inside an async context — use run_in_executor trick.
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(asyncio.run, tool.execute(**kwargs))
                    return future.result(timeout=120)
            return loop.run_until_complete(tool.execute(**kwargs))
        except RuntimeError:
            return asyncio.run(tool.execute(**kwargs))

    def __len__(self) -> int:
        return len(self._tools)

    def __repr__(self) -> str:  # pragma: no cover
        return f"ToolRegistry({list(self._tools.keys())})"


# ---------------------------------------------------------------------------
# Global (per-process) registry
# ---------------------------------------------------------------------------

_default_registry = ToolRegistry()


def get_registry() -> ToolRegistry:
    """Return the global (per-process) ToolRegistry instance."""
    return _default_registry
