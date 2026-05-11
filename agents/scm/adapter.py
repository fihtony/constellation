"""SCM Agent adapter — boundary agent for Bitbucket Server REST 1.0.

Dispatches capabilities directly via BitbucketClient (in-process).
Inject a custom ``scm_client`` for testing.
"""
from __future__ import annotations

import json
import os

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode

scm_definition = AgentDefinition(
    agent_id="scm",
    name="SCM Agent",
    description="Boundary adapter: repo inspect, branch list/create, PR operations",
    mode=AgentMode.SINGLE_TURN,
    execution_mode=ExecutionMode.PERSISTENT,
    workflow=None,
    tools=[],
)


class SCMAgentAdapter(BaseAgent):
    """Proxy adapter for Bitbucket Server REST API 1.0.

    Parameters
    ----------
    scm_client:
        Optional pre-constructed BitbucketClient (for testing / DI).
        Falls back to SCM_BASE_URL / SCM_TOKEN / SCM_USERNAME env vars.
    """

    def __init__(
        self,
        definition: AgentDefinition,
        services: AgentServices,
        scm_client=None,
    ):
        super().__init__(definition, services)
        self._scm_client = scm_client

    def _get_client(self):
        if self._scm_client:
            return self._scm_client
        from agents.scm.client import BitbucketClient
        return BitbucketClient(
            base_url=os.environ.get("SCM_BASE_URL", ""),
            token=os.environ.get("SCM_TOKEN", ""),
            username=os.environ.get("SCM_USERNAME", ""),
        )

    async def handle_message(self, message: dict) -> dict:
        from framework.a2a.protocol import Artifact, TaskState, TaskStatus

        task_store = self.services.task_store
        capability = (message.get("metadata") or {}).get("requestedCapability", "")
        parts = message.get("parts") or []
        text = next((p.get("text", "") for p in parts if p.get("text")), "")

        task = task_store.create_task(
            agent_id=self.definition.agent_id,
            metadata={"capability": capability},
        )

        result = self._dispatch(capability, text, message)
        artifacts = [Artifact(
            name="scm-result",
            artifact_type="application/json",
            parts=[{"text": json.dumps(result, ensure_ascii=False)}],
            metadata={"agentId": "scm", "capability": capability, "taskId": task.id},
        )]
        task_store.complete_task(task.id, artifacts=artifacts)
        return task_store.get_task_dict(task.id)

    def _dispatch(self, capability: str, text: str, message: dict) -> dict:
        client = self._get_client()
        meta = message.get("metadata") or {}
        project = meta.get("project") or ""
        repo = meta.get("repo") or ""

        if not project or not repo:
            if "/" in text:
                parts = text.strip().split("/", 1)
                project, repo = parts[0], parts[1]

        if capability in ("scm.repo.inspect", "scm.repo.get"):
            data, status = client.get_repo(project, repo)
            return {"repo": data, "status": status}

        if capability == "scm.branch.list":
            data, status = client.list_branches(project, repo)
            return {"branches": data, "status": status}

        if capability == "scm.branch.create":
            branch_name = meta.get("branchName") or meta.get("branch") or ""
            from_branch = meta.get("fromBranch") or meta.get("fromRef") or "main"
            data, status = client.create_branch(project, repo, branch_name, from_branch)
            return {"branch": data, "status": status}

        if capability == "scm.pr.list":
            data, status = client.list_prs(project, repo)
            return {"prs": data, "status": status}

        if capability == "scm.pr.create":
            title = meta.get("title") or text.strip()
            source = meta.get("sourceBranch") or meta.get("fromBranch") or ""
            target = meta.get("targetBranch") or meta.get("toBranch") or "main"
            description = meta.get("description") or ""
            data, status = client.create_pr(project, repo, title, source, target, description)
            return {"pr": data, "status": status}

        return {"error": f"Unknown SCM capability: {capability!r}"}

    async def get_task(self, task_id: str) -> dict:
        return self.services.task_store.get_task_dict(task_id)
