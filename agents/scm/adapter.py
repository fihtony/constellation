"""SCM Agent adapter — boundary agent for source control operations.

Supports three backends (selected via SCM_BACKEND env var or auto-detected
from SCM_BASE_URL):
  bitbucket   — Bitbucket Server REST 1.0 (default)
  github-rest — GitHub REST API v3
  github-mcp  — GitHub MCP (falls back to github-rest in v2)

Dispatches capabilities directly via the appropriate client (in-process).
Inject a custom ``scm_client`` for testing.
"""
from __future__ import annotations

import json
import os
import subprocess

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode

scm_definition = AgentDefinition(
    agent_id="scm",
    name="SCM Agent",
    description="Boundary adapter: repo inspect, branch list/create, PR operations (Bitbucket/GitHub)",
    mode=AgentMode.SINGLE_TURN,
    execution_mode=ExecutionMode.PERSISTENT,
    workflow=None,
    tools=[],
)


class SCMAgentAdapter(BaseAgent):
    """Proxy adapter for SCM backends (Bitbucket Server REST, GitHub REST, GitHub MCP).

    Parameters
    ----------
    scm_client:
        Optional pre-constructed client (for testing / DI).
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
        from agents.scm.client import create_scm_client
        return create_scm_client(
            base_url=os.environ.get("SCM_BASE_URL", ""),
            token=os.environ.get("SCM_TOKEN", ""),
            username=os.environ.get("SCM_USERNAME", ""),
            default_project=os.environ.get("SCM_DEFAULT_PROJECT", ""),
            ca_bundle=os.environ.get("SCM_CA_BUNDLE", ""),
        )

    async def handle_message(self, message: dict) -> dict:
        from framework.a2a.protocol import Artifact, TaskState, TaskStatus

        task_store = self.services.task_store
        msg = message.get("message", message)
        capability = (msg.get("metadata") or {}).get("requestedCapability", "")
        parts = msg.get("parts") or []
        text = next((p.get("text", "") for p in parts if p.get("text")), "")

        task = task_store.create_task(
            agent_id=self.definition.agent_id,
            metadata={"capability": capability},
        )

        result = self._dispatch(capability, text, msg)
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
            # Args order: (project/owner, repo, from_branch, to_branch, title, description)
            data, status = client.create_pr(project, repo, source, target, title, description)
            pr_url = ""
            if isinstance(data, dict):
                # Bitbucket: links.self[0].href  |  GitHub: links.self[0].href (normalised)
                links = data.get("links", {}).get("self", [])
                if links:
                    pr_url = links[0].get("href", "")
            return {"pr": data, "status": status, "prUrl": pr_url}

        if capability == "scm.pr.get":
            pr_id = meta.get("prId") or meta.get("prNumber") or text.strip()
            data, status = client.get_pr(project, repo, pr_id)
            return {"pr": data, "status": status}

        if capability == "scm.pr.comment":
            pr_id = meta.get("prId") or meta.get("prNumber") or ""
            comment_text = meta.get("comment") or text.strip()
            data, status = client.add_pr_comment(project, repo, pr_id, comment_text)
            return {"comment": data, "status": status}

        if capability == "scm.repo.clone":
            return self._handle_clone(meta)

        if capability == "scm.branch.push":
            return self._handle_push(meta)

        return {"error": f"Unknown SCM capability: {capability!r}"}

    # ------------------------------------------------------------------
    # Git subprocess operations
    # ------------------------------------------------------------------

    def _build_auth_url(self, repo_url: str) -> str:
        """Build an authenticated clone URL with token embedded."""
        from urllib.parse import urlparse, urlunparse

        token = os.environ.get("SCM_TOKEN", "")
        parsed = urlparse(repo_url)
        # For Bitbucket bearer: embed token in URL
        # For GitHub: embed token as username
        if "github" in parsed.netloc.lower():
            authed = parsed._replace(netloc=f"{token}@{parsed.netloc}")
        else:
            # Bitbucket: use HTTP header via git config or embed in URL
            username = os.environ.get("SCM_USERNAME", "")
            if username:
                authed = parsed._replace(netloc=f"{username}:{token}@{parsed.netloc}")
            else:
                authed = parsed._replace(netloc=f"x-token-auth:{token}@{parsed.netloc}")
        return urlunparse(authed)

    def _handle_clone(self, meta: dict) -> dict:
        """Clone a repository to a target path."""
        repo_url = meta.get("repoUrl", "")
        target_path = meta.get("targetPath", "")
        if not repo_url or not target_path:
            return {"error": "repoUrl and targetPath are required", "status": "error"}

        # Strip /browse suffix from Bitbucket URLs
        clean_url = repo_url.split("/browse")[0].rstrip("/")

        # Build authenticated clone URL
        auth_url = self._build_auth_url(clean_url)

        try:
            os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
            result = subprocess.run(
                ["git", "clone", "--depth", "1", auth_url, target_path],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            if result.returncode != 0:
                return {
                    "cloned": False,
                    "error": result.stderr.strip()[:500],
                    "status": "clone_failed",
                }
            return {"cloned": True, "path": target_path, "status": "ok"}
        except subprocess.TimeoutExpired:
            return {"cloned": False, "error": "clone timed out", "status": "timeout"}
        except Exception as exc:
            return {"cloned": False, "error": str(exc)[:500], "status": "error"}

    def _handle_push(self, meta: dict) -> dict:
        """Push a local branch to the remote."""
        repo_path = meta.get("repoPath", "")
        branch = meta.get("branch", "")
        if not repo_path or not branch:
            return {"error": "repoPath and branch are required", "status": "error"}

        try:
            # Set the push URL with auth
            remote_url = subprocess.run(
                ["git", "-C", repo_path, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()
            if remote_url:
                auth_url = self._build_auth_url(remote_url)
                subprocess.run(
                    ["git", "-C", repo_path, "remote", "set-url", "origin", auth_url],
                    capture_output=True, text=True, timeout=10,
                    env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                )

            result = subprocess.run(
                ["git", "-C", repo_path, "push", "-u", "origin", branch],
                capture_output=True, text=True, timeout=120,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            if result.returncode != 0:
                return {
                    "pushed": False,
                    "error": result.stderr.strip()[:500],
                    "status": "push_failed",
                }
            return {"pushed": True, "branch": branch, "status": "ok"}
        except subprocess.TimeoutExpired:
            return {"pushed": False, "error": "push timed out", "status": "timeout"}
        except Exception as exc:
            return {"pushed": False, "error": str(exc)[:500], "status": "error"}

    async def get_task(self, task_id: str) -> dict:
        return self.services.task_store.get_task_dict(task_id)
