"""Web Dev Agent boundary tools — Jira and SCM operations via A2A dispatch.

These tools allow the Web Dev Agent's workflow nodes to call boundary agents
(Jira, SCM) through the standard A2A protocol.
"""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse

from framework.tools.base import BaseTool, ToolResult
from framework.tools.registry import get_registry


def _resolve_agent_url(capability: str, env_var: str = "", default: str = "") -> str:
    """Resolve an agent's URL via Registry discovery, env var, or default."""
    if env_var:
        env_val = os.environ.get(env_var)
        if env_val:
            return env_val

    try:
        from framework.registry_client import RegistryClient
        client = RegistryClient.from_config()
        url = client.discover(capability)
        if url:
            return url
    except Exception:
        pass

    if not default:
        try:
            from framework.config import load_global_config
            cfg = load_global_config()
            services = cfg.get("services") or {}
            if "jira" in capability:
                return services.get("jira_agent_url", "http://jira:8010")
            if "scm" in capability:
                return services.get("scm_agent_url", "http://scm:8020")
        except Exception:
            pass

    return default


def _dispatch_jira(capability: str, ticket_key: str = "", **meta) -> dict:
    """Dispatch a Jira capability via A2A."""
    jira_url = _resolve_agent_url(capability, "JIRA_AGENT_URL", "http://jira:8010")
    try:
        from framework.a2a.client import dispatch_sync
        result = dispatch_sync(
            url=jira_url,
            capability=capability,
            message_parts=[{"text": ticket_key}],
            metadata={"ticketKey": ticket_key, **meta},
        )
        artifacts = result.get("task", result).get("artifacts", [])
        if artifacts:
            parts = artifacts[0].get("parts", [])
            if parts:
                return json.loads(parts[0].get("text", "{}"))
        return {}
    except Exception as exc:
        return {"error": str(exc)}


def _dispatch_scm(capability: str, text: str = "", **meta) -> dict:
    """Dispatch an SCM capability via A2A."""
    scm_url = _resolve_agent_url(capability, "SCM_AGENT_URL", "http://scm:8020")
    try:
        from framework.a2a.client import dispatch_sync

        result = dispatch_sync(
            url=scm_url,
            capability=capability,
            message_parts=[{"text": text}],
            metadata=meta,
        )
        artifacts = result.get("task", result).get("artifacts", [])
        if artifacts:
            parts = artifacts[0].get("parts", [])
            if parts:
                return json.loads(parts[0].get("text", "{}"))
        return {}
    except Exception as exc:
        return {"error": str(exc)}


def _parse_repo_coordinates(repo_url: str) -> tuple[str, str]:
    """Infer SCM project/owner and repo name from a repository URL."""
    parts = [part for part in urlparse(repo_url).path.split("/") if part]
    if not parts:
        return "", ""

    if "projects" in parts and "repos" in parts:
        project_idx = parts.index("projects")
        repo_idx = parts.index("repos")
        if project_idx + 1 < len(parts) and repo_idx + 1 < len(parts):
            return parts[project_idx + 1], parts[repo_idx + 1]

    if "users" in parts and "repos" in parts:
        owner_idx = parts.index("users")
        repo_idx = parts.index("repos")
        if owner_idx + 1 < len(parts) and repo_idx + 1 < len(parts):
            # Bitbucket Server personal repos use ~username as the project key in REST API
            return f"~{parts[owner_idx + 1]}", parts[repo_idx + 1]

    if len(parts) >= 2:
        owner = parts[0]
        repo = parts[1]
        if repo.endswith(".git"):
            repo = repo[:-4]
        return owner, repo

    return "", ""


# ---------------------------------------------------------------------------
# Jira tools for Web Dev
# ---------------------------------------------------------------------------

class JiraTransition(BaseTool):
    """Transition a Jira ticket to a new status."""
    name = "jira_transition"
    description = "Transition a Jira ticket to a new status (e.g. In Progress, In Review)."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string", "description": "Jira ticket key"},
            "transition_name": {"type": "string", "description": "Target transition name"},
        },
        "required": ["ticket_key", "transition_name"],
    }

    def execute_sync(self, ticket_key: str = "", transition_name: str = "") -> ToolResult:
        result = _dispatch_jira(
            "jira.ticket.transition",
            ticket_key=ticket_key,
            transitionName=transition_name,
        )
        return ToolResult(output=json.dumps(result))


class JiraComment(BaseTool):
    """Add a comment to a Jira ticket."""
    name = "jira_comment"
    description = "Add a comment to a Jira ticket."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string", "description": "Jira ticket key"},
            "comment": {"type": "string", "description": "Comment text to add"},
        },
        "required": ["ticket_key", "comment"],
    }

    def execute_sync(self, ticket_key: str = "", comment: str = "") -> ToolResult:
        result = _dispatch_jira(
            "jira.ticket.comment",
            ticket_key=ticket_key,
            comment=comment,
        )
        return ToolResult(output=json.dumps(result))


