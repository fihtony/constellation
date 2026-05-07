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
        audit_fn=lambda operation, target, result, duration_ms=0: _write_ui_design_audit(...),
    )
"""
from __future__ import annotations

import json
import sys
import os
import time
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
_audit_fn: Callable[..., None] | None = None


def configure_ui_provider_tools(
    *,
    message: dict,
    permission_fn: Callable[[str, str], None] | None = None,
    audit_fn: Callable[..., None] | None = None,
) -> None:
    """Wire up the permission and audit callbacks for the current task."""
    global _current_message, _permission_fn, _audit_fn
    _current_message = message
    _permission_fn = permission_fn
    _audit_fn = audit_fn


def _require(action: str, target: str) -> None:
    if _permission_fn:
        _permission_fn(action, target)


def _audit(operation: str, target: str, result: dict, duration_ms: int = 0) -> None:
    """Write a structured audit entry if an audit function is configured."""
    if _audit_fn:
        _audit_fn(operation, target, result, duration_ms)


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
        file_key = args.get("file_key", "")
        _require("figma.read", file_key)
        import figma_client as fc
        t0 = time.perf_counter()
        try:
            pages = fc.list_pages(file_key)
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _audit("figma.list_pages", file_key, {"success": True, "pageCount": len(pages) if isinstance(pages, list) else None}, duration_ms)
            return self.ok(json.dumps(pages, ensure_ascii=False))
        except Exception as exc:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _audit("figma.list_pages", file_key, {"success": False, "error": str(exc)}, duration_ms)
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
        file_key = args.get("file_key", "")
        _require("figma.read", file_key)
        import figma_client as fc
        t0 = time.perf_counter()
        try:
            page = fc.fetch_page(file_key, args.get("page_name", ""))
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _audit("figma.fetch_page", f"{file_key}/{args.get('page_name', '')}", {"success": True}, duration_ms)
            return self.ok(json.dumps(page, ensure_ascii=False))
        except Exception as exc:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _audit("figma.fetch_page", file_key, {"success": False, "error": str(exc)}, duration_ms)
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
        file_key = args.get("file_key", "")
        node_id = args.get("node_id", "")
        _require("element.inspect", file_key)
        import figma_client as fc
        t0 = time.perf_counter()
        try:
            node = fc.fetch_node(file_key, node_id)
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _audit("figma.fetch_node", f"{file_key}/{node_id}", {"success": True}, duration_ms)
            return self.ok(json.dumps(node, ensure_ascii=False))
        except Exception as exc:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _audit("figma.fetch_node", f"{file_key}/{node_id}", {"success": False, "error": str(exc)}, duration_ms)
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
        project_id = args.get("project_id", "")
        _require("stitch.read", project_id)
        import stitch_client as sc
        t0 = time.perf_counter()
        try:
            screens = sc.list_screens(project_id)
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _audit("stitch.list_screens", project_id, {"success": True, "screenCount": len(screens) if isinstance(screens, list) else None}, duration_ms)
            return self.ok(json.dumps(screens, ensure_ascii=False))
        except Exception as exc:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _audit("stitch.list_screens", project_id, {"success": False, "error": str(exc)}, duration_ms)
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
        project_id = args.get("project_id", "")
        screen_id = args.get("screen_id", "")
        _require("stitch.read", project_id)
        import stitch_client as sc
        t0 = time.perf_counter()
        try:
            screen = sc.fetch_screen(project_id, screen_id)
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _audit("stitch.fetch_screen", f"{project_id}/{screen_id}", {"success": True}, duration_ms)
            return self.ok(json.dumps(screen, ensure_ascii=False))
        except Exception as exc:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _audit("stitch.fetch_screen", f"{project_id}/{screen_id}", {"success": False, "error": str(exc)}, duration_ms)
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
        project_id = args.get("project_id", "")
        screen_name = args.get("screen_name", "")
        _require("stitch.read", project_id)
        import stitch_client as sc
        t0 = time.perf_counter()
        try:
            screen = sc.find_screen_by_name(project_id, screen_name)
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _audit("stitch.find_screen_by_name", f"{project_id}/{screen_name}", {"success": True}, duration_ms)
            return self.ok(json.dumps(screen, ensure_ascii=False))
        except Exception as exc:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _audit("stitch.find_screen_by_name", f"{project_id}/{screen_name}", {"success": False, "error": str(exc)}, duration_ms)
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
        project_id = args.get("project_id", "")
        screen_id = args.get("screen_id", "")
        _require("stitch.read", project_id)
        import stitch_client as sc
        t0 = time.perf_counter()
        try:
            image_data = sc.fetch_image(project_id, screen_id)
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _audit("stitch.fetch_image", f"{project_id}/{screen_id}", {"success": True}, duration_ms)
            if isinstance(image_data, dict):
                return self.ok(json.dumps(image_data, ensure_ascii=False))
            return self.ok(str(image_data))
        except Exception as exc:
            duration_ms = int((time.perf_counter() - t0) * 1000)
            _audit("stitch.fetch_image", f"{project_id}/{screen_id}", {"success": False, "error": str(exc)}, duration_ms)
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
