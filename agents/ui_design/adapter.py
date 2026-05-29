"""UI Design Agent adapter -- routes design requests to Figma or Stitch.

  figma.*   -> Figma REST API v1 (FigmaClient)
  stitch.*  -> Google Stitch MCP (StitchMcpClient)
  design.*  -> auto-routes by URL pattern
"""
from __future__ import annotations

import json
import os

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode
from framework.boundary_permissions import enforce_boundary_permission

ui_design_definition = AgentDefinition(
    agent_id="ui-design",
    name="UI Design Agent",
    description="Boundary adapter: Figma REST and Google Stitch MCP design context",
    mode=AgentMode.SINGLE_TURN,
    execution_mode=ExecutionMode.PERSISTENT,
    workflow=None,
    tools=[],
)


_UI_DESIGN_CAPABILITY_RULES: dict[str, dict[str, object]] = {
    "figma.page.fetch": {"tools": ["fetch_figma_page", "fetch_design"], "action": "figma.read"},
    "figma.file.fetch": {"tools": ["fetch_figma_page", "fetch_design"], "action": "figma.read"},
    "figma.design.fetch": {"tools": ["fetch_design"], "action": "figma.read"},
    "figma.pages.list": {"tools": ["fetch_figma_page", "fetch_design"], "action": "figma.read"},
    "figma.node.fetch": {"tools": ["fetch_figma_page", "fetch_design"], "action": "element.inspect"},
    "figma.styles.fetch": {"tools": ["fetch_design_tokens", "fetch_design"], "action": "element.inspect"},
    "stitch.project.get": {"tools": ["fetch_stitch_screen", "fetch_design"], "action": "stitch.read"},
    "stitch.screens.list": {"tools": ["fetch_stitch_screen", "fetch_design"], "action": "stitch.read"},
    "stitch.screen.fetch": {"tools": ["fetch_stitch_screen", "fetch_design"], "action": "stitch.read"},
    "stitch.screen.image": {"tools": ["export_design_screenshot", "fetch_stitch_screen", "fetch_design"], "action": "stitch.read"},
    "stitch.tools.list": {"tools": ["fetch_design"], "action": "stitch.read"},
}


def _enforce_ui_design_permission(capability: str, meta: dict) -> dict | None:
    normalized_capability = capability
    if capability.startswith("design."):
        normalized_capability = "figma.design.fetch" if "figma.com" in str(meta.get("designUrl") or "").lower() else "stitch.screen.fetch"
    rule = _UI_DESIGN_CAPABILITY_RULES.get(normalized_capability)
    if not rule:
        return None
    return enforce_boundary_permission(
        agent_id="ui-design",
        capability=capability,
        metadata=meta,
        required_tools=list(rule.get("tools") or []),
        grant_agent="ui-design",
        grant_action=str(rule.get("action") or "element.inspect"),
    )


