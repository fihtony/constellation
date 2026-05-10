"""SCM (Source Control Management) tools for agentic runtimes.

Import this module to register SCM tools (branch, push, PR creation).
"""

from __future__ import annotations

import json
import os
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

from common.tools.agent_discovery import discover_capability_url
from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import is_registered, register_tool

_REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")
_ACK_TIMEOUT = int(os.environ.get("A2A_ACK_TIMEOUT_SECONDS", "15"))
_TASK_TIMEOUT = int(os.environ.get("A2A_TASK_TIMEOUT_SECONDS", "300"))
_CLONE_TASK_TIMEOUT = int(os.environ.get("SCM_CLONE_TASK_TIMEOUT_SECONDS", "600"))
_TASK_POLL_INTERVAL = float(os.environ.get("A2A_TASK_POLL_INTERVAL_SECONDS", "1.0"))


def _discover_scm_url(capability: str) -> str | None:
    try:
        return discover_capability_url(_REGISTRY_URL, capability)
    except Exception:  # noqa: BLE001
        return None


def _a2a_send(agent_url: str, capability: str, params: dict) -> dict:
    agent_url = agent_url.rstrip("/")
    payload = {
        "message": {
            "role": "user",
            "parts": [{"text": json.dumps(params, ensure_ascii=False)}],
            "metadata": {"requestedCapability": capability, **params},
        },
        "configuration": {"returnImmediately": True},
    }
    req = Request(
        f"{agent_url}/message:send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=_ACK_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    task = data.get("task") or {}
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        return data
    if _is_terminal_task(task):
        return task
    timeout = _CLONE_TASK_TIMEOUT if capability == "scm.git.clone" else _TASK_TIMEOUT
    return _poll_task(agent_url, task_id, timeout=timeout)


def _is_terminal_task(task: dict) -> bool:
    state = str(((task or {}).get("status") or {}).get("state") or "")
    return state in {
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_INPUT_REQUIRED",
    }


def _poll_task(agent_url: str, task_id: str, *, timeout: int) -> dict:
    deadline = time.time() + max(1, timeout)
    last_task: dict = {"id": task_id}
    while time.time() < deadline:
        req = Request(
            f"{agent_url}/tasks/{task_id}",
            headers={"Accept": "application/json"},
        )
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        task = data.get("task") or {}
        if task:
            last_task = task
        if _is_terminal_task(task):
            return task
        time.sleep(_TASK_POLL_INTERVAL)

    status = last_task.setdefault("status", {})
    status.setdefault("state", "TASK_STATE_FAILED")
    status.setdefault(
        "message",
        {"parts": [{"text": f"Timed out waiting for {task_id} to complete."}]},
    )
    return last_task


class ScmCreateBranchTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_create_branch",
            description="Create a new branch in the repository.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository path or URL"},
                    "branch": {"type": "string", "description": "New branch name"},
                    "base": {"type": "string", "description": "Base branch (default: main)"},
                },
                "required": ["repo", "branch"],
            },
        )

    def execute(self, args: dict) -> dict:
        url = _discover_scm_url("scm.git.branch")
        if not url:
            return self.error("SCM Agent is not available.")
        try:
            result = _a2a_send(url, "scm.git.branch", args)
            return self.ok(json.dumps(result, ensure_ascii=False))
        except (URLError, OSError) as exc:
            return self.error(f"SCM create branch failed: {exc}")


class ScmPushFilesTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_push_files",
            description="Push a set of files to the remote repository on the given branch.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository path or URL"},
                    "branch": {"type": "string", "description": "Target branch"},
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string"},
                                "content": {"type": "string"},
                            },
                            "required": ["path", "content"],
                        },
                        "description": "Files to push (path + content)",
                    },
                    "commit_message": {"type": "string", "description": "Commit message"},
                },
                "required": ["repo", "branch", "files", "commit_message"],
            },
        )

    def execute(self, args: dict) -> dict:
        url = _discover_scm_url("scm.git.push")
        if not url:
            return self.error("SCM Agent is not available.")
        try:
            result = _a2a_send(url, "scm.git.push", args)
            return self.ok(json.dumps(result, ensure_ascii=False))
        except (URLError, OSError) as exc:
            return self.error(f"SCM push files failed: {exc}")


class ScmCreatePRTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_create_pr",
            description="Create a pull request in the repository.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository path or URL"},
                    "title": {"type": "string", "description": "PR title"},
                    "body": {"type": "string", "description": "PR description"},
                    "head": {"type": "string", "description": "Source branch"},
                    "base": {"type": "string", "description": "Target branch (default: main)"},
                },
                "required": ["repo", "title", "head"],
            },
        )

    def execute(self, args: dict) -> dict:
        url = _discover_scm_url("scm.pr.create")
        if not url:
            return self.error("SCM Agent is not available.")
        try:
            result = _a2a_send(url, "scm.pr.create", args)
            return self.ok(json.dumps(result, ensure_ascii=False))
        except (URLError, OSError) as exc:
            return self.error(f"SCM create PR failed: {exc}")


