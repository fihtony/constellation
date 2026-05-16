"""UI Design boundary tools — in-process implementations using UIDesignAgentAdapter.

Registered by UIDesignAgentAdapter.start() so the global ToolRegistry has live
design tools before Team Lead calls register_team_lead_tools().
"""
from __future__ import annotations

import json
import os

from pathlib import Path as _Path

from framework.config import load_agent_config as _load_agent_cfg
from framework.devlog import AgentLogger
from framework.tools.base import BaseTool, ToolResult
from framework.tools.registry import get_registry

# Load agent_id from config.yaml — single source of truth for identity
_AGENT_ID: str = _load_agent_cfg(
    _Path(__file__).parent.name.replace("_", "-")
).get("agent_id", _Path(__file__).parent.name.replace("_", "-"))


def _log(task_id: str) -> AgentLogger:
    return AgentLogger(task_id=task_id, agent_name=_AGENT_ID)


def _get_adapter():
    from agents.ui_design.adapter import UIDesignAgentAdapter, ui_design_definition
    from framework.agent import AgentServices
    from framework.checkpoint import InMemoryCheckpointer
    from framework.event_store import InMemoryEventStore
    from framework.memory import InMemoryMemoryService
    from framework.plugin import PluginManager
    from framework.runtime.adapter import get_runtime
    from framework.session import InMemorySessionService
    from framework.skills import SkillsRegistry
    from framework.task_store import InMemoryTaskStore

    services = AgentServices(
        session_service=InMemorySessionService(),
        event_store=InMemoryEventStore(),
        memory_service=InMemoryMemoryService(),
        skills_registry=SkillsRegistry(),
        plugin_manager=PluginManager(),
        checkpoint_service=InMemoryCheckpointer(),
        runtime=get_runtime("claude-code", model=os.environ.get("OPENAI_MODEL", "claude-haiku-4-5-20251001")),
        registry_client=None,
        task_store=InMemoryTaskStore(),
    )
    return UIDesignAgentAdapter(definition=ui_design_definition, services=services)


class FetchDesign(BaseTool):
    name = "fetch_design"
    description = (
        "Fetch design specification from a Figma URL or a Google Stitch project. "
        "Provide either figma_url or stitch_project_id."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "figma_url": {"type": "string", "description": "Full Figma file URL."},
            "stitch_project_id": {"type": "string", "description": "Google Stitch project ID."},
            "stitch_screen_id": {"type": "string", "description": "Stitch screen ID (optional)."},
            "screen_name": {"type": "string", "description": "Screen name for Stitch (optional)."},
            "task_id": {"type": "string", "description": "Caller task ID for log correlation (optional)."},
        },
        "required": [],
    }

    def execute_sync(
        self,
        figma_url: str = "",
        stitch_project_id: str = "",
        stitch_screen_id: str = "",
        screen_name: str = "",
        task_id: str = "",
        **kw,
    ) -> ToolResult:
        log = _log(task_id)
        adapter = _get_adapter()
        if figma_url:
            log.info("fetch_design called", source="figma", figma_url=figma_url)
            result = adapter._dispatch(
                "figma.file.fetch", figma_url,
                {"metadata": {"figmaUrl": figma_url}},
            )
        elif stitch_project_id:
            if stitch_screen_id or screen_name:
                capability = "stitch.screen.fetch"
            else:
                capability = "stitch.screens.list"
            log.info("fetch_design called", source="stitch",
                     stitch_project_id=stitch_project_id, capability=capability)
            result = adapter._dispatch(
                capability, stitch_project_id,
                {
                    "metadata": {
                        "stitchProjectId": stitch_project_id,
                        "stitchScreenId": stitch_screen_id,
                        "screenName": screen_name,
                    }
                },
            )
        else:
            # Fall back to env-configured sources
            eff_project_id = os.environ.get("STITCH_PROJECT_ID", "")
            eff_screen_id = os.environ.get("STITCH_SCREEN_ID", "")
            eff_figma_url = os.environ.get("FIGMA_FILE_URL", "")
            if eff_project_id:
                return self.execute_sync(
                    stitch_project_id=eff_project_id,
                    stitch_screen_id=eff_screen_id,
                    task_id=task_id,
                )
            if eff_figma_url:
                return self.execute_sync(figma_url=eff_figma_url, task_id=task_id)
            log.warn("fetch_design: no design source configured")
            print("[ui-design-tools] No design source configured, returning empty context")
            return ToolResult(output=json.dumps({"design": {}, "status": "no_source"}))

        if result.get("error"):
            log.error("fetch_design failed", error=result["error"])
        else:
            log.debug("fetch_design ok")
        return ToolResult(output=json.dumps(result))


_TOOLS = [FetchDesign()]


def register_ui_design_tools() -> None:
    """Register in-process UI design tools (idempotent, won't override existing)."""
    registry = get_registry()
    existing = {s["function"]["name"] for s in registry.list_schemas()}
    for tool in _TOOLS:
        if tool.name not in existing:
            registry.register(tool)
    print(f"[ui-design-tools] Registered: {[t.name for t in _TOOLS if t.name not in existing]}")
