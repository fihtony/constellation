"""Code Review Agent boundary tools for SCM PR metadata and diff retrieval."""
from __future__ import annotations

import json
from urllib.parse import urlparse

from framework.tools.base import BaseTool, ToolResult
from framework.tools.registry import get_registry


def _resolve_agent_url(capability: str) -> str:
    try:
        from framework.registry_client import RegistryClient

        client = RegistryClient.from_config()
        url = client.discover(capability)
        if url:
            return url
    except Exception:
        pass
    return ""


def _dispatch_scm(capability: str, text: str = "", **meta) -> dict:
    scm_url = _resolve_agent_url(capability)
    if not scm_url:
        return {"error": "No registered SCM instance was found in the registry."}
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
            return f"~{parts[owner_idx + 1]}", parts[repo_idx + 1]

    if len(parts) >= 2:
        owner = parts[0]
        repo = parts[1]
        if repo.endswith(".git"):
            repo = repo[:-4]
        return owner, repo

    return "", ""


def fetch_pr_diff(
    repo_url: str,
    pr_number: int,
    *,
    task_id: str = "",
    permissions: dict | None = None,
    **kwargs,
) -> dict:
    project, repo = _parse_repo_coordinates(repo_url)
    if not project or not repo:
        return {"error": "Cannot infer project/repo from repo_url"}
    meta = {
        "project": project,
        "repo": repo,
        "repoUrl": repo_url,
        "prNumber": pr_number,
        "taskId": task_id,
        **kwargs,
    }
    if isinstance(permissions, dict):
        meta["permissions"] = permissions
    return _dispatch_scm("scm.pr.diff", text=f"{project}/{repo}", **meta)


def fetch_pr_info(
    repo_url: str,
    pr_number: int,
    *,
    task_id: str = "",
    permissions: dict | None = None,
    **kwargs,
) -> dict:
    project, repo = _parse_repo_coordinates(repo_url)
    if not project or not repo:
        return {"error": "Cannot infer project/repo from repo_url"}
    meta = {
        "project": project,
        "repo": repo,
        "repoUrl": repo_url,
        "prNumber": pr_number,
        "taskId": task_id,
        **kwargs,
    }
    if isinstance(permissions, dict):
        meta["permissions"] = permissions
    return _dispatch_scm("scm.pr.info", text=f"{project}/{repo}", **meta)


class SCMGetPRDiff(BaseTool):
    name = "scm_get_pr_diff"
    description = "Fetch a PR diff through the SCM boundary agent."
    parameters_schema = {
        "type": "object",
        "properties": {
            "repo_url": {"type": "string"},
            "pr_number": {"type": "integer"},
            "task_id": {"type": "string"},
        },
        "required": ["repo_url", "pr_number"],
    }

    def execute_sync(
        self,
        repo_url: str = "",
        pr_number: int = 0,
        task_id: str = "",
        **kwargs,
    ) -> ToolResult:
        result = fetch_pr_diff(
            repo_url,
            pr_number,
            task_id=task_id,
            permissions=kwargs.pop("permissions", None),
            **kwargs,
        )
        return ToolResult(output=json.dumps(result))


class SCMGetPRInfo(BaseTool):
    name = "scm_get_pr_info"
    description = "Fetch PR metadata through the SCM boundary agent."
    parameters_schema = {
        "type": "object",
        "properties": {
            "repo_url": {"type": "string"},
            "pr_number": {"type": "integer"},
            "task_id": {"type": "string"},
        },
        "required": ["repo_url", "pr_number"],
    }

    def execute_sync(
        self,
        repo_url: str = "",
        pr_number: int = 0,
        task_id: str = "",
        **kwargs,
    ) -> ToolResult:
        result = fetch_pr_info(
            repo_url,
            pr_number,
            task_id=task_id,
            permissions=kwargs.pop("permissions", None),
            **kwargs,
        )
        return ToolResult(output=json.dumps(result))


_TOOLS = [
    SCMGetPRDiff(),
    SCMGetPRInfo(),
]


def register_code_review_tools() -> None:
    registry = get_registry()
    existing = {schema["function"]["name"] for schema in registry.list_schemas()}
    for tool in _TOOLS:
        if tool.name not in existing:
            registry.register(tool)
