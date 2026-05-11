"""Jira Agent adapter — boundary agent for Jira Cloud REST API v3.

Dispatches capabilities directly via JiraClient (in-process).
Inject a custom ``jira_client`` for testing.
"""
from __future__ import annotations

import json
import os

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
    """Proxy adapter for Jira Cloud REST API v3.

    Parameters
    ----------
    jira_client:
        Optional pre-constructed JiraClient (for testing / DI).
        Falls back to JIRA_BASE_URL / JIRA_TOKEN / JIRA_EMAIL env vars.
    """

    def __init__(
        self,
        definition: AgentDefinition,
        services: AgentServices,
        jira_client=None,
    ):
        super().__init__(definition, services)
        self._jira_client = jira_client

    def _get_client(self):
        if self._jira_client:
            return self._jira_client
        from agents.jira.client import JiraClient
        return JiraClient(
            base_url=os.environ.get("JIRA_BASE_URL", ""),
            token=os.environ.get("JIRA_TOKEN", ""),
            email=os.environ.get("JIRA_EMAIL", ""),
        )

    async def handle_message(self, message: dict) -> dict:
        from framework.a2a.protocol import Artifact, TaskState, TaskStatus

        task_store = self.services.task_store
        capability = (message.get("metadata") or {}).get("requestedCapability", "")
        parts = message.get("parts") or []
        text = next((p.get("text", "") for p in parts if p.get("text")), "")
        meta = message.get("metadata") or {}

        task = task_store.create_task(
            agent_id=self.definition.agent_id,
            metadata={"capability": capability},
        )

        result = self._dispatch(capability, text, meta)
        artifacts = [Artifact(
            name="jira-result",
            artifact_type="application/json",
            parts=[{"text": json.dumps(result, ensure_ascii=False)}],
            metadata={"agentId": "jira", "capability": capability, "taskId": task.id},
        )]
        task_store.complete_task(task.id, artifacts=artifacts)
        return task_store.get_task_dict(task.id)

    def _dispatch(self, capability: str, text: str, meta: dict) -> dict:
        client = self._get_client()

        if capability in ("jira.ticket.fetch", "jira.ticket.get"):
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

        if capability == "jira.transitions.list":
            key = meta.get("ticketKey") or text.strip()
            data, status = client.get_transitions(key)
            return {"transitions": data, "status": status}

        return {"error": f"Unknown Jira capability: {capability!r}"}

    async def get_task(self, task_id: str) -> dict:
        return self.services.task_store.get_task_dict(task_id)
