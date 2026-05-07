"""Internal SCM provider tools for agentic runtime.

These tools wrap the local SCM provider directly (no HTTP self-calls).
They are registered in the global tool registry so the connect-agent runtime
can expose them to the LLM running inside the SCM Agent process.

Usage in app.py:
    import scm.provider_tools as _pt          # auto-registers tools
    _pt.configure_scm_provider_tools(
        message=message,
        provider=_provider,
        permission_fn=lambda action, target, scope="*": _require_scm_permission(
            action=action, target=target, scope=scope, message=message
        ),
        clone_fn=_clone_to_workspace,
    )
"""
from __future__ import annotations

import json
from typing import Any, Callable

from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import is_registered, register_tool

# ---------------------------------------------------------------------------
# Per-task context — configured by configure_scm_provider_tools() before
# run_agentic() is called.
# ---------------------------------------------------------------------------
_current_message: dict = {}
_current_provider: Any = None
_permission_fn: Callable[[str, str, str], None] | None = None
_clone_fn: Callable[..., Any] | None = None


def configure_scm_provider_tools(
    *,
    message: dict,
    provider: Any,
    permission_fn: Callable[[str, str, str], None] | None = None,
    clone_fn: Callable[..., Any] | None = None,
) -> None:
    """Wire up the provider and permission callback for the current task."""
    global _current_message, _current_provider, _permission_fn, _clone_fn
    _current_message = message
    _current_provider = provider
    _permission_fn = permission_fn
    _clone_fn = clone_fn


def _require(action: str, target: str, scope: str = "*") -> None:
    if _permission_fn:
        _permission_fn(action, target, scope)


# ---------------------------------------------------------------------------
# Read-only tools
# ---------------------------------------------------------------------------

class _ScmRepoInspectTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_repo_inspect",
            description="Inspect repository metadata: default branch, languages, topics, size.",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner or organization."},
                    "repo": {"type": "string", "description": "Repository name."},
                },
                "required": ["owner", "repo"],
            },
        )

    def execute(self, args: dict) -> dict:
        owner, repo = args.get("owner", ""), args.get("repo", "")
        _require("repo.read", f"{owner}/{repo}")
        info, status = _current_provider.get_repo(owner, repo)
        if "error" in (status or "").lower() and not info:
            return self.error(f"scm_repo_inspect: {status}")
        return self.ok(json.dumps(info or {}, ensure_ascii=False))


class _ScmListBranchesTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_list_branches",
            description="List all branches in a remote repository.",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                },
                "required": ["owner", "repo"],
            },
        )

    def execute(self, args: dict) -> dict:
        owner, repo = args.get("owner", ""), args.get("repo", "")
        _require("repo.read", f"{owner}/{repo}")
        branches, status = _current_provider.list_branches(owner, repo)
        if "error" in (status or "").lower() and not branches:
            return self.error(f"scm_list_branches: {status}")
        return self.ok(json.dumps(branches or [], ensure_ascii=False))


class _ScmGetDefaultBranchTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_get_default_branch",
            description="Get the default branch and protected branches of a repository.",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                },
                "required": ["owner", "repo"],
            },
        )

    def execute(self, args: dict) -> dict:
        owner, repo = args.get("owner", ""), args.get("repo", "")
        _require("repo.read", f"{owner}/{repo}")
        result, status = _current_provider.get_default_branch(owner, repo)
        if "error" in (status or "").lower() and not result:
            return self.error(f"scm_get_default_branch: {status}")
        return self.ok(json.dumps(result or {}, ensure_ascii=False))


class _ScmGetBranchRulesTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_get_branch_rules",
            description="Get branch protection rules for a repository.",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "branch": {"type": "string", "description": "Branch to check rules for (optional)."},
                },
                "required": ["owner", "repo"],
            },
        )

    def execute(self, args: dict) -> dict:
        owner, repo = args.get("owner", ""), args.get("repo", "")
        branch = args.get("branch", "")
        _require("repo.read", f"{owner}/{repo}")
        result, status = _current_provider.get_branch_rules(owner, repo)
        if "error" in (status or "").lower() and not result:
            return self.error(f"scm_get_branch_rules: {status}")
        return self.ok(json.dumps(result or {}, ensure_ascii=False))