class UIDesignAgentAdapter(BaseAgent):
    """Design context adapter supporting Figma REST and Google Stitch MCP.

    Parameters
    ----------
    figma_client:
        Optional pre-constructed FigmaClient (for testing / DI).
        Falls back to FIGMA_TOKEN env var.
    stitch_client:
        Optional pre-constructed StitchMcpClient (for testing / DI).
        Falls back to STITCH_API_KEY env var.
    """

    def __init__(
        self,
        definition: AgentDefinition,
        services: AgentServices,
        figma_client=None,
        stitch_client=None,
    ):
        super().__init__(definition, services)
        self._figma_client = figma_client
        self._stitch_client = stitch_client

    async def start(self) -> None:
        await super().start()
        from agents.ui_design.tools import register_ui_design_tools
        register_ui_design_tools()

    async def handle_message(self, message: dict) -> dict:
        from framework.a2a.protocol import Artifact, TaskState, TaskStatus

        task_store = self.services.task_store
        msg = message.get("message", message)
        cap = (msg.get("metadata") or {}).get("requestedCapability", "")
        meta = msg.get("metadata") or {}
        parts = msg.get("parts") or []
        text = next((p.get("text", "") for p in parts if p.get("text")), "")

        task = task_store.create_task(
            agent_id=self.definition.agent_id,
            metadata={"capability": cap},
        )

        result = self._dispatch(cap, text, msg)
        result = self._persist_workspace_outputs(cap, result, meta, task.id)
        artifact_metadata = {
            "agentId": "ui-design",
            "capability": cap,
            "taskId": task.id,
        }
        if result.get("local_folder"):
            artifact_metadata["localFolder"] = result.get("local_folder", "")
        if result.get("design_code_path"):
            artifact_metadata["designCodePath"] = result.get("design_code_path", "")
        if result.get("design_md_path"):
            artifact_metadata["designMdPath"] = result.get("design_md_path", "")
        if result.get("design_screen_path"):
            artifact_metadata["designScreenPath"] = result.get("design_screen_path", "")
        if result.get("files"):
            artifact_metadata["filesJson"] = json.dumps(result.get("files", []), ensure_ascii=False)
        artifacts = [Artifact(
            name="design-result",
            artifact_type="application/json",
            parts=[{"text": json.dumps(result, ensure_ascii=False)}],
            metadata=artifact_metadata,
        )]
        task_store.complete_task(task.id, artifacts=artifacts)
        return task_store.get_task_dict(task.id)

    async def get_task(self, task_id: str) -> dict:
        return self.services.task_store.get_task_dict(task_id)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _dispatch(self, cap: str, text: str, message: dict) -> dict:
        meta = message.get("metadata") or {}
        permission_error = _enforce_ui_design_permission(cap, meta)
        if permission_error:
            return permission_error
        if cap.startswith("figma."):
            return self._dispatch_figma(cap, text, meta)
        if cap.startswith("stitch."):
            return self._dispatch_stitch(cap, text, meta)
        if cap.startswith("design."):
            url = meta.get("designUrl") or text.strip()
            if "figma.com" in url.lower():
                return self._dispatch_figma("figma.file.fetch", url, meta)
            # No clear URL signal — use the configured default provider
            default_provider = self._get_default_provider()
            if default_provider == "figma":
                return self._dispatch_figma("figma.file.fetch", url, meta)
            return self._dispatch_stitch("stitch.screen.fetch", url, meta)
        if "figma.com" in text.lower():
            return self._dispatch_figma("figma.file.fetch", text, meta)
        return {"error": f"Unknown UI Design capability: {cap!r}"}

    # ------------------------------------------------------------------
    # Figma REST
    # ------------------------------------------------------------------

    def _get_default_provider(self) -> str:
        """Return the configured default provider (stitch | figma).

        Resolution order: config loader (boundary.ui_design.default_provider)
        → hardcoded default 'stitch'.
        """
        from framework.config import get_boundary_backend
        return get_boundary_backend("ui_design")

    def _get_figma(self):
        if self._figma_client:
            return self._figma_client
        from agents.ui_design.clients.figma_rest import FigmaClient
        return FigmaClient(token=os.environ.get("FIGMA_TOKEN", ""))

    def _dispatch_figma(self, cap: str, text: str, meta: dict) -> dict:
        client = self._get_figma()
        url = meta.get("figmaUrl") or meta.get("fileUrl") or text.strip()

        if cap in ("figma.page.fetch", "figma.file.fetch", "figma.design.fetch"):
            data, status = client.get_file(url)
            if not data:
                return {"error": f"Figma fetch failed: {status}"}
            pages = [
                {"id": c.get("id"), "name": c.get("name")}
                for c in data.get("document", {}).get("children", [])
            ]
            return {
                "name": data.get("name", ""),
                "lastModified": data.get("lastModified", ""),
                "pages": pages,
                "status": status,
            }

        if cap == "figma.pages.list":
            pages, status = client.list_pages(url)
            return {"pages": pages, "status": status}

        if cap == "figma.node.fetch":
            node_id = meta.get("nodeId") or ""
            data, status = client.get_node(url, node_id)
            return {"node": data, "status": status}

        if cap == "figma.styles.fetch":
            data, status = client.get_file_styles(url)
            return {"styles": data, "status": status}

        data, status = client.get_file(url)
        return {"file": data, "status": status}

    # ------------------------------------------------------------------
    # Google Stitch MCP
    # ------------------------------------------------------------------

    def _get_stitch(self):
        if self._stitch_client:
            return self._stitch_client
        from agents.ui_design.clients.stitch_mcp import StitchMcpClient
        return StitchMcpClient(api_key=os.environ.get("STITCH_API_KEY", ""))

    def _dispatch_stitch(self, cap: str, text: str, meta: dict) -> dict:
        client = self._get_stitch()
        project_id = meta.get("stitchProjectId") or meta.get("projectId") or text.strip()
        screen_id = meta.get("stitchScreenId") or meta.get("screenId") or ""
        screen_name = meta.get("screenName") or ""

        if cap == "stitch.project.get":
            data, status = client.get_project(project_id)
            return {"project": data, "status": status}

        if cap == "stitch.screens.list":
            screens, status = client.list_screens(project_id)
            return {"screens": screens, "status": status}

        if cap == "stitch.screen.fetch":
            if not screen_id and screen_name:
                screen, fs = client.find_screen_by_name(project_id, screen_name)
                if not screen:
                    return {"error": f"Screen not found ({fs})"}
                screen_id = screen["id"]
            data, status = client.get_screen(project_id, screen_id)
            return {"screen": data, "status": status}

        if cap == "stitch.screen.image":
            data, status = client.get_screen_image(project_id, screen_id)
            return {"image": data, "status": status}

        if cap == "stitch.tools.list":
            tools, status = client.list_tools()
            return {"tools": tools, "status": status}

        return {"error": f"Unknown Stitch capability: {cap!r}"}

    def _persist_workspace_outputs(self, cap: str, result: dict, meta: dict, task_id: str) -> dict:
        workspace_path = meta.get("workspacePath") or meta.get("workspace_path") or ""
        if not workspace_path or result.get("error"):
            return result

        try:
            from agents.ui_design.tools import _log, _save_figma_files, _save_stitch_files

            log = _log(str(meta.get("taskId") or meta.get("task_id") or task_id))
            saved: dict = {}
            if cap.startswith("figma."):
                saved = _save_figma_files(result, workspace_path, log)
            elif cap == "stitch.screen.fetch":
                project_id = meta.get("stitchProjectId") or meta.get("projectId") or ""
                screen_id = meta.get("stitchScreenId") or meta.get("screenId") or ""
                if project_id:
                    project_result = self._dispatch_stitch(
                        "stitch.project.get",
                        project_id,
                        {"stitchProjectId": project_id},
                    )
                    saved = _save_stitch_files(
                        result,
                        project_result,
                        project_id,
                        screen_id,
                        workspace_path,
                        log,
                    )
            if saved:
                return {**result, **saved}
        except Exception as exc:  # noqa: BLE001
            return {**result, "workspace_save_error": str(exc)}
        return result
