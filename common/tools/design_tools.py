"""UI Design tools for agentic runtimes.

Provides tools to fetch design specs from Figma/Stitch via the UI Design Agent.
Import this module to register all design tools.
"""

from __future__ import annotations

import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen

from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import register_tool

_REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")
_ACK_TIMEOUT = int(os.environ.get("A2A_ACK_TIMEOUT_SECONDS", "15"))


def _discover_design_url(capability: str) -> str | None:
    try:
        req = Request(
            f"{_REGISTRY_URL}/query?capability={capability}",
            headers={"Accept": "application/json"},
        )
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        agents = data.get("agents") or []
        for agent in agents:
            instances = agent.get("instances") or []
            for inst in instances:
                url = inst.get("url") or agent.get("baseUrl")
                if url:
                    return url.rstrip("/")
        return None
    except Exception:  # noqa: BLE001
        return None


def _a2a_send(agent_url: str, capability: str, params: dict) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": "tool-call",
        "method": "message:send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": json.dumps(params)}],
            },
            "metadata": {"capability": capability, **params},
        },
    }
    req = Request(
        f"{agent_url}/message:send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=_ACK_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


class FigmaFetchScreenTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="design_fetch_figma_screen",
            description=(
                "Fetch a Figma screen / component specification. "
                "Returns the design spec including layout, colors, and component details."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "figma_url": {
                        "type": "string",
                        "description": "Figma node URL or file URL",
                    },
                    "node_id": {
                        "type": "string",
                        "description": "Optional Figma node ID to fetch a specific component",
                    },
                },
                "required": ["figma_url"],
            },
        )

    def execute(self, args: dict) -> dict:
        url = _discover_design_url("ui-design.figma.fetch")
        if not url:
            return self.error("UI Design Agent is not available (capability ui-design.figma.fetch not found).")
        try:
            result = _a2a_send(url, "ui-design.figma.fetch", args)
            return self.ok(json.dumps(result, ensure_ascii=False, indent=2))
        except (URLError, OSError) as exc:
            return self.error(f"Failed to fetch Figma screen: {exc}")


class StitchFetchScreenTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="design_fetch_stitch_screen",
            description="Fetch a Stitch design screen specification.",
            input_schema={
                "type": "object",
                "properties": {
                    "screen_id": {
                        "type": "string",
                        "description": "Stitch screen ID",
                    },
                },
                "required": ["screen_id"],
            },
        )

    def execute(self, args: dict) -> dict:
        url = _discover_design_url("ui-design.stitch.fetch")
        if not url:
            return self.error("UI Design Agent is not available (capability ui-design.stitch.fetch not found).")
        try:
            result = _a2a_send(url, "ui-design.stitch.fetch", args)
            return self.ok(json.dumps(result, ensure_ascii=False, indent=2))
        except (URLError, OSError) as exc:
            return self.error(f"Failed to fetch Stitch screen: {exc}")


register_tool(FigmaFetchScreenTool())
register_tool(StitchFetchScreenTool())
