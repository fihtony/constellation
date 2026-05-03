"""Planning and context management tools for the Connect Agent runtime.

Provides: todo_write, compress.
Self-registers on import.
"""

from __future__ import annotations

from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import register_tool

# ---------------------------------------------------------------------------
# Shared state — set by the agent loop
# ---------------------------------------------------------------------------
_todo_manager = None
_compress_fn = None


def configure_planning_tools(*, todo_manager: object, compress_fn: object = None) -> None:
    """Wire up the TodoManager and compression callback.

    Called once by ConnectAgentAdapter before the agent loop starts.
    """
    global _todo_manager, _compress_fn
    _todo_manager = todo_manager
    _compress_fn = compress_fn


# ===================================================================
# todo_write
# ===================================================================

class TodoWriteTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="todo_write",
            description=(
                "Update the task plan.  Provide the complete list of todo items "
                "with their status (pending / in_progress / completed).  "
                "Only one item may be in_progress at a time."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "description": "List of todo items.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": "Task description.",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": ["pending", "in_progress", "completed"],
                                    "description": "Current status.",
                                },
                            },
                            "required": ["content", "status"],
                        },
                    },
                },
                "required": ["items"],
            },
        )

    def execute(self, args: dict) -> dict:
        if _todo_manager is None:
            return self.error("TodoManager not initialised.")
        items = args.get("items", [])
        # Validate: only one in_progress
        in_progress = [i for i in items if i.get("status") == "in_progress"]
        if len(in_progress) > 1:
            return self.error("Only one item may be in_progress at a time.")
        rendered = _todo_manager.update(items)
        return self.ok(rendered)


# ===================================================================
# compress
# ===================================================================

class CompressTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="compress",
            description=(
                "Manually trigger context compression to free up token budget.  "
                "Use when the conversation feels cluttered."
            ),
            input_schema={
                "type": "object",
                "properties": {},
            },
        )

    def execute(self, args: dict) -> dict:
        if _compress_fn is None:
            return self.error("Compression not configured.")
        try:
            _compress_fn()
            return self.ok("Context compressed successfully.")
        except Exception as exc:
            return self.error(f"Compression failed: {exc}")


register_tool(TodoWriteTool())
register_tool(CompressTool())
