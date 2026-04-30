"""Self-registering tool registry.

Tools call ``register_tool()`` at module import time.  Adapters call
``list_tools()`` to discover all registered tools.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from common.tools.base import ConstellationTool

_registry: dict[str, "ConstellationTool"] = {}


def register_tool(tool: "ConstellationTool") -> None:
    """Register *tool*.  Raises ``ValueError`` if name already registered."""
    name = tool.schema.name
    if name in _registry:
        raise ValueError(f"Tool already registered: {name!r}")
    _registry[name] = tool


def get_tool(name: str) -> "ConstellationTool":
    """Return registered tool by name.  Raises ``KeyError`` if not found."""
    if name not in _registry:
        available = ", ".join(_registry) or "(none)"
        raise KeyError(f"Unknown tool: {name!r}. Available: {available}")
    return _registry[name]


def list_tools() -> list["ConstellationTool"]:
    """Return all registered tools in registration order."""
    return list(_registry.values())


def is_registered(name: str) -> bool:
    return name in _registry


def clear_registry() -> None:
    """Remove all registered tools.  Intended for test isolation only."""
    _registry.clear()
