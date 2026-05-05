"""Native (function_calling) adapter.

Exposes all registered ``ConstellationTool`` instances as OpenAI-compatible
function definitions for use with the connect-agent ``run_agentic()`` loop.
"""

from __future__ import annotations

from common.tools.registry import get_tool, list_tools


def get_function_definitions(tool_names: list[str] | None = None) -> list[dict]:
    """Return OpenAI function_calling schema for registered tools.

    When *tool_names* is provided, only currently registered tools with those
    names are returned, preserving the requested order.
    """
    if tool_names:
        tools = []
        for name in tool_names:
            try:
                tools.append(get_tool(name))
            except KeyError:
                continue
    else:
        tools = list_tools()
    return [
        {
            "type": "function",
            "function": {
                "name": t.schema.name,
                "description": t.schema.description,
                "parameters": t.schema.input_schema,
            },
        }
        for t in tools
    ]


def dispatch_function_call(name: str, args: dict) -> str:
    """Execute a function call and return its text result.

    Used by the connect-agent multi-turn loop to feed tool results back
    into the conversation.
    """
    tool = get_tool(name)
    result = tool.execute(args)
    content = result.get("content") or []
    texts = [c["text"] for c in content if isinstance(c, dict) and c.get("type") == "text"]
    return "\n".join(texts)
