"""Registry discovery tools for the Team Lead Agent.

Allows the Team Lead to query the Registry for available agents and capabilities.
Import this module to register all registry tools.
"""

from __future__ import annotations

import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen

from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import register_tool

_REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")


def _load_registry_json(path: str) -> list | dict:
    req = Request(
        f"{_REGISTRY_URL}{path}",
        headers={"Accept": "application/json"},
    )
    with urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _summarize_agent_status(agent: dict) -> dict:
    instances = list(agent.get("instances") or [])
    idle_instances = sum(1 for instance in instances if instance.get("status") == "idle")
    busy_instances = sum(1 for instance in instances if instance.get("status") == "busy")
    last_heartbeat = ""
    for instance in instances:
        heartbeat = str(instance.get("updated_at") or instance.get("updatedAt") or "").strip()
        if heartbeat and heartbeat > last_heartbeat:
            last_heartbeat = heartbeat
    status = "healthy"
    if not instances:
        status = "unavailable"
    elif idle_instances == 0:
        status = "degraded"
    return {
        "agentId": agent.get("agent_id") or agent.get("agentId") or "",
        "displayName": agent.get("display_name") or agent.get("displayName") or "",
        "capabilities": list(agent.get("capabilities") or []),
        "instanceCount": len(instances),
        "idleInstances": idle_instances,
        "busyInstances": busy_instances,
        "status": status,
        "lastHeartbeat": last_heartbeat,
    }


class RegistryQueryTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="registry_query",
            description=(
                "Query the Constellation Registry for agents that provide a specific capability. "
                "Returns agent metadata including URLs and instance status."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "capability": {
                        "type": "string",
                        "description": "Capability identifier to search for, e.g. 'web.task.execute'",
                    },
                },
                "required": ["capability"],
            },
        )

    def execute(self, args: dict) -> dict:
        capability = args.get("capability", "").strip()
        if not capability:
            return self.error("Missing required argument: capability")
        try:
            data = _load_registry_json(f"/query?capability={capability}")
            return self.ok(json.dumps(data, ensure_ascii=False, indent=2))
        except (URLError, OSError) as exc:
            return self.error(f"Registry query failed: {exc}")


class RegistryListAgentsTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="registry_list_agents",
            description="List all agents currently registered in the Constellation Registry.",
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    def execute(self, args: dict) -> dict:
        del args
        try:
            data = _load_registry_json("/agents")
            return self.ok(json.dumps(data, ensure_ascii=False, indent=2))
        except (URLError, OSError) as exc:
            return self.error(f"Registry list agents failed: {exc}")


class RegistryAgentStatusTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="registry_agent_status",
            description=(
                "Summarize the runtime status of registered agents by capability or agent id, "
                "including instance counts and availability."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "capability": {
                        "type": "string",
                        "description": "Optional capability identifier to filter by.",
                    },
                    "agentId": {
                        "type": "string",
                        "description": "Optional agent id to inspect directly.",
                    },
                },
                "required": [],
            },
        )

    def execute(self, args: dict) -> dict:
        capability = str(args.get("capability") or "").strip()
        agent_id = str(args.get("agentId") or "").strip()
        if not capability and not agent_id:
            return self.error("Provide either capability or agentId")
        try:
            if capability:
                agents = _load_registry_json(f"/query?capability={capability}")
            else:
                agents = _load_registry_json("/agents")
            items = list(agents or [])
            if agent_id:
                items = [item for item in items if (item.get("agent_id") or item.get("agentId")) == agent_id]
                if items and "instances" not in items[0]:
                    status_items = []
                    for item in items:
                        item_id = item.get("agent_id") or item.get("agentId")
                        instances = _load_registry_json(f"/agents/{item_id}/instances")
                        normalized = dict(item)
                        normalized["instances"] = list(instances or [])
                        status_items.append(normalized)
                    items = status_items
            summary = [_summarize_agent_status(item) for item in items]
            return self.ok(json.dumps(summary, ensure_ascii=False, indent=2))
        except (URLError, OSError) as exc:
            return self.error(f"Registry agent status failed: {exc}")


class CheckAgentStatusTool(ConstellationTool):
    """check_agent_status — canonical name as defined in the design doc (Section 6.2.1)."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="check_agent_status",
            description=(
                "Query the runtime status and availability of another agent in the Constellation system. "
                "Returns health status, instance counts, and last heartbeat. "
                "Use this before dispatching tasks to confirm the target agent is healthy."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "capability": {
                        "type": "string",
                        "description": "Capability identifier to look up (preferred), e.g. 'scm.pr.create'.",
                    },
                    "agentId": {
                        "type": "string",
                        "description": "Optional agent ID to look up directly.",
                    },
                },
                "required": [],
            },
        )

    def execute(self, args: dict) -> dict:
        capability = str(args.get("capability") or "").strip()
        agent_id = str(args.get("agentId") or "").strip()
        if not capability and not agent_id:
            return self.error("Provide either capability or agentId.")
        try:
            if capability:
                agents = _load_registry_json(f"/query?capability={capability}")
            else:
                agents = _load_registry_json("/agents")
            items = list(agents or [])
            if agent_id:
                items = [item for item in items if (item.get("agent_id") or item.get("agentId")) == agent_id]
                if items and "instances" not in items[0]:
                    enriched = []
                    for item in items:
                        item_id = item.get("agent_id") or item.get("agentId")
                        instances = _load_registry_json(f"/agents/{item_id}/instances")
                        normalized = dict(item)
                        normalized["instances"] = list(instances or [])
                        enriched.append(normalized)
                    items = enriched
            if not items:
                return self.ok(
                    json.dumps(
                        {"status": "unavailable", "reason": "not_registered", "capability": capability, "agentId": agent_id},
                        ensure_ascii=False,
                    )
                )
            summary = [_summarize_agent_status(item) for item in items]
            return self.ok(json.dumps(summary, ensure_ascii=False, indent=2))
        except (URLError, OSError) as exc:
            return self.ok(
                json.dumps(
                    {"status": "unknown", "reason": "registry_unavailable", "error": str(exc)},
                    ensure_ascii=False,
                )
            )


class ListAvailableAgentsTool(ConstellationTool):
    """list_available_agents — canonical name as defined in the design doc (Section 6.2)."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="list_available_agents",
            description=(
                "List all agents currently registered in the Constellation system, "
                "including their capabilities and availability status. "
                "Use this to discover what agents and capabilities are online."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "required": [],
            },
        )

    def execute(self, args: dict) -> dict:
        del args
        try:
            agents = _load_registry_json("/agents")
            items = list(agents or [])
            summary = [_summarize_agent_status(item) for item in items]
            return self.ok(json.dumps(summary, ensure_ascii=False, indent=2))
        except (URLError, OSError) as exc:
            return self.ok(
                json.dumps(
                    {"status": "unknown", "reason": "registry_unavailable", "error": str(exc)},
                    ensure_ascii=False,
                )
            )


register_tool(RegistryQueryTool())
register_tool(RegistryListAgentsTool())
register_tool(RegistryAgentStatusTool())
register_tool(CheckAgentStatusTool())
register_tool(ListAvailableAgentsTool())
