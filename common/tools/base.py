"""Base classes for Constellation tools.

Each tool is defined exactly once as a ``ConstellationTool`` subclass and
registers itself at import time.  Adapters (MCP or native) discover all
registered tools via ``common.tools.registry.list_tools()``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class ToolSchema:
    """Metadata describing a tool's interface."""

    name: str
    description: str
    input_schema: dict


class ConstellationTool(ABC):
    """Abstract base for all Constellation tools.

    Subclasses declare their schema and implement ``execute()``.
    Import the module to trigger self-registration via ``register_tool()``.
    """

    @property
    @abstractmethod
    def schema(self) -> ToolSchema:
        """Tool metadata (name, description, JSON Schema for arguments)."""
        ...

    @abstractmethod
    def execute(self, args: dict) -> dict:
        """Run the tool.

        Returns a MCP-compatible result dict::

            {"content": [{"type": "text", "text": "..."}], "isError": False}
        """
        ...

    @staticmethod
    def ok(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "isError": False}

    @staticmethod
    def error(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "isError": True}
