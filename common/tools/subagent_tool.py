"""Subagent tool for the Connect Agent runtime.

Provides context-isolated sub-execution for exploration or focused tasks.
Self-registers on import.
"""

from __future__ import annotations

from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import register_tool

# ---------------------------------------------------------------------------
# Shared state — set by the agent loop
# ---------------------------------------------------------------------------
_subagent_fn = None


def configure_subagent_tool(*, subagent_fn: object) -> None:
    """Wire up the subagent execution callback.

    Called once by ConnectAgentAdapter before the agent loop starts.
    """
    global _subagent_fn
    _subagent_fn = subagent_fn


class SubagentTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="subagent",
            description=(
                "Launch an isolated sub-agent with its own context to perform "
                "a focused task.  The sub-agent shares the file system but has "
                "a separate conversation history.  Returns a text summary. "
                "Useful for exploration, research, or parallel investigation "
                "without cluttering the main context."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Task description for the sub-agent.",
                    },
                    "tools": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Tool names available to the sub-agent. "
                            "Default: read_file, glob, grep, bash."
                        ),
                    },
                    "max_turns": {
                        "type": "integer",
                        "description": "Maximum turns for the sub-agent (default 30).",
                    },
                },
                "required": ["prompt"],
            },
        )

    def execute(self, args: dict) -> dict:
        if _subagent_fn is None:
            return self.error("Subagent execution not configured.")
        prompt = args.get("prompt", "")
        tools = args.get("tools") or ["read_file", "glob", "grep", "bash"]
        max_turns = min(int(args.get("max_turns", 30)), 50)

        try:
            result = _subagent_fn(prompt=prompt, tools=tools, max_turns=max_turns)
            return self.ok(result)
        except Exception as exc:
            return self.error(f"Subagent failed: {exc}")


register_tool(SubagentTool())
