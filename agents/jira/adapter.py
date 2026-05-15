"""Jira Agent adapter — boundary agent for Jira Cloud.

Supports two backends:
  - ``rest`` (default) — direct Jira Cloud REST API v3 calls.
  - ``mcp``  — Atlassian Rovo MCP server with REST fallback.

Backend selection via ``JIRA_BACKEND`` env var or ``jira_backend`` constructor arg.
Inject a custom provider for testing via ``jira_provider``.
"""
from __future__ import annotations

import json
import os

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode

jira_definition = AgentDefinition(
    agent_id="jira",
    name="Jira Agent",
    description="Boundary adapter: Jira ticket fetch, search, comment (REST + MCP)",
    mode=AgentMode.SINGLE_TURN,
    execution_mode=ExecutionMode.PERSISTENT,
    workflow=None,
    tools=[],
)


def _make_provider(backend: str = "rest", **kwargs):
    """Create a JiraProvider instance for the given backend.

    Parameters
    ----------
    backend:
        ``rest`` (default) or ``mcp``.
    **kwargs:
        Credential / config overrides; falls back to env vars.
    """
    base_url = kwargs.get("base_url") or os.environ.get("JIRA_BASE_URL", "")
    token = kwargs.get("token") or os.environ.get("JIRA_TOKEN", "")
    email = kwargs.get("email") or os.environ.get("JIRA_EMAIL", "")
    auth_mode = kwargs.get("auth_mode") or os.environ.get("JIRA_AUTH_MODE", "basic")
    ca_bundle = kwargs.get("corp_ca_bundle") or os.environ.get("JIRA_CA_BUNDLE", "")

    if backend == "mcp":
        from agents.jira.providers.mcp import JiraMCPProvider
        return JiraMCPProvider(
            base_url=base_url,
            token=token,
            email=email,
            auth_mode=auth_mode,
            cloud_id=kwargs.get("cloud_id") or os.environ.get("JIRA_CLOUD_ID", ""),
            corp_ca_bundle=ca_bundle,
            mcp_url=kwargs.get("mcp_url") or os.environ.get(
                "JIRA_MCP_URL", "https://mcp.atlassian.com/v1/mcp"
            ),
        )

    from agents.jira.providers.rest import JiraRESTProvider
    return JiraRESTProvider(
        base_url=base_url,
        token=token,
        email=email,
        auth_mode=auth_mode,
        corp_ca_bundle=ca_bundle,
    )


class JiraAgentAdapter(BaseAgent):
    """Proxy adapter for Jira Cloud (REST or MCP backend).

    Parameters
    ----------
    jira_provider:
        Optional pre-constructed JiraProvider (for testing / DI).
    jira_backend:
        ``rest`` or ``mcp``.  Falls back to ``JIRA_BACKEND`` env var.
    """

    def __init__(
        self,
        definition: AgentDefinition,
        services: AgentServices,
        jira_provider=None,
        jira_backend: str | None = None,
    ):
        super().__init__(definition, services)
        self._provider = jira_provider
        self._backend = jira_backend or os.environ.get("JIRA_BACKEND", "rest")

    def _get_provider(self):
        if self._provider:
            return self._provider
        self._provider = _make_provider(self._backend)
        return self._provider

    async def handle_message(self, message: dict) -> dict:
        from framework.a2a.protocol import Artifact, TaskState, TaskStatus

        task_store = self.services.task_store
        msg = message.get("message", message)
        capability = (msg.get("metadata") or {}).get("requestedCapability", "")
        parts = msg.get("parts") or []
        text = next((p.get("text", "") for p in parts if p.get("text")), "")
        meta = msg.get("metadata") or {}

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
        provider = self._get_provider()

        if capability in ("jira.ticket.fetch", "jira.ticket.get"):
            key = meta.get("ticketKey") or text.strip()
            data, status = provider.fetch_issue(key)
            return {"ticket": data, "status": status}

        if capability == "jira.ticket.search":
            jql = meta.get("jql") or text.strip()
            data, status = provider.search_issues(jql)
            return {"issues": data, "status": status}

        if capability in ("jira.comment.add", "jira.ticket.comment"):
            key = meta.get("ticketKey") or ""
            comment = meta.get("comment") or text.strip()
            data, status = provider.add_comment(key, comment)
            return {"comment": data, "status": status}

        if capability == "jira.transitions.list":
            key = meta.get("ticketKey") or text.strip()
            data, status = provider.get_transitions(key)
            return {"transitions": data, "status": status}

        if capability == "jira.ticket.update":
            key = meta.get("ticketKey") or text.strip()
            fields = meta.get("fields") or {}
            data, status = provider.update_issue_fields(key, fields)
            return {"result": data, "status": status}

        if capability == "jira.ticket.transition":
            key = meta.get("ticketKey") or ""
            transition_name = meta.get("transitionName") or text.strip()
            data, status = provider.transition_issue(key, transition_name)
            return {"transitionId": data, "status": status}

        if capability == "jira.user.me":
            data, status = provider.get_myself()
            return {"user": data, "status": status}

        if capability == "jira.comment.list":
            key = meta.get("ticketKey") or text.strip()
            data, status = provider.list_comments(key)
            return {"comments": data, "status": status}

        return {"error": f"Unknown Jira capability: {capability!r}"}

    async def get_task(self, task_id: str) -> dict:
        return self.services.task_store.get_task_dict(task_id)
