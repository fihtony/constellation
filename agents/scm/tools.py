"""SCM boundary tools — in-process implementations using SCMAgentAdapter.

Registered by SCMAgentAdapter.start() so the global ToolRegistry has live
SCM tools before Team Lead calls register_team_lead_tools().
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse

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
    from agents.scm.adapter import SCMAgentAdapter, scm_definition
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
    return SCMAgentAdapter(definition=scm_definition, services=services)


def _parse_repo_coordinates(repo_url: str) -> tuple[str, str]:
    """Extract (project/owner, repo) from a GitHub or Bitbucket URL."""
    parsed = urlparse(repo_url)
    path_parts = [p for p in parsed.path.strip("/").split("/") if p]
    if len(path_parts) >= 2:
        return path_parts[0], path_parts[1].rstrip(".git")
    return "", ""


class CloneRepo(BaseTool):
    name = "clone_repo"
    description = "Clone a Git repository to a local workspace path."
    parameters_schema = {
        "type": "object",
        "properties": {
            "repo_url": {"type": "string", "description": "Git repository URL to clone."},
            "target_path": {"type": "string", "description": "Local filesystem path to clone into."},
            "task_id": {"type": "string", "description": "Caller task ID for log correlation (optional)."},
        },
        "required": ["repo_url", "target_path"],
    }

    def execute_sync(self, repo_url: str = "", target_path: str = "", task_id: str = "") -> ToolResult:
        log = _log(task_id)
        log.info("clone_repo called", repo_url=repo_url, target_path=target_path)
        adapter = _get_adapter()
        result = adapter._dispatch(
            "scm.repo.clone", "",
            {"metadata": {"repoUrl": repo_url, "targetPath": target_path}},
        )
        if result.get("error"):
            log.error("clone_repo failed", error=result["error"], repo_url=repo_url)
        else:
            log.info("clone_repo ok", local_path=target_path, repo_url=repo_url)
            print(f"[{_AGENT_ID}] Repo cloned: {repo_url} → {target_path}")
            result["localPath"] = target_path
        return ToolResult(output=json.dumps(result))


class SCMListBranches(BaseTool):
    name = "scm_list_branches"
    description = "List remote branches in a repository."
    parameters_schema = {
        "type": "object",
        "properties": {
            "repo_url": {"type": "string"},
            "task_id": {"type": "string"},
        },
        "required": ["repo_url"],
    }

    def execute_sync(self, repo_url: str = "", task_id: str = "") -> ToolResult:
        log = _log(task_id)
        log.debug("scm_list_branches called", repo_url=repo_url)
        adapter = _get_adapter()
        project, repo = _parse_repo_coordinates(repo_url)
        result = adapter._dispatch(
            "scm.branch.list", f"{project}/{repo}",
            {"metadata": {"project": project, "repo": repo}},
        )
        log.debug("scm_list_branches result", count=len(result.get("branches", [])))
        return ToolResult(output=json.dumps(result))


class SCMPush(BaseTool):
    name = "scm_push"
    description = "Push a local branch to the remote origin."
    parameters_schema = {
        "type": "object",
        "properties": {
            "repo_path": {"type": "string", "description": "Local repo directory."},
            "branch": {"type": "string", "description": "Branch name to push."},
            "task_id": {"type": "string"},
        },
        "required": ["repo_path", "branch"],
    }

    def execute_sync(self, repo_path: str = "", branch: str = "", task_id: str = "") -> ToolResult:
        log = _log(task_id)
        log.info("scm_push called", repo_path=repo_path, branch=branch)
        adapter = _get_adapter()
        result = adapter._dispatch(
            "scm.branch.push", "",
            {"metadata": {"repoPath": repo_path, "branch": branch}},
        )
        if result.get("error"):
            log.error("scm_push failed", error=result["error"])
        else:
            log.info("scm_push ok", branch=branch)
        return ToolResult(output=json.dumps(result))


class SCMCreatePR(BaseTool):
    name = "scm_create_pr"
    description = "Create a pull request in the remote repository."
    parameters_schema = {
        "type": "object",
        "properties": {
            "repo_url": {"type": "string"},
            "source_branch": {"type": "string"},
            "target_branch": {"type": "string"},
            "title": {"type": "string"},
            "description": {"type": "string"},
            "task_id": {"type": "string"},
        },
        "required": ["repo_url", "source_branch", "title", "description"],
    }

    def execute_sync(
        self,
        repo_url: str = "",
        source_branch: str = "",
        target_branch: str = "main",
        title: str = "",
        description: str = "",
        task_id: str = "",
    ) -> ToolResult:
        log = _log(task_id)
        log.info("scm_create_pr called",
                 repo_url=repo_url, source_branch=source_branch, target_branch=target_branch,
                 title=title[:80])
        adapter = _get_adapter()
        project, repo = _parse_repo_coordinates(repo_url)
        result = adapter._dispatch(
            "scm.pr.create", title,
            {
                "metadata": {
                    "project": project,
                    "repo": repo,
                    "sourceBranch": source_branch,
                    "targetBranch": target_branch,
                    "title": title,
                    "description": description,
                }
            },
        )
        pr_url = result.get("prUrl", result.get("url", ""))
        # Extract the PR number from the nested pr.id field so callers
        # can access prNumber directly without navigating the pr dict.
        pr_data = result.get("pr", {})
        pr_number = 0
        if isinstance(pr_data, dict):
            pr_number = pr_data.get("id") or pr_data.get("number") or 0
        if not isinstance(pr_number, int):
            try:
                pr_number = int(pr_number)
            except (TypeError, ValueError):
                pr_number = 0
        if result.get("error"):
            log.error("scm_create_pr failed", error=result["error"])
        else:
            log.info("scm_create_pr ok", pr_url=pr_url, pr_number=pr_number)
        # Merge prNumber into the top-level result so web_dev/nodes.py can read it
        result["prNumber"] = pr_number
        return ToolResult(output=json.dumps(result))


class SCMAddPRComment(BaseTool):
    """Post a Markdown comment to an existing pull request."""

    name = "scm_add_pr_comment"
    description = "Post a Markdown comment to an existing pull request."
    parameters_schema = {
        "type": "object",
        "properties": {
            "repo_url": {"type": "string", "description": "Full GitHub repo URL."},
            "pr_number": {"type": "integer", "description": "Pull request number."},
            "comment": {"type": "string", "description": "Markdown comment body."},
            "task_id": {"type": "string"},
        },
        "required": ["repo_url", "pr_number", "comment"],
    }

    def execute_sync(
        self,
        repo_url: str = "",
        pr_number: int = 0,
        comment: str = "",
        task_id: str = "",
    ) -> ToolResult:
        log = _log(task_id)
        log.info("scm_add_pr_comment called", repo_url=repo_url, pr_number=pr_number)
        owner, repo = _parse_repo_coordinates(repo_url)
        # Use GitHub client directly for the comment API
        from agents.scm.client import create_scm_client
        import os as _os
        client = create_scm_client(
            base_url=repo_url,
            token=_os.environ.get("SCM_TOKEN", ""),
            backend="github-rest",
        )
        result, status = client.add_pr_comment(owner, repo, pr_number, comment)
        if status != "ok":
            log.error("scm_add_pr_comment failed", status=status, pr_number=pr_number)
            return ToolResult(output=json.dumps({"error": f"status={status}", "detail": result}))
        log.info("scm_add_pr_comment ok", pr_number=pr_number, comment_id=result.get("id"))
        return ToolResult(output=json.dumps({"ok": True, "comment_id": result.get("id")}))


class SCMUploadPRImage(BaseTool):
    """Upload a local image file to a GitHub PR and return the CDN URL.

    The image is uploaded via GitHub's issue-assets upload endpoint (same
    mechanism used by drag-and-drop in the GitHub web UI).  The returned
    ``image_url`` is a ``user-images.githubusercontent.com`` CDN link that
    can be embedded in PR descriptions or comments without committing any
    binary file to the repository.
    """

    name = "scm_upload_pr_image"
    description = (
        "Upload a local image file to a GitHub PR via GitHub's asset upload "
        "endpoint and return the CDN URL for embedding in Markdown."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "repo_url": {"type": "string", "description": "Full GitHub repo URL."},
            "pr_number": {"type": "integer", "description": "Pull request number."},
            "image_path": {"type": "string", "description": "Absolute path to the local image file."},
            "filename": {"type": "string", "description": "Override filename used in the upload (optional)."},
            "task_id": {"type": "string"},
        },
        "required": ["repo_url", "pr_number", "image_path"],
    }

    def execute_sync(
        self,
        repo_url: str = "",
        pr_number: int = 0,
        image_path: str = "",
        filename: str = "",
        task_id: str = "",
    ) -> ToolResult:
        from agents.scm.tools import _parse_repo_coordinates
        import os as _os
        log = _log(task_id)
        log.info("scm_upload_pr_image called", repo_url=repo_url, pr_number=pr_number,
                 image_path=image_path)
        if not _os.path.isfile(image_path):
            return ToolResult(output=json.dumps({"error": f"image_path not found: {image_path}"}))
        owner, repo = _parse_repo_coordinates(repo_url)
        from agents.scm.client import create_scm_client  # noqa: PLC0415 (local import OK)
        client = create_scm_client(
            base_url=repo_url,
            token=_os.environ.get("SCM_TOKEN", ""),
            backend="github-rest",
        )
        result, status = client.upload_issue_image(
            owner, repo, pr_number, image_path, filename=filename, task_id=task_id
        )
        if status != "ok":
            log.error("scm_upload_pr_image failed", status=status, error=str(result)[:200])
            return ToolResult(output=json.dumps({
                "error": f"upload failed: status={status}",
                "detail": str(result)[:200],
            }))
        image_url = result.get("href", "")
        log.info("scm_upload_pr_image ok", image_url=image_url[:80] if image_url else "")
        return ToolResult(output=json.dumps({"ok": True, "image_url": image_url}))


class SCMUpdatePR(BaseTool):
    """Update an existing pull request's title and/or description body."""

    name = "scm_update_pr"
    description = (
        "Update the title and/or description body of an existing pull request. "
        "Use this to append screenshot CDN URLs or review feedback to the PR description."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "repo_url": {"type": "string", "description": "Full GitHub repo URL."},
            "pr_number": {"type": "integer", "description": "Pull request number."},
            "description": {"type": "string", "description": "New Markdown body for the PR description."},
            "title": {"type": "string", "description": "New PR title (optional)."},
            "task_id": {"type": "string"},
        },
        "required": ["repo_url", "pr_number", "description"],
    }

    def execute_sync(
        self,
        repo_url: str = "",
        pr_number: int = 0,
        description: str = "",
        title: str = "",
        task_id: str = "",
    ) -> ToolResult:
        log = _log(task_id)
        log.info("scm_update_pr called", repo_url=repo_url, pr_number=pr_number)
        owner, repo = _parse_repo_coordinates(repo_url)
        from agents.scm.client import create_scm_client
        import os as _os
        client = create_scm_client(
            base_url=repo_url,
            token=_os.environ.get("SCM_TOKEN", ""),
            backend="github-rest",
        )
        kwargs: dict = {"body": description or None}
        if title:
            kwargs["title"] = title
        result, status = client.update_pr(owner, repo, pr_number, **kwargs)
        if status not in ("ok", "no_changes"):
            log.error("scm_update_pr failed", status=status, pr_number=pr_number)
            return ToolResult(output=json.dumps({"error": f"status={status}", "detail": result}))
        log.info("scm_update_pr ok", pr_number=pr_number, status=status)
        return ToolResult(output=json.dumps({"ok": True, "status": status}))


