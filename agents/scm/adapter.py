"""SCM Agent adapter — boundary agent proxy for v2 framework.

Supports two dispatch modes:
  direct  (default) — calls BitbucketClient directly (fast, in-process)
  a2a               — forwards via A2AClient to the v1 SCM Agent HTTP service
"""
from __future__ import annotations

import json

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode

scm_definition = AgentDefinition(
    agent_id="scm",
    name="SCM Agent",
    description="Boundary adapter: repo inspect, branch list, PR operations",
    mode=AgentMode.SINGLE_TURN,
    execution_mode=ExecutionMode.PERSISTENT,
    workflow=None,
    tools=[],
)


class SCMAgentAdapter(BaseAgent):
    """Proxy adapter for the SCM boundary service.

    Parameters
    ----------
    existing_agent_url:
        URL of the running v1 SCM Agent (a2a mode).
    dispatch_mode:
        ``direct`` — call BitbucketClient in-process.
        ``a2a``    — forward via A2AClient.
    scm_client:
        Optional pre-constructed BitbucketClient (direct mode only).
        If None, constructed from SCM_BASE_URL / SCM_TOKEN / SCM_USERNAME env vars.
    """

    def __init__(
        self,
        definition: AgentDefinition,
        services: AgentServices,
        existing_agent_url: str = "http://scm:8020",
        dispatch_mode: str = "direct",
        scm_client=None,
    ):
        super().__init__(definition, services)
        self._existing_agent_url = existing_agent_url
        self._dispatch_mode = dispatch_mode
        self._scm_client = scm_client

    def _get_client(self):
        if self._scm_client:
            return self._scm_client
        import os
        from agents.scm.client import BitbucketClient
        return BitbucketClient(
            base_url=os.environ.get("SCM_BASE_URL", ""),
            token=os.environ.get("SCM_TOKEN", ""),
            username=os.environ.get("SCM_USERNAME", ""),
        )

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
            name="scm-result",
            artifact_type="application/json",
            parts=[{"text": json.dumps(result, ensure_ascii=False)}],
            metadata={"agentId": "scm", "capability": capability, "taskId": task.id},
        )]
        return task.to_dict()

    def _dispatch_direct(self, capability: str, text: str, message: dict) -> dict:
        client = self._get_client()
        meta = message.get("metadata") or {}
        project = meta.get("project", "")
        repo = meta.get("repo", "")

        if capability in ("scm.repo.inspect", "scm.repo.get"):
            data, status = client.get_repo(project, repo)
            return {"repo": data, "status": status}

        if capability == "scm.branch.list":
            data, status = client.list_branches(project, repo)
            return {"branches": data, "status": status}

        if capability == "scm.branch.create":
            branch = meta.get("branch", "")
            from_ref = meta.get("fromRef", "main")
            data, status = client.create_branch(project, repo, branch, from_ref)
            return {"branch": data, "status": status}

        if capability == "scm.pr.list":
            data, status = client.list_prs(project, repo)
            return {"prs": data, "status": status}

        if capability == "scm.pr.create":
            data, status = client.create_pr(
                project, repo,
                from_branch=meta.get("fromBranch", ""),
                to_branch=meta.get("toBranch", "main"),
                title=meta.get("title", ""),
                description=meta.get("description", ""),
            )
            return {"pr": data, "status": status}

        return {"error": f"Unknown SCM capability: {capability}"}

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
                                       parts=[{"text": str(exc)}], metadata={"agentId": "scm"})]
        return task.to_dict()

    async def get_task(self, task_id: str) -> dict:
        return {"task": {"id": task_id, "status": {"state": "TASK_STATE_WORKING"}}}
