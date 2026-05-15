"""SCM Agent adapter — boundary agent for source control operations.

Supports three backends (selected via SCM_BACKEND env var or auto-detected
from SCM_BASE_URL):
  bitbucket   — Bitbucket Server REST 1.0 (default for Bitbucket hosts)
  github-rest — GitHub REST API v3
  github-mcp  — GitHub MCP (default for GitHub hosts)

Dispatches capabilities directly via the appropriate client (in-process).
Inject a custom ``scm_client`` for testing.
"""
from __future__ import annotations

import json
import os
import subprocess

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode
from framework.env_utils import build_isolated_git_env

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

    async def start(self) -> None:
        await super().start()
        from agents.scm.tools import register_scm_tools
        register_scm_tools()

    def _get_client(self):
        if self._scm_client:
            return self._scm_client
        from agents.scm.client import create_scm_client
        base_url = os.environ.get("SCM_BASE_URL", "")
        backend = os.environ.get("SCM_BACKEND", "")
        # Auto-select github-mcp for GitHub URLs when no explicit backend is set
        if not backend and "github" in base_url.lower():
            backend = "github-mcp"
        return create_scm_client(
            base_url=base_url,
            token=os.environ.get("SCM_TOKEN", ""),
            username=os.environ.get("SCM_USERNAME", ""),
            default_project=os.environ.get("SCM_DEFAULT_PROJECT", ""),
            ca_bundle=os.environ.get("SCM_CA_BUNDLE", ""),
            backend=backend,
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

    def _build_auth_header(self, repo_url: str) -> str:
        """Build a git http.extraHeader value for credentials.

        Returns an ``Authorization: ...`` header string for use with
        ``git -c http.extraHeader=<value>``.  Credentials are NOT
        embedded in the remote URL.

        Auth strategy:
          GitHub: Bearer token (PAT or OAuth)
          Bitbucket Server + username: Basic base64(username:token)
          Bitbucket Server + no username: Bearer token (PAT, Bitbucket Server 6.4+)
        """
        import base64
        from urllib.parse import urlparse

        token = os.environ.get("SCM_TOKEN", "")
        netloc = urlparse(repo_url).netloc.lower()
        username = os.environ.get("SCM_USERNAME", "")

        if "github" in netloc:
            # GitHub git-over-HTTPS: Basic auth with x-access-token as username.
            # This works for both classic PATs and fine-grained PATs.
            # Bearer/token scheme only works for GitHub Apps, not PATs in git ops.
            basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            return f"Authorization: Basic {basic}"

        # Bitbucket Server / Data Center
        if username:
            # username:PAT Basic auth
            creds = base64.b64encode(f"{username}:{token}".encode()).decode()
            return f"Authorization: Basic {creds}"

        # PAT-only Bearer auth (Bitbucket Server 6.4+ / Data Center)
        return f"Authorization: Bearer {token}"

    def _to_git_clone_url(self, repo_url: str) -> str:
        """Convert a Bitbucket browser URL to a valid git clone URL.

        Bitbucket Server:
          Browser URL:  https://host/users/userabc/repos/web-ui-test
          Clone URL:    https://host/scm/~userabc/web-ui-test.git
          Or:           https://host/projects/PROJ/repos/my-repo
          Clone URL:    https://host/scm/proj/my-repo.git

        GitHub:
          Browser URL already valid for git — just ensure no /browse suffix.
        """
        from urllib.parse import urlparse
        parsed = urlparse(repo_url)
        netloc = parsed.netloc.lower()

        # GitHub: clone URL equals browser URL (with .git optional)
        if "github" in netloc:
            return repo_url

        # Bitbucket Server: derive /scm/<project>/<repo>.git path
        path_parts = [p for p in parsed.path.split("/") if p]

        try:
            # /users/<username>/repos/<slug>
            user_idx = path_parts.index("users")
            username = path_parts[user_idx + 1]
            repos_idx = path_parts.index("repos")
            slug = path_parts[repos_idx + 1]
            project = f"~{username}"
            return f"{parsed.scheme}://{parsed.netloc}/scm/{project.lower()}/{slug}.git"
        except (ValueError, IndexError):
            pass

        try:
            # /projects/<KEY>/repos/<slug>
            proj_idx = path_parts.index("projects")
            project = path_parts[proj_idx + 1]
            repos_idx = path_parts.index("repos")
            slug = path_parts[repos_idx + 1]
            return f"{parsed.scheme}://{parsed.netloc}/scm/{project.lower()}/{slug}.git"
        except (ValueError, IndexError):
            pass

        # Fallback: use URL as-is (may not work but avoids silent wrong URL)
        return repo_url

    def _handle_clone(self, meta: dict) -> dict:
        """Clone a repository to a target path."""
        repo_url = meta.get("repoUrl", "")
        target_path = meta.get("targetPath", "")
        if not repo_url or not target_path:
            return {"error": "repoUrl and targetPath are required", "status": "error"}

        # Strip /browse suffix then convert browser URL → git clone URL
        clean_url = repo_url.split("/browse")[0].rstrip("/")
        git_url = self._to_git_clone_url(clean_url)

        # Auth via http.extraHeader — credentials are NEVER in the remote URL
        auth_header = self._build_auth_header(git_url)
        git_cfg = ["-c", f"http.extraHeader={auth_header}"]
        ca_bundle = os.environ.get("SCM_CA_BUNDLE", "")
        if ca_bundle and os.path.isfile(ca_bundle):
            git_cfg += ["-c", f"http.sslCAInfo={ca_bundle}"]

        # build_isolated_git_env sets isolated HOME, GIT_CONFIG_GLOBAL, and
        # GIT_CONFIG_NOSYSTEM=1 so macOS Keychain / host ~/.gitconfig are never used.
        git_env = build_isolated_git_env(scope="scm-clone")

        print(f"[scm] Cloning {git_url} → {target_path}")
        # Reuse existing clone rather than fail when the directory is already present
        if os.path.isdir(target_path) and os.path.isdir(os.path.join(target_path, ".git")):
            print(f"[scm] Target already a git repo, reusing existing clone: {target_path}")
            return {"cloned": True, "path": target_path, "status": "ok", "reused": True}
        # Remove stale empty (or broken) target dir so git clone does not fail
        if os.path.isdir(target_path):
            import shutil as _shutil
            _shutil.rmtree(target_path, ignore_errors=True)
            print(f"[scm] Removed stale target directory: {target_path}")
        try:
            os.makedirs(os.path.dirname(target_path) or ".", exist_ok=True)
            result = subprocess.run(
                ["git", *git_cfg, "clone", "--depth", "1", git_url, target_path],
                capture_output=True, text=True, timeout=120,
                env=git_env,
            )
            if result.returncode != 0:
                # stderr from git will not contain credentials (they are in -c, not in URL)
                stderr_safe = result.stderr.strip()[:400]
                return {
                    "cloned": False,
                    "error": "Clone failed — check SCM_TOKEN and SCM_USERNAME.",
                    "detail": stderr_safe,
                    "status": "clone_failed",
                }
            return {"cloned": True, "path": target_path, "status": "ok"}
        except subprocess.TimeoutExpired:
            return {"cloned": False, "error": "clone timed out", "status": "timeout"}
        except Exception as exc:
            return {"cloned": False, "error": str(exc)[:200], "status": "error"}

    def _handle_push(self, meta: dict) -> dict:
        """Push a local branch to the remote."""
        repo_path = meta.get("repoPath", "")
        branch = meta.get("branch", "")
        if not repo_path or not branch:
            return {"error": "repoPath and branch are required", "status": "error"}

        try:
            remote_url = subprocess.run(
                ["git", "-C", repo_path, "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=10,
            ).stdout.strip()

            # Auth via http.extraHeader — credentials are NEVER in the remote URL
            auth_header = self._build_auth_header(remote_url) if remote_url else ""
            cmd = ["git", "-C", repo_path]
            if auth_header:
                cmd += ["-c", f"http.extraHeader={auth_header}"]
            ca_bundle = os.environ.get("SCM_CA_BUNDLE", "")
            if ca_bundle and os.path.isfile(ca_bundle):
                cmd += ["-c", f"http.sslCAInfo={ca_bundle}"]
            cmd += ["push", "-u", "origin", branch]

            # build_isolated_git_env sets isolated HOME, GIT_CONFIG_GLOBAL, and
            # GIT_CONFIG_NOSYSTEM=1 so macOS Keychain / host ~/.gitconfig are never used.
            git_env = build_isolated_git_env(scope="scm-push")

            print(f"[scm] Pushing branch {branch} in {repo_path}")
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
                env=git_env,
            )
            if result.returncode != 0:
                stderr_safe = result.stderr.strip()[:400]
                return {
                    "pushed": False,
                    "error": "Push failed — check SCM_TOKEN and SCM_USERNAME.",
                    "detail": stderr_safe,
                    "status": "push_failed",
                }
            return {"pushed": True, "branch": branch, "status": "ok"}
        except subprocess.TimeoutExpired:
            return {"pushed": False, "error": "push timed out", "status": "timeout"}
        except Exception as exc:
            return {"pushed": False, "error": str(exc)[:200], "status": "error"}

    async def get_task(self, task_id: str) -> dict:
        return self.services.task_store.get_task_dict(task_id)