class SCMGetPRDiff(BaseTool):
    """Get the full diff of a pull request."""

    name = "scm_get_pr_diff"
    description = "Get the full diff text and list of changed files for a pull request."
    parameters_schema = {
        "type": "object",
        "properties": {
            "repo_url": {"type": "string", "description": "Full GitHub repo URL."},
            "pr_number": {"type": "integer", "description": "Pull request number."},
            "task_id": {"type": "string"},
        },
        "required": ["repo_url", "pr_number"],
    }

    def execute_sync(
        self,
        repo_url: str = "",
        pr_number: int = 0,
        task_id: str = "",
    ) -> ToolResult:
        log = _log(task_id)
        log.info("scm_get_pr_diff called", repo_url=repo_url, pr_number=pr_number)
        owner, repo = _parse_repo_coordinates(repo_url)
        from agents.scm.client import create_scm_client
        import os as _os
        client = create_scm_client(
            base_url=repo_url,
            token=_os.environ.get("SCM_TOKEN", ""),
            backend="github-rest",
        )
        # Use GitHub's pull diff endpoint
        result, status = client.get_pr_diff(owner, repo, pr_number)
        if status != "ok":
            log.error("scm_get_pr_diff failed", status=status, pr_number=pr_number)
            return ToolResult(output=json.dumps({"error": f"status={status}", "detail": str(result)[:200]}))
        log.info("scm_get_pr_diff ok", pr_number=pr_number,
                 changed_files_count=len(result.get("changed_files", [])))
        return ToolResult(output=json.dumps(result))


