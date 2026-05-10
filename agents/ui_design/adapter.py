"""UI Design Agent adapter — boundary agent proxy for v2 framework.

Supports two dispatch modes:
  direct  (default) — calls FigmaClient directly (fast, in-process)
  a2a               — forwards via A2AClient to the v1 UI Design Agent HTTP service
"""
from __future__ import annotations

import json

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode

ui_design_definition = AgentDefinition(
    agent_id="ui-design",
    name="UI Design Agent",
    description="Boundary adapter: Figma design context fetch (REST)",
    mode=AgentMode.SINGLE_TURN,
    execution_mode=ExecutionMode.PERSISTENT,
    workflow=None,
    tools=[],
)


class UIDesignAgentAdapter(BaseAgent):
    """Proxy adapter for the UI Design boundary service.

    Parameters
    ----------
    existing_agent_url:
        URL of the running v1 UI Design Agent (a2a mode).
    dispatch_mode:
        ``direct`` — call FigmaClient in-process.
        ``a2a``    — forward via A2AClient.
    figma_client:
        Optional pre-constructed FigmaClient (direct mode only).
        If None, constructed from FIGMA_TOKEN env var.
    """

    def __init__(
        self,
        definition: AgentDefinition,
        services: AgentServices,
        existing_agent_url: str = "http://ui-design:8040",
        dispatch_mode: str = "direct",
        figma_client=None,
    ):
        super().__init__(definition, services)
        self._existing_agent_url = existing_agent_url
        self._dispatch_mode = dispatch_mode
        self._figma_client = figma_client

    def _get_client(self):
        if self._figma_client:
            return self._figma_client
        import os
        from agents.ui_design.client import FigmaClient
        return FigmaClient(token=os.environ.get("FIGMA_TOKEN", ""))

    async def handle_message(self, message: dict) -> dict:
        from framework.a2a.protocol import Artifact, Task, TaskState, TaskStatus

        task = Task()
        capability = (message.get("metadata") or {}).get("requestedCapability", "")
        parts = message.get("parts") or []
        text = next((p.get("text", "") for p in parts if p.get("text")), "")

        if self._dispatch_mode == "a2a":
            return await self._forward_a2a(message, task)

        result = self._dispatch_direct(capability, text, message)
        task.status = TaskStatus(state=TaskState.COMPLETED)
        task.artifacts = [Artifact(
            name="design-result",
            artifact_type="application/json",
            parts=[{"text": json.dumps(result, ensure_ascii=False)}],
            metadata={"agentId": "ui-design", "capability": capability, "taskId": task.id},
        )]
        return task.to_dict()

    def _dispatch_direct(self, capability: str, text: str, message: dict) -> dict:
        client = self._get_client()
        meta = message.get("metadata") or {}
        file_url = meta.get("figmaUrl") or meta.get("fileUrl") or text.strip()

        if capability in ("figma.page.fetch", "figma.file.fetch", "figma.design.fetch"):
            data, status = client.get_file(file_url)
            if not data:
                return {"error": f"Figma fetch failed: {status}"}
            # Return lightweight summary
            doc = data.get("document", {})
            pages = [
                {"id": c.get("id"), "name": c.get("name")}
                for c in doc.get("children", [])
            ]
            return {
                "name": data.get("name", ""),
                "lastModified": data.get("lastModified", ""),
                "pages": pages,
                "status": status,
            }

        if capability == "figma.pages.list":
            pages, status = client.list_pages(file_url)
            return {"pages": pages, "status": status}

        if capability == "figma.node.fetch":
            node_id = meta.get("nodeId") or ""
            data, status = client.get_node(file_url, node_id)
            return {"node": data, "status": status}

        # Default: fetch file
        if file_url:
            data, status = client.get_file(file_url)
            return {"file": data, "status": status}

        return {"error": f"Unknown UI Design capability: {capability}"}

    async def _forward_a2a(self, message: dict, task) -> dict:
        from framework.a2a.client import A2AClient
        from framework.a2a.protocol import Artifact, TaskState, TaskStatus
        client = A2AClient()
        try:
            result = await client.dispatch(url=self._existing_agent_url, message=message, wait=True)
            task.status = TaskStatus(state=TaskState.COMPLETED)
            raw = (result.get("task") or result).get("artifacts", [])
            task.artifacts = [
                Artifact(name=a.get("name", ""), artifact_type=a.get("artifactType", "text/plain"),
                         parts=a.get("parts", []), metadata=a.get("metadata", {}))
                for a in raw
            ]
        except Exception as exc:
            task.status = TaskStatus(state=TaskState.FAILED)
            task.artifacts = [Artifact(name="error", artifact_type="text/plain",
                                       parts=[{"text": str(exc)}], metadata={"agentId": "ui-design"})]
        return task.to_dict()

    async def get_task(self, task_id: str) -> dict:
        return {"task": {"id": task_id, "status": {"state": "TASK_STATE_WORKING"}}}
