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
            req = Request(
                f"{_REGISTRY_URL}/query?capability={capability}",
                headers={"Accept": "application/json"},
            )
            with urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
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
            req = Request(
                f"{_REGISTRY_URL}/agents",
                headers={"Accept": "application/json"},
            )
            with urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return self.ok(json.dumps(data, ensure_ascii=False, indent=2))
        except (URLError, OSError) as exc:
            return self.error(f"Registry list agents failed: {exc}")


register_tool(RegistryQueryTool())
register_tool(RegistryListAgentsTool())
