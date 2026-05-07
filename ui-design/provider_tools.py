"""Internal UI-Design provider tools for agentic runtime.

These tools wrap figma_client and stitch_client directly.
They are registered in the global tool registry so the connect-agent runtime
can expose them to the LLM running inside the UI-Design Agent process.

Usage in app.py:
    import ui_design.provider_tools as _udt     # auto-registers tools
    _udt.configure_ui_provider_tools(
        message=message,
        permission_fn=lambda action, target: _require_ui_permission(
            action=action, target=target, message=message
        ),
    )
"""
from __future__ import annotations

import json
import sys
import os
from typing import Callable

from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import is_registered, register_tool

# Add the ui-design agent directory to sys.path so figma_client / stitch_client
# can be imported as local modules.
_AGENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
_UI_AGENT_DIR = os.path.join(_AGENT_DIR, "ui-design")
if _UI_AGENT_DIR not in sys.path:
    sys.path.insert(0, _UI_AGENT_DIR)

# ---------------------------------------------------------------------------
# Per-task context — configured by configure_ui_provider_tools() before
# run_agentic() is called.
# ---------------------------------------------------------------------------
_current_message: dict = {}
_permission_fn: Callable[[str, str], None] | None = None


def configure_ui_provider_tools(
    *,
    message: dict,
    permission_fn: Callable[[str, str], None] | None = None,
) -> None:
    """Wire up the permission callback for the current task."""
    global _current_message, _permission_fn
    _current_message = message
    _permission_fn = permission_fn


def _require(action: str, target: str) -> None:
    if _permission_fn:
        _permission_fn(action, target)


# ---------------------------------------------------------------------------
# Figma tools
# ---------------------------------------------------------------------------

class _FigmaListPagesTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="figma_list_pages",
            description=(
                "List all pages in a Figma file. "
                "Returns page names, IDs, and node count. "
                "Use the file key from the Figma URL: figma.com/file/<key>/..."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_key": {
                        "type": "string",
                        "description": "Figma file key (from the Figma URL).",
                    },
                },
                "required": ["file_key"],
            },
        )

    def execute(self, args: dict) -> dict:
        _require("figma.read", args.get("file_key", ""))
        import figma_client as fc
        try:
            pages = fc.list_pages(args.get("file_key", ""))
            return self.ok(json.dumps(pages, ensure_ascii=False))
        except Exception as exc:
            return self.error(f"figma_list_pages: {exc}")


class _FigmaFetchPageTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="figma_fetch_page",
            description=(
                "Fetch a Figma file page by name or page ID. "
                "Returns the full design spec including components, layers, and styles. "
                "Uses fuzzy matching if the exact page name is not found."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_key": {
                        "type": "string",
                        "description": "Figma file key (from the Figma URL).",
                    },
                    "page_name": {
                        "type": "string",
                        "description": "Page name (fuzzy matched) or page node ID.",
                    },
                },
                "required": ["file_key"],
            },
        )

    def execute(self, args: dict) -> dict:
        _require("figma.read", args.get("file_key", ""))
        import figma_client as fc
        try:
            page = fc.fetch_page(
                args.get("file_key", ""),
                args.get("page_name", ""),
            )
            return self.ok(json.dumps(page, ensure_ascii=False))
        except Exception as exc:
            return self.error(f"figma_fetch_page: {exc}")


class _FigmaFetchNodeTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="figma_fetch_node",
            description=(
                "Fetch details of a specific Figma node (component, frame, group, etc.). "
                "Returns the node spec including properties, children, and style overrides."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "file_key": {
                        "type": "string",
                        "description": "Figma file key (from the Figma URL).",
                    },
                    "node_id": {
                        "type": "string",
                        "description": "Figma node ID.",
                    },
                },
                "required": ["file_key", "node_id"],
            },
        )

    def execute(self, args: dict) -> dict:
        _require("element.inspect", args.get("file_key", ""))
        import figma_client as fc
        try:
            node = fc.fetch_node(args.get("file_key", ""), args.get("node_id", ""))
            return self.ok(json.dumps(node, ensure_ascii=False))
        except Exception as exc:
            return self.error(f"figma_fetch_node: {exc}")


