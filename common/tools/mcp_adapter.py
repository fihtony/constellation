"""MCP stdio server adapter.

Exposes all registered ``ConstellationTool`` instances as an MCP server
over stdin/stdout (JSON-RPC 2.0).  Consumed by agentic runtimes that
support the Model Context Protocol (claude-code, copilot-cli, etc.).

Usage::

    python3 -m common.tools.mcp_adapter
"""

from __future__ import annotations

import json
import sys

from common.tools.registry import get_tool, list_tools


def _handle_request(request: dict) -> dict:
    req_id = request.get("id")
    method = request.get("method", "")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "constellation", "version": "1.0.0"},
            },
        }

    if method == "tools/list":
        tools = list_tools()
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "tools": [
                    {
                        "name": t.schema.name,
                        "description": t.schema.description,
                        "inputSchema": t.schema.input_schema,
                    }
                    for t in tools
                ]
            },
        }

    if method == "tools/call":
        params = request.get("params") or {}
        name = params.get("name", "")
        args = params.get("arguments") or {}
        try:
            tool = get_tool(name)
            result = tool.execute(args)
            return {"jsonrpc": "2.0", "id": req_id, "result": result}
        except KeyError as exc:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": str(exc)},
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"content": [{"type": "text", "text": f"Tool error: {exc}"}], "isError": True},
            }

    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "error": {"code": -32601, "message": f"Method not found: {method}"},
    }


def start_mcp_server(*, stdin=None, stdout=None) -> None:
    """Start the MCP stdio server.  Reads JSON-RPC lines from *stdin*."""
    reader = stdin or sys.stdin
    writer = stdout or sys.stdout
    for line in reader:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError as exc:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": f"Parse error: {exc}"},
            }
        else:
            response = _handle_request(request)
        print(json.dumps(response), file=writer, flush=True)


if __name__ == "__main__":
    start_mcp_server()