for _tool in (ScmCreateBranchTool(), ScmPushFilesTool(), ScmCreatePRTool()):
    if not is_registered(_tool.schema.name):
        register_tool(_tool)


# ---------------------------------------------------------------------------
# Remote read-only SCM tools
# These call dedicated REST endpoints on the SCM Agent.
# ---------------------------------------------------------------------------

def _scm_rest(endpoint: str, params: dict) -> dict:
    """Call a SCM Agent REST endpoint directly (non-A2A) with JSON params."""
    scm_url = _discover_scm_url("scm.repo.inspect")
    if not scm_url:
        raise OSError("SCM Agent is not available.")
    # Build query string from params
    import urllib.parse
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None and v != ""})
    full_url = f"{scm_url}{endpoint}"
    if qs:
        full_url = f"{full_url}?{qs}"
    req = Request(full_url, headers={"Accept": "application/json"})
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _scm_rest_post(endpoint: str, body: dict) -> dict:
    """POST to a SCM Agent REST endpoint with JSON body."""
    scm_url = _discover_scm_url("scm.repo.inspect")
    if not scm_url:
        raise OSError("SCM Agent is not available.")
    req = Request(
        f"{scm_url}{endpoint}",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


class ScmReadFileTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_read_file",
            description=(
                "Read a file from a remote repository branch without cloning. "
                "Returns the file content as text."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner or organization."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "path": {"type": "string", "description": "File path within the repository."},
                    "ref": {"type": "string", "description": "Branch, tag, or commit SHA (default: main)."},
                },
                "required": ["owner", "repo", "path"],
            },
        )

    def execute(self, args: dict) -> dict:
        try:
            result = _scm_rest("/scm/remote/file", {
                "owner": args.get("owner", ""),
                "repo": args.get("repo", ""),
                "path": args.get("path", ""),
                "ref": args.get("ref", ""),
            })
            return self.ok(json.dumps(result, ensure_ascii=False))
        except (URLError, OSError) as exc:
            return self.error(f"scm_read_file failed: {exc}")


class ScmListDirTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_list_dir",
            description=(
                "List directory contents of a remote repository without cloning. "
                "Returns file/directory names with types."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner or organization."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "path": {"type": "string", "description": "Directory path (use '' or '/' for root)."},
                    "ref": {"type": "string", "description": "Branch, tag, or commit SHA (default: main)."},
                },
                "required": ["owner", "repo"],
            },
        )

    def execute(self, args: dict) -> dict:
        try:
            result = _scm_rest("/scm/remote/dir", {
                "owner": args.get("owner", ""),
                "repo": args.get("repo", ""),
                "path": args.get("path", ""),
                "ref": args.get("ref", ""),
            })
            return self.ok(json.dumps(result, ensure_ascii=False))
        except (URLError, OSError) as exc:
            return self.error(f"scm_list_dir failed: {exc}")


class ScmSearchCodeTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_search_code",
            description=(
                "Search for code patterns in a remote repository without cloning. "
                "Returns matching files and snippets."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner or organization."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "query": {"type": "string", "description": "Search query or code pattern."},
                    "ref": {"type": "string", "description": "Branch to search on (optional)."},
                },
                "required": ["owner", "repo", "query"],
            },
        )

    def execute(self, args: dict) -> dict:
        try:
            result = _scm_rest("/scm/remote/search", {
                "owner": args.get("owner", ""),
                "repo": args.get("repo", ""),
                "query": args.get("query", ""),
                "ref": args.get("ref", ""),
            })
            return self.ok(json.dumps(result, ensure_ascii=False))
        except (URLError, OSError) as exc:
            return self.error(f"scm_search_code failed: {exc}")


class ScmCompareRefsTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_compare_refs",
            description=(
                "Compare two branches or commits in a remote repository. "
                "Returns ahead/behind counts, changed files, and optionally diffs."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "base": {"type": "string", "description": "Base branch or commit SHA."},
                    "head": {"type": "string", "description": "Head branch or commit SHA."},
                    "stat_only": {
                        "type": "boolean",
                        "description": "If true, return only stats (no full diff content).",
                    },
                },
                "required": ["owner", "repo", "base", "head"],
            },
        )

    def execute(self, args: dict) -> dict:
        try:
            result = _scm_rest("/scm/refs/compare", {
                "owner": args.get("owner", ""),
                "repo": args.get("repo", ""),
                "base": args.get("base", ""),
                "head": args.get("head", ""),
                "stat_only": "true" if args.get("stat_only") else "",
            })
            return self.ok(json.dumps(result, ensure_ascii=False))
        except (URLError, OSError) as exc:
            return self.error(f"scm_compare_refs failed: {exc}")


class ScmGetDefaultBranchTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_get_default_branch",
            description=(
                "Get the default branch and protected branches of a remote repository. "
                "Use this before creating a new branch to avoid targeting protected branches."
            ),
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
        try:
            result = _scm_rest("/scm/branch/default", {
                "owner": args.get("owner", ""),
                "repo": args.get("repo", ""),
            })
            return self.ok(json.dumps(result, ensure_ascii=False))
        except (URLError, OSError) as exc:
            return self.error(f"scm_get_default_branch failed: {exc}")


class ScmGetBranchRulesTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_get_branch_rules",
            description=(
                "Get branch protection rules for a repository. "
                "Returns combined policy from local permissions config and remote repo settings."
            ),
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
        try:
            result = _scm_rest("/scm/branch/rules", {
                "owner": args.get("owner", ""),
                "repo": args.get("repo", ""),
                "branch": args.get("branch", ""),
            })
            return self.ok(json.dumps(result, ensure_ascii=False))
        except (URLError, OSError) as exc:
            return self.error(f"scm_get_branch_rules failed: {exc}")


class ScmGetPRDetailsTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_get_pr_details",
            description=(
                "Get details of a pull request including title, description, status, "
                "reviewers, labels, and linked issues."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository path or URL."},
                    "pr_number": {"type": "integer", "description": "Pull request number."},
                },
                "required": ["repo", "pr_number"],
            },
        )

    def execute(self, args: dict) -> dict:
        url = _discover_scm_url("scm.pr.get")
        if not url:
            return self.error("SCM Agent is not available.")
        try:
            result = _a2a_send(url, "scm.pr.get", args)
            return self.ok(json.dumps(result, ensure_ascii=False))
        except (URLError, OSError) as exc:
            return self.error(f"scm_get_pr_details failed: {exc}")


class ScmGetPRDiffTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_get_pr_diff",
            description=(
                "Get the diff content of a pull request. "
                "Use this for code review to inspect what changed."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository path or URL."},
                    "pr_number": {"type": "integer", "description": "Pull request number."},
                    "stat_only": {
                        "type": "boolean",
                        "description": "If true, return only file change stats (no full diff).",
                    },
                },
                "required": ["repo", "pr_number"],
            },
        )

    def execute(self, args: dict) -> dict:
        url = _discover_scm_url("scm.pr.get")
        if not url:
            return self.error("SCM Agent is not available.")
        try:
            params = dict(args)
            params["include_diff"] = True
            result = _a2a_send(url, "scm.pr.get", params)
            return self.ok(json.dumps(result, ensure_ascii=False))
        except (URLError, OSError) as exc:
            return self.error(f"scm_get_pr_diff failed: {exc}")


class ScmListBranchesTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_list_branches",
            description="List branches in a remote repository.",
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository path or URL."},
                },
                "required": ["repo"],
            },
        )

    def execute(self, args: dict) -> dict:
        url = _discover_scm_url("scm.git.branches")
        if not url:
            return self.error("SCM Agent is not available.")
        try:
            result = _a2a_send(url, "scm.git.branches", args)
            return self.ok(json.dumps(result, ensure_ascii=False))
        except (URLError, OSError) as exc:
            return self.error(f"scm_list_branches failed: {exc}")


class ScmCloneRepoTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_clone_repo",
            description=(
                "Clone a remote repository into the shared workspace. "
                "Use full_history=true when git history is needed; "
                "otherwise a shallow clone (depth=1) is faster."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository URL or owner/name."},
                    "branch": {"type": "string", "description": "Branch to clone (default: default branch)."},
                    "full_history": {
                        "type": "boolean",
                        "description": "Clone full history instead of shallow depth=1.",
                    },
                    "depth": {
                        "type": "integer",
                        "description": "Custom clone depth (overrides full_history).",
                    },
                    "workspace_path": {
                        "type": "string",
                        "description": "Destination path inside the shared workspace.",
                    },
                },
                "required": ["repo"],
            },
        )

    def execute(self, args: dict) -> dict:
        url = _discover_scm_url("scm.git.clone")
        if not url:
            return self.error("SCM Agent is not available.")
        try:
            result = _a2a_send(url, "scm.git.clone", args)
            return self.ok(json.dumps(result, ensure_ascii=False))
        except (URLError, OSError) as exc:
            return self.error(f"scm_clone_repo failed: {exc}")


class ScmRepoInspectTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="scm_repo_inspect",
            description=(
                "Inspect a remote repository: get metadata, default branch, "
                "languages, topics, and basic stats."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository URL or owner/name."},
                },
                "required": ["repo"],
            },
        )

    def execute(self, args: dict) -> dict:
        url = _discover_scm_url("scm.repo.inspect")
        if not url:
            return self.error("SCM Agent is not available.")
        try:
            result = _a2a_send(url, "scm.repo.inspect", args)
            return self.ok(json.dumps(result, ensure_ascii=False))
        except (URLError, OSError) as exc:
            return self.error(f"scm_repo_inspect failed: {exc}")


for _tool in (
    ScmReadFileTool(),
    ScmListDirTool(),
    ScmSearchCodeTool(),
    ScmCompareRefsTool(),
    ScmGetDefaultBranchTool(),
    ScmGetBranchRulesTool(),
    ScmGetPRDetailsTool(),
    ScmGetPRDiffTool(),
    ScmListBranchesTool(),
    ScmCloneRepoTool(),
    ScmRepoInspectTool(),
):
    if not is_registered(_tool.schema.name):
        register_tool(_tool)