class _ScmReadFileTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_read_file",
            description="Read a file from a remote repository branch without cloning.",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner or organization."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "path": {"type": "string", "description": "File path within the repository."},
                    "ref": {"type": "string", "description": "Branch, tag, or commit SHA."},
                },
                "required": ["owner", "repo", "path"],
            },
        )

    def execute(self, args: dict) -> dict:
        owner, repo = args.get("owner", ""), args.get("repo", "")
        _require("repo.read", f"{owner}/{repo}")
        content, status = _current_provider.read_remote_file(
            owner, repo, args.get("path", ""), args.get("ref", "")
        )
        if "error" in (status or "").lower() and not content:
            return self.error(f"scm_read_file: {status}")
        return self.ok(content or "")


class _ScmListDirTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_list_dir",
            description="List directory contents of a remote repository without cloning.",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "path": {"type": "string", "description": "Directory path (use '' for root)."},
                    "ref": {"type": "string", "description": "Branch, tag, or commit SHA."},
                },
                "required": ["owner", "repo"],
            },
        )

    def execute(self, args: dict) -> dict:
        owner, repo = args.get("owner", ""), args.get("repo", "")
        _require("repo.read", f"{owner}/{repo}")
        entries, status = _current_provider.list_remote_dir(
            owner, repo, args.get("path", ""), args.get("ref", "")
        )
        if "error" in (status or "").lower() and not entries:
            return self.error(f"scm_list_dir: {status}")
        return self.ok(json.dumps(entries or [], ensure_ascii=False))


class _ScmSearchCodeTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_search_code",
            description="Search for code patterns in a remote repository without cloning.",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "query": {"type": "string", "description": "Search query or code pattern."},
                    "ref": {"type": "string", "description": "Branch to search on (optional)."},
                },
                "required": ["owner", "repo", "query"],
            },
        )

    def execute(self, args: dict) -> dict:
        owner, repo = args.get("owner", ""), args.get("repo", "")
        _require("repo.read", f"{owner}/{repo}")
        results, status = _current_provider.search_code(
            owner, repo, args.get("query", ""), 20
        )
        if "error" in (status or "").lower() and not results:
            return self.error(f"scm_search_code: {status}")
        return self.ok(json.dumps(results or [], ensure_ascii=False))


class _ScmCompareRefsTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_compare_refs",
            description="Compare two branches or commits in a remote repository.",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "base": {"type": "string", "description": "Base branch or commit SHA."},
                    "head": {"type": "string", "description": "Head branch or commit SHA."},
                    "stat_only": {
                        "type": "boolean",
                        "description": "Return only stats (no full diff content).",
                    },
                },
                "required": ["owner", "repo", "base", "head"],
            },
        )

    def execute(self, args: dict) -> dict:
        owner, repo = args.get("owner", ""), args.get("repo", "")
        _require("repo.read", f"{owner}/{repo}")
        result, status = _current_provider.compare_refs(
            owner, repo,
            args.get("base", ""),
            args.get("head", ""),
            args.get("stat_only", False),
        )
        if "error" in (status or "").lower() and not result:
            return self.error(f"scm_compare_refs: {status}")
        return self.ok(json.dumps(result or {}, ensure_ascii=False))


class _ScmGetPRDetailsTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_get_pr_details",
            description="Get details of a pull request including title, status, and reviewers.",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "pr_number": {"type": "integer", "description": "Pull request number."},
                },
                "required": ["owner", "repo", "pr_number"],
            },
        )

    def execute(self, args: dict) -> dict:
        owner, repo = args.get("owner", ""), args.get("repo", "")
        _require("pr.read", f"{owner}/{repo}")
        pr, status = _current_provider.get_pr(owner, repo, args.get("pr_number", 0))
        if "error" in (status or "").lower() and not pr:
            return self.error(f"scm_get_pr_details: {status}")
        return self.ok(json.dumps(pr or {}, ensure_ascii=False))