# ---------------------------------------------------------------------------
# Stitch tools
# ---------------------------------------------------------------------------

class _StitchListScreensTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="stitch_list_screens",
            description=(
                "List all screens in a Google Stitch project. "
                "Returns screen names, IDs, and component counts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Stitch project ID.",
                    },
                },
                "required": ["project_id"],
            },
        )

    def execute(self, args: dict) -> dict:
        _require("stitch.read", args.get("project_id", ""))
        import stitch_client as sc
        try:
            screens = sc.list_screens(args.get("project_id", ""))
            return self.ok(json.dumps(screens, ensure_ascii=False))
        except Exception as exc:
            return self.error(f"stitch_list_screens: {exc}")


class _StitchFetchScreenTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="stitch_fetch_screen",
            description=(
                "Fetch full design data for a specific Google Stitch screen. "
                "Returns the screen spec including components, layout, and styles."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Stitch project ID.",
                    },
                    "screen_id": {
                        "type": "string",
                        "description": "Screen ID.",
                    },
                },
                "required": ["project_id", "screen_id"],
            },
        )

    def execute(self, args: dict) -> dict:
        _require("stitch.read", args.get("project_id", ""))
        import stitch_client as sc
        try:
            screen = sc.fetch_screen(args.get("project_id", ""), args.get("screen_id", ""))
            return self.ok(json.dumps(screen, ensure_ascii=False))
        except Exception as exc:
            return self.error(f"stitch_fetch_screen: {exc}")


class _StitchFindScreenByNameTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="stitch_find_screen_by_name",
            description=(
                "Find a Google Stitch screen by name with fuzzy matching. "
                "Returns the best matching screen with its design data."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Stitch project ID.",
                    },
                    "screen_name": {
                        "type": "string",
                        "description": "Screen name to search for (fuzzy matched).",
                    },
                },
                "required": ["project_id", "screen_name"],
            },
        )

    def execute(self, args: dict) -> dict:
        _require("stitch.read", args.get("project_id", ""))
        import stitch_client as sc
        try:
            screen = sc.find_screen_by_name(
                args.get("project_id", ""), args.get("screen_name", "")
            )
            return self.ok(json.dumps(screen, ensure_ascii=False))
        except Exception as exc:
            return self.error(f"stitch_find_screen_by_name: {exc}")


class _StitchFetchImageTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="stitch_fetch_image",
            description="Fetch a rendered image of a Google Stitch screen.",
            input_schema={
                "type": "object",
                "properties": {
                    "project_id": {
                        "type": "string",
                        "description": "Stitch project ID.",
                    },
                    "screen_id": {
                        "type": "string",
                        "description": "Screen ID.",
                    },
                },
                "required": ["project_id", "screen_id"],
            },
        )

    def execute(self, args: dict) -> dict:
        _require("stitch.read", args.get("project_id", ""))
        import stitch_client as sc
        try:
            image_data = sc.fetch_image(args.get("project_id", ""), args.get("screen_id", ""))
            if isinstance(image_data, dict):
                return self.ok(json.dumps(image_data, ensure_ascii=False))
            return self.ok(str(image_data))
        except Exception as exc:
            return self.error(f"stitch_fetch_image: {exc}")


# ---------------------------------------------------------------------------
# Self-registration — runs once at import time.
# ---------------------------------------------------------------------------
_TOOLS = [
    _FigmaListPagesTool(),
    _FigmaFetchPageTool(),
    _FigmaFetchNodeTool(),
    _StitchListScreensTool(),
    _StitchFetchScreenTool(),
    _StitchFindScreenByNameTool(),
    _StitchFetchImageTool(),
]

for _t in _TOOLS:
    if not is_registered(_t.schema.name):
        register_tool(_t)
