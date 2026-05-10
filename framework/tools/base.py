"""Base tool types for the agent runtime.

All tools inherit from ``BaseTool`` and return a ``ToolResult``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolResult:
    """Standard result returned by any tool invocation."""

    output: str = ""
    error: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def success(self) -> bool:
        return not self.error


class BaseTool:
    """Abstract base for all tools.

    Subclasses implement either ``execute()`` (async) or ``execute_sync()``
    (sync).  The default ``execute()`` calls ``execute_sync()``; the default
    ``execute_sync()`` raises NotImplementedError so the registry can fall
    back to the async path.
    """

    name: str = ""
    description: str = ""
    parameters_schema: dict = {}  # JSON Schema for parameters

    def execute_sync(self, **kwargs: Any) -> ToolResult:
        """Synchronous execution.  Override this for pure-HTTP / stdlib tools."""
        raise NotImplementedError

    async def execute(self, **kwargs: Any) -> ToolResult:
        """Async execution.  Default delegates to execute_sync()."""
        return self.execute_sync(**kwargs)

    def to_openai_schema(self) -> dict:
        """Return an OpenAI function-calling compatible tool definition."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema or {"type": "object", "properties": {}},
            },
        }