class _ScmGetPRDiffTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_get_pr_diff",
            description="Get the diff content of a pull request for code review.",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "pr_number": {"type": "integer", "description": "Pull request number."},
                    "stat_only": {
                        "type": "boolean",
                        "description": "Return only file change stats (no full diff).",
                    },
                },
                "required": ["owner", "repo", "pr_number"],
            },
        )

    def execute(self, args: dict) -> dict:
        owner, repo = args.get("owner", ""), args.get("repo", "")
        _require("pr.read", f"{owner}/{repo}")
        result, status = _current_provider.compare_refs(
            owner, repo, "", "", args.get("stat_only", False)
        )
        # Fallback: get PR then compare base vs head
        pr, pr_status = _current_provider.get_pr(owner, repo, args.get("pr_number", 0))
        if pr:
            base = (pr.get("base") or {}).get("sha") or (pr.get("base") or {}).get("ref", "")
            head = (pr.get("head") or {}).get("sha") or (pr.get("head") or {}).get("ref", "")
            if base and head:
                result, status = _current_provider.compare_refs(
                    owner, repo, base, head, args.get("stat_only", False)
                )
        return self.ok(json.dumps(result or {}, ensure_ascii=False))


class _ScmListPRsTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_list_prs",
            description="List pull requests for a repository.",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "state": {
                        "type": "string",
                        "description": "Filter by state: 'open', 'closed', 'merged' (default: open).",
                    },
                },
                "required": ["owner", "repo"],
            },
        )

    def execute(self, args: dict) -> dict:
        owner, repo = args.get("owner", ""), args.get("repo", "")
        _require("pr.read", f"{owner}/{repo}")
        prs, status = _current_provider.list_prs(owner, repo, args.get("state", "open"))
        if "error" in (status or "").lower() and not prs:
            return self.error(f"scm_list_prs: {status}")
        return self.ok(json.dumps(prs or [], ensure_ascii=False))


class _ScmListPRCommentsTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_list_pr_comments",
            description="List comments on a pull request.",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "pr_number": {"type": "integer", "description": "Pull request number."},
                },
                "required": ["owner", "repo", "pr_number"],
            },
        )

    def execute(self, args: dict) -> dict:
        owner, repo = args.get("owner", ""), args.get("repo", "")
        _require("pr.read", f"{owner}/{repo}")
        comments, status = _current_provider.list_pr_comments(
            owner, repo, args.get("pr_number", 0)
        )
        if "error" in (status or "").lower() and not comments:
            return self.error(f"scm_list_pr_comments: {status}")
        return self.ok(json.dumps(comments or [], ensure_ascii=False))


# ---------------------------------------------------------------------------
# Write tools
# ---------------------------------------------------------------------------

class _ScmCreateBranchTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_create_branch",
            description="Create a new branch from a base ref in a remote repository.",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "branch": {"type": "string", "description": "New branch name."},
                    "from_ref": {
                        "type": "string",
                        "description": "Base branch or commit SHA to branch from.",
                    },
                },
                "required": ["owner", "repo", "branch"],
            },
        )

    def execute(self, args: dict) -> dict:
        owner, repo = args.get("owner", ""), args.get("repo", "")
        _require("branch.create", f"{owner}/{repo}")
        result, status = _current_provider.create_branch(
            owner, repo, args.get("branch", ""), args.get("from_ref", "")
        )
        if "error" in (status or "").lower() and not result:
            return self.error(f"scm_create_branch: {status}")
        return self.ok(json.dumps(result or {}, ensure_ascii=False))


class _ScmPushFilesTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_push_files",
            description="Push file changes to a remote branch.",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "branch": {"type": "string", "description": "Target branch to push to."},
                    "base_branch": {"type": "string", "description": "Base branch (used for PR target)."},
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                        },
                        "description": "List of {path, content} objects to push.",
                    },
                    "commit_message": {"type": "string", "description": "Commit message."},
                    "files_to_delete": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file paths to delete.",
                    },
                },
                "required": ["owner", "repo", "branch", "files", "commit_message"],
            },
        )

    def execute(self, args: dict) -> dict:
        owner, repo = args.get("owner", ""), args.get("repo", "")
        _require("branch.push", f"{owner}/{repo}")
        result, status = _current_provider.push_files(
            owner,
            repo,
            args.get("branch", ""),
            args.get("base_branch", ""),
            args.get("files", []),
            args.get("commit_message", ""),
            args.get("files_to_delete", []),
        )
        if "error" in (status or "").lower() and not result:
            return self.error(f"scm_push_files: {status}")
        return self.ok(json.dumps(result or {}, ensure_ascii=False))


