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
            log.error("clone_repo failed", error=result["error"])
        else:
            log.info("clone_repo ok", target_path=target_path)
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
        if result.get("error"):
            log.error("scm_create_pr failed", error=result["error"])
        else:
            log.info("scm_create_pr ok", pr_url=pr_url)
        return ToolResult(output=json.dumps(result))


_TOOLS = [CloneRepo(), SCMListBranches(), SCMPush(), SCMCreatePR()]


def register_scm_tools() -> None:
    """Register in-process SCM tools (idempotent, won't override existing)."""
    registry = get_registry()
    existing = {s["function"]["name"] for s in registry.list_schemas()}
    for tool in _TOOLS:
        if tool.name not in existing:
            registry.register(tool)
    print(f"[scm-tools] Registered: {[t.name for t in _TOOLS if t.name not in existing]}")