class JiraUpdate(BaseTool):
    """Update Jira ticket fields."""
    name = "jira_update"
    description = "Update fields on a Jira ticket (e.g. assignee)."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string", "description": "Jira ticket key"},
            "fields": {"type": "object", "description": "Fields to update"},
        },
        "required": ["ticket_key", "fields"],
    }

    def execute_sync(self, ticket_key: str = "", fields: dict | None = None) -> ToolResult:
        result = _dispatch_jira(
            "jira.ticket.update",
            ticket_key=ticket_key,
            fields=fields or {},
        )
        return ToolResult(output=json.dumps(result))


class JiraListTransitions(BaseTool):
    """List available transitions for a Jira ticket."""
    name = "jira_list_transitions"
    description = "List the available status transitions for a Jira ticket."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string", "description": "Jira ticket key"},
        },
        "required": ["ticket_key"],
    }

    def execute_sync(self, ticket_key: str = "") -> ToolResult:
        result = _dispatch_jira("jira.transitions.list", ticket_key=ticket_key)
        return ToolResult(output=json.dumps(result))


class JiraGetTokenUser(BaseTool):
    """Resolve the current Jira token user identity."""
    name = "jira_get_token_user"
    description = "Get the identity of the Jira user associated with the current API token."
    parameters_schema = {"type": "object", "properties": {}, "required": []}

    def execute_sync(self) -> ToolResult:
        result = _dispatch_jira("jira.user.me")
        return ToolResult(output=json.dumps(result))


class JiraListComments(BaseTool):
    """List comments on a Jira ticket."""
    name = "jira_list_comments"
    description = "List comments on a Jira ticket for idempotency checks."
    parameters_schema = {
        "type": "object",
        "properties": {
            "ticket_key": {"type": "string", "description": "Jira ticket key"},
        },
        "required": ["ticket_key"],
    }

    def execute_sync(self, ticket_key: str = "") -> ToolResult:
        result = _dispatch_jira("jira.comment.list", ticket_key=ticket_key)
        return ToolResult(output=json.dumps(result))


class SCMListBranches(BaseTool):
    """List remote branches for a repository via SCM Agent."""

    name = "scm_list_branches"
    description = "List remote branches for a repository through the SCM Agent."
    parameters_schema = {
        "type": "object",
        "properties": {
            "repo_url": {"type": "string", "description": "Repository URL"},
        },
        "required": ["repo_url"],
    }

    def execute_sync(self, repo_url: str = "") -> ToolResult:
        project, repo = _parse_repo_coordinates(repo_url)
        if not project or not repo:
            return ToolResult(output=json.dumps({"branches": [], "error": "Cannot infer project/repo from repo_url"}))
        result = _dispatch_scm(
            "scm.branch.list",
            text=f"{project}/{repo}",
            project=project,
            repo=repo,
        )
        return ToolResult(output=json.dumps(result))


class SCMPush(BaseTool):
    """Push a local branch to the remote via SCM Agent."""

    name = "scm_push"
    description = "Push a local branch to the remote repository through the SCM Agent."
    parameters_schema = {
        "type": "object",
        "properties": {
            "repo_path": {"type": "string", "description": "Local repository path"},
            "branch": {"type": "string", "description": "Local branch name"},
        },
        "required": ["repo_path", "branch"],
    }

    def execute_sync(self, repo_path: str = "", branch: str = "") -> ToolResult:
        result = _dispatch_scm(
            "scm.branch.push",
            text=branch,
            repoPath=repo_path,
            branch=branch,
        )
        return ToolResult(output=json.dumps(result))


class SCMCreatePR(BaseTool):
    """Create a PR through the SCM Agent using the repo URL to derive coordinates."""

    name = "scm_create_pr"
    description = "Create a pull request through the SCM Agent."
    parameters_schema = {
        "type": "object",
        "properties": {
            "repo_url": {"type": "string", "description": "Repository URL used to derive project/owner and repo"},
            "source_branch": {"type": "string", "description": "Source branch name"},
            "target_branch": {"type": "string", "description": "Target branch name", "default": "main"},
            "title": {"type": "string", "description": "Pull request title"},
            "description": {"type": "string", "description": "Pull request description"},
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
    ) -> ToolResult:
        project, repo = _parse_repo_coordinates(repo_url)
        if not project or not repo:
            return ToolResult(output=json.dumps({"error": "Unable to infer project/repo from repo_url"}))

        result = _dispatch_scm(
            "scm.pr.create",
            text=title,
            project=project,
            repo=repo,
            sourceBranch=source_branch,
            targetBranch=target_branch,
            title=title,
            description=description,
        )
        return ToolResult(output=json.dumps(result))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register_web_dev_tools():
    """Register all Web Dev boundary tools in the global ToolRegistry.

    SCM tools (scm_push, scm_create_pr, scm_list_branches) are only registered
    when they are NOT already present — the in-process SCM adapter registers
    direct-dispatch variants first during E2E/test setup, which must not be
    overridden by the HTTP-proxy versions here.
    """
    registry = get_registry()
    existing = {s["function"]["name"] for s in registry.list_schemas()}
    for tool_cls in (
        JiraTransition,
        JiraComment,
        JiraUpdate,
        JiraListTransitions,
        JiraGetTokenUser,
        JiraListComments,
        SCMListBranches,
        SCMPush,
        SCMCreatePR,
    ):
        tool = tool_cls()
        if tool.name not in existing:
            registry.register(tool)
