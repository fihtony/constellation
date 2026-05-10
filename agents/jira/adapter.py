"""Jira Agent adapter — boundary agent proxy for v2 framework.

Supports two dispatch modes:
  direct  (default) — calls JiraClient directly (fast, no HTTP hop, ideal for
                       tests and in-process execution)
  a2a               — forwards via A2AClient to the v1 Jira Agent HTTP service
"""
from __future__ import annotations

import json

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode

jira_definition = AgentDefinition(
    agent_id="jira",
    name="Jira Agent",
    description="Boundary adapter: Jira ticket fetch, search, comment",
    mode=AgentMode.SINGLE_TURN,
    execution_mode=ExecutionMode.PERSISTENT,
    workflow=None,
    tools=[],
)


class JiraAgentAdapter(BaseAgent):
    """Proxy adapter for the Jira boundary service.

    Parameters
    ----------
    existing_agent_url:
        URL of the running v1 Jira Agent HTTP service (a2a mode).
    dispatch_mode:
        ``direct`` — call JiraClient in-process.
        ``a2a``    — forward via A2AClient.
    jira_client:
        Optional pre-constructed JiraClient (direct mode only).
        If None, constructed from JIRA_BASE_URL / JIRA_TOKEN / JIRA_EMAIL.
    """

    def __init__(
        self,
        definition: AgentDefinition,
        services: AgentServices,
        existing_agent_url: str = "http://jira:8010",
        dispatch_mode: str = "direct",
        jira_client=None,
    ):
        super().__init__(definition, services)
        self._existing_agent_url = existing_agent_url
        self._dispatch_mode = dispatch_mode
        self._jira_client = jira_client

    def _get_client(self):
        if self._jira_client:
            return self._jira_client
        import os
        from agents.jira.client import JiraClient
        return JiraClient(
            base_url=os.environ.get("JIRA_BASE_URL", ""),
            token=os.environ.get("JIRA_TOKEN", ""),
            email=os.environ.get("JIRA_EMAIL", ""),
        )

    async def handle_message(self, message: dict) -> dict:
        """Dispatch a Jira task based on the requested capability."""
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
            name="jira-result",
            artifact_type="application/json",
            parts=[{"text": json.dumps(result, ensure_ascii=False)}],
            metadata={"agentId": "jira", "capability": capability, "taskId": task.id},
        )]
        return task.to_dict()

    def _dispatch_direct(self, capability: str, text: str, message: dict) -> dict:
        client = self._get_client()
        meta = message.get("metadata") or {}

        if capability in ("jira.ticket.fetch", "jira.issue.fetch"):
            key = meta.get("ticketKey") or text.strip()
            data, status = client.fetch_ticket(key)
            return {"ticket": data, "status": status}

        if capability == "jira.ticket.search":
            jql = meta.get("jql") or text.strip()
            data, status = client.search(jql)
            return {"issues": data, "status": status}

        if capability in ("jira.comment.add", "jira.ticket.comment"):
            key = meta.get("ticketKey") or ""
            comment = meta.get("comment") or text.strip()
            data, status = client.add_comment(key, comment)
            return {"comment": data, "status": status}

        if capability == "jira.myself":
            data, status = client.get_myself()
            return {"user": data, "status": status}

        if text.strip():
            data, status = client.fetch_ticket(text.strip())
            return {"ticket": data, "status": status}

        return {"error": f"Unknown Jira capability: {capability}"}

    async def _forward_a2a(self, message: dict, task) -> dict:
        from framework.a2a.client import A2AClient
        from framework.a2a.protocol import Artifact, TaskState, TaskStatus
        client = A2AClient()
        try:
            result = await client.dispatch(url=self._existing_agent_url, message=message, wait=True)
            task.status = TaskStatus(state=TaskState.COMPLETED)
            # Re-wrap raw artifact dicts as Artifact objects
            raw = (result.get("task") or result).get("artifacts", [])
            task.artifacts = [
                Artifact(name=a.get("name", ""), artifact_type=a.get("artifactType", "text/plain"),
                         parts=a.get("parts", []), metadata=a.get("metadata", {}))
                for a in raw
            ]
        except Exception as exc:
            task.status = TaskStatus(state=TaskState.FAILED)
            task.artifacts = [Artifact(name="error", artifact_type="text/plain",
                                       parts=[{"text": str(exc)}], metadata={"agentId": "jira"})]
        return task.to_dict()

    async def get_task(self, task_id: str) -> dict:
        return {"task": {"id": task_id, "status": {"state": "TASK_STATE_WORKING"}}}