class SCMGetPRInfo(BaseTool):
    """Get pull request metadata (title, description, state, author, commits)."""

    name = "scm_get_pr_info"
    description = "Get pull request metadata including title, description, state, author, and commits."
    parameters_schema = {
        "type": "object",
        "properties": {
            "repo_url": {"type": "string", "description": "Full GitHub repo URL."},
            "pr_number": {"type": "integer", "description": "Pull request number."},
            "task_id": {"type": "string"},
        },
        "required": ["repo_url", "pr_number"],
    }

    def execute_sync(
        self,
        repo_url: str = "",
        pr_number: int = 0,
        task_id: str = "",
    ) -> ToolResult:
        log = _log(task_id)
        log.info("scm_get_pr_info called", repo_url=repo_url, pr_number=pr_number)
        owner, repo = _parse_repo_coordinates(repo_url)
        from agents.scm.client import create_scm_client
        import os as _os
        client = create_scm_client(
            base_url=repo_url,
            token=_os.environ.get("SCM_TOKEN", ""),
            backend="github-rest",
        )
        result, status = client.get_pr_info(owner, repo, pr_number)
        if status != "ok":
            log.error("scm_get_pr_info failed", status=status, pr_number=pr_number)
            return ToolResult(output=json.dumps({"error": f"status={status}", "detail": str(result)[:200]}))
        log.info("scm_get_pr_info ok", pr_number=pr_number, title=result.get("title", "")[:60])
        return ToolResult(output=json.dumps(result))


_TOOLS = [
    CloneRepo(),
    SCMListBranches(),
    SCMPush(),
    SCMCreatePR(),
    SCMAddPRComment(),
    SCMUploadPRImage(),
    SCMUpdatePR(),
    SCMGetPRDiff(),
    SCMGetPRInfo(),
]


def register_scm_tools() -> None:
    """Register in-process SCM tools (idempotent, won't override existing)."""
    registry = get_registry()
    existing = {s["function"]["name"] for s in registry.list_schemas()}
    for tool in _TOOLS:
        if tool.name not in existing:
            registry.register(tool)
    print(f"[scm-tools] Registered: {[t.name for t in _TOOLS if t.name not in existing]}")
