"""MCP preparation helpers for the Connect Agent runtime.

The current implementation supports two safe modes:
- Allowlist validation for requested MCP server configs.
- Local Constellation MCP bootstrap, which exposes selected tool modules as
  ordinary function-calling tools for the connect-agent loop.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

from common.runtime.connect_agent.policy import PolicyProfile


@dataclass
class PreparedMcpContext:
    requested_servers: dict[str, dict] = field(default_factory=dict)
    approved_servers: dict[str, dict] = field(default_factory=dict)
    loaded_tools: dict[str, list[str]] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def prepare_mcp_servers(
    mcp_servers: dict | None,
    *,
    profile: PolicyProfile,
) -> PreparedMcpContext:
    requested = _load_requested_servers(mcp_servers)
    context = PreparedMcpContext(requested_servers=requested)
    if not requested:
        return context

    allowlist = _load_allowlist()
    allowed_server_ids = _resolve_allowed_server_ids(profile, set(requested))

    for server_id, config in requested.items():
        if allowed_server_ids is not None and server_id not in allowed_server_ids:
            context.warnings.append(f"MCP server '{server_id}' is not allowed by the active policy profile.")
            continue
        if allowlist and server_id not in allowlist:
            context.warnings.append(f"MCP server '{server_id}' is not present in the configured allowlist.")
            continue
        if allowlist.get(server_id, {}).get("enabled") is False:
            context.warnings.append(f"MCP server '{server_id}' is disabled by the configured allowlist.")
            continue
        context.approved_servers[server_id] = config

    if context.approved_servers:
        try:
            from common.mcp.constellation_server import bootstrap_tools_from_mcp_servers

            context.loaded_tools = bootstrap_tools_from_mcp_servers(context.approved_servers)
        except Exception as exc:  # noqa: BLE001
            context.warnings.append(f"Failed to bootstrap approved MCP servers: {exc}")

    for server_id in context.approved_servers:
        if server_id not in context.loaded_tools:
            context.warnings.append(
                f"MCP server '{server_id}' passed policy validation but is not a local Constellation MCP bridge; "
                "connect-agent currently supports allowlisted local bootstrap only."
            )
    return context


def _load_requested_servers(explicit_servers: dict | None) -> dict[str, dict]:
    if isinstance(explicit_servers, dict) and explicit_servers:
        return {str(name): dict(config) for name, config in explicit_servers.items() if isinstance(config, dict)}

    path = os.environ.get("CONNECT_AGENT_MCP_CONFIG", "").strip()
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    if isinstance(raw.get("mcpServers"), dict):
        return {str(name): dict(config) for name, config in raw["mcpServers"].items() if isinstance(config, dict)}
    if isinstance(raw.get("servers"), dict):
        servers: dict[str, dict] = {}
        for name, config in raw["servers"].items():
            if isinstance(config, dict):
                servers[str(name)] = dict(config)
        return servers
    return {}


def _load_allowlist() -> dict[str, dict]:
    path = os.environ.get("CONNECT_AGENT_MCP_ALLOWLIST", "").strip()
    if not path or not os.path.isfile(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    servers = raw.get("servers")
    if isinstance(servers, dict):
        return {str(name): dict(config) for name, config in servers.items() if isinstance(config, dict)}
    return {}


def _resolve_allowed_server_ids(profile: PolicyProfile, requested_ids: set[str]) -> set[str] | None:
    if "*" in profile.allow_mcp_servers:
        allowed_ids: set[str] | None = None
    else:
        allowed_ids = set(profile.allow_mcp_servers)

    env_allowed = os.environ.get("CONNECT_AGENT_ALLOWED_MCP_SERVERS", "").strip()
    if env_allowed:
        env_ids = {item.strip() for item in env_allowed.split(",") if item.strip()}
        allowed_ids = env_ids if allowed_ids is None else allowed_ids & env_ids

    if allowed_ids is None:
        return set(requested_ids)
    return allowed_ids