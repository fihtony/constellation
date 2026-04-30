"""Embeddable MCP server for containerized agents.

Agents can start this server as a subprocess to expose Constellation tools
over stdio (JSON-RPC 2.0) to agentic runtimes that support the Model Context
Protocol (claude-code, copilot-cli, etc.).

Usage from within an agent container::

    import subprocess, os
    proc = subprocess.Popen(
        ["python3", "-m", "common.mcp.constellation_server",
         "--tools", "jira_tools,scm_tools"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env={**os.environ, "PYTHONPATH": "/app"},
    )

Or run directly::

    python3 -m common.mcp.constellation_server --tools jira_tools,scm_tools

The ``--tools`` argument selects which tool modules to import/register.
Each name maps to ``common.tools.<name>`` and must call ``register_tool()``
at import time.  If omitted, all available tool modules are loaded.
"""

from __future__ import annotations

import argparse
import importlib
import sys

# Known tool module names (under common.tools)
_KNOWN_TOOL_MODULES = [
    "jira_tools",
    "scm_tools",
    "design_tools",
    "dev_agent_tools",
    "progress_tools",
    "registry_tools",
    "team_lead_tools",
]


def _tool_names_from_args(args: list[str] | None) -> list[str] | None:
    args = [str(arg) for arg in (args or [])]
    for idx, arg in enumerate(args):
        if arg == "--tools" and idx + 1 < len(args):
            return [name.strip() for name in args[idx + 1].split(",") if name.strip()]
        if arg.startswith("--tools="):
            return [name.strip() for name in arg.split("=", 1)[1].split(",") if name.strip()]
    return None


def _is_constellation_server_command(command: str, args: list[str] | None) -> bool:
    command = str(command or "").strip()
    args = [str(arg) for arg in (args or [])]
    if not command:
        return False
    if len(args) >= 2 and args[0] == "-m" and args[1] == "common.mcp.constellation_server":
        return True
    joined = " ".join([command, *args])
    if "common.mcp.constellation_server" in joined:
        return True
    normalized = joined.replace("\\", "/")
    return normalized.endswith("common/mcp/constellation_server.py") or "/common/mcp/constellation_server.py " in normalized


def _load_tool_modules(names: list[str] | None = None) -> list[str]:
    """Import tool modules to trigger self-registration.

    Returns the list of module names that were successfully loaded.
    """
    from common.tools.registry import list_tools

    targets = names if names else list(_KNOWN_TOOL_MODULES)
    loaded = []
    for name in targets:
        module_path = f"common.tools.{name}"
        try:
            module = sys.modules.get(module_path)
            if module is not None and not list_tools():
                importlib.reload(module)
            else:
                importlib.import_module(module_path)
            loaded.append(name)
        except Exception as exc:  # noqa: BLE001
            print(f"[mcp-server] Warning: failed to load {module_path}: {exc}", file=sys.stderr)
    return loaded


def bootstrap_tools_from_mcp_servers(mcp_servers: dict | None) -> dict[str, list[str]]:
    """Load Constellation tool modules referenced by compatible MCP server configs.

    This is used by OpenAI/function-calling fallbacks that cannot speak MCP
    natively but can still expose the same underlying tools by importing the
    registered tool modules locally.
    """
    loaded_by_server: dict[str, list[str]] = {}
    for server_name, config in (mcp_servers or {}).items():
        if not isinstance(config, dict):
            continue
        command = str(config.get("command") or "").strip()
        args = config.get("args") or []
        if not _is_constellation_server_command(command, args):
            continue
        tool_names = _tool_names_from_args(args)
        loaded = _load_tool_modules(tool_names)
        loaded_by_server[str(server_name)] = loaded
    return loaded_by_server


def main(argv: list[str] | None = None) -> None:
    """Entry point for the containerized MCP server."""
    parser = argparse.ArgumentParser(description="Constellation MCP Server")
    parser.add_argument(
        "--tools",
        default="",
        help="Comma-separated tool module names to load (e.g. jira_tools,scm_tools). "
             "Omit to load all available tool modules.",
    )
    args = parser.parse_args(argv)

    tool_names = [n.strip() for n in args.tools.split(",") if n.strip()] if args.tools else None
    loaded = _load_tool_modules(tool_names)
    print(f"[mcp-server] Loaded tool modules: {loaded}", file=sys.stderr)

    from common.tools.mcp_adapter import start_mcp_server
    start_mcp_server()


if __name__ == "__main__":
    main()