class _ScmCreatePRTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_create_pr",
            description="Create a pull request in a remote repository.",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "from_branch": {"type": "string", "description": "Source branch."},
                    "to_branch": {"type": "string", "description": "Target branch."},
                    "title": {"type": "string", "description": "PR title."},
                    "description": {"type": "string", "description": "PR description (Markdown)."},
                },
                "required": ["owner", "repo", "from_branch", "to_branch", "title"],
            },
        )

    def execute(self, args: dict) -> dict:
        owner, repo = args.get("owner", ""), args.get("repo", "")
        _require("pr.create", f"{owner}/{repo}")
        pr, status = _current_provider.create_pr(
            owner,
            repo,
            args.get("from_branch", ""),
            args.get("to_branch", ""),
            args.get("title", ""),
            args.get("description", ""),
        )
        if "error" in (status or "").lower() and not pr:
            return self.error(f"scm_create_pr: {status}")
        return self.ok(json.dumps(pr or {}, ensure_ascii=False))


class _ScmAddPRCommentTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_add_pr_comment",
            description="Add a review comment to a pull request.",
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "pr_number": {"type": "integer", "description": "Pull request number."},
                    "comment": {"type": "string", "description": "Comment text (Markdown)."},
                },
                "required": ["owner", "repo", "pr_number", "comment"],
            },
        )

    def execute(self, args: dict) -> dict:
        owner, repo = args.get("owner", ""), args.get("repo", "")
        _require("pr.comment", f"{owner}/{repo}")
        result, status = _current_provider.add_pr_comment(
            owner, repo, args.get("pr_number", 0), args.get("comment", "")
        )
        if "error" in (status or "").lower() and not result:
            return self.error(f"scm_add_pr_comment: {status}")
        return self.ok(json.dumps(result or {}, ensure_ascii=False))


class _ScmCloneRepoTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_clone_repo",
            description=(
                "Clone a remote repository into the shared workspace. "
                "Returns the local clone path."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "branch": {"type": "string", "description": "Branch to clone (default: default branch)."},
                    "workspace_path": {
                        "type": "string",
                        "description": "Destination path inside the shared workspace.",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Clone depth (default: 1 for shallow). Use 0 for full history.",
                    },
                },
                "required": ["owner", "repo"],
            },
        )

    def execute(self, args: dict) -> dict:
        owner, repo = args.get("owner", ""), args.get("repo", "")
        _require("repo.clone", f"{owner}/{repo}")
        if not _clone_fn:
            return self.error("Clone function not configured.")
        clone_url = _current_provider.get_clone_url(owner, repo)
        try:
            result = _clone_fn(
                repo_url=clone_url,
                branch=args.get("branch", ""),
                workspace_path=args.get("workspace_path", ""),
                depth=args.get("depth", 1),
                message=_current_message,
            )
            return self.ok(json.dumps(result or {}, ensure_ascii=False))
        except Exception as exc:
            return self.error(f"scm_clone_repo: {exc}")


# ---------------------------------------------------------------------------
# Self-registration — runs once at import time.
# ---------------------------------------------------------------------------
_TOOLS = [
    _ScmRepoInspectTool(),
    _ScmListBranchesTool(),
    _ScmGetDefaultBranchTool(),
    _ScmGetBranchRulesTool(),
    _ScmReadFileTool(),
    _ScmListDirTool(),
    _ScmSearchCodeTool(),
    _ScmCompareRefsTool(),
    _ScmGetPRDetailsTool(),
    _ScmGetPRDiffTool(),
    _ScmListPRsTool(),
    _ScmListPRCommentsTool(),
    _ScmCreateBranchTool(),
    _ScmPushFilesTool(),
    _ScmCreatePRTool(),
    _ScmAddPRCommentTool(),
    _ScmCloneRepoTool(),
]

for _t in _TOOLS:
    if not is_registered(_t.schema.name):
        register_tool(_t)
