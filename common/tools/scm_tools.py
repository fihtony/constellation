"""SCM (Source Control Management) tools for agentic runtimes.

Import this module to register SCM tools (branch, push, PR creation).
"""

from __future__ import annotations

import json
import os
from urllib.error import URLError
from urllib.request import Request, urlopen

from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import register_tool

_REGISTRY_URL = os.environ.get("REGISTRY_URL", "http://registry:9000")
_ACK_TIMEOUT = int(os.environ.get("A2A_ACK_TIMEOUT_SECONDS", "15"))


def _discover_scm_url(capability: str) -> str | None:
    try:
        req = Request(
            f"{_REGISTRY_URL}/query?capability={capability}",
            headers={"Accept": "application/json"},
        )
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        agents = data.get("agents") or []
        for agent in agents:
            instances = agent.get("instances") or []
            for inst in instances:
                url = inst.get("url") or agent.get("baseUrl")
                if url:
                    return url.rstrip("/")
        return None
    except Exception:  # noqa: BLE001
        return None


def _a2a_send(agent_url: str, capability: str, params: dict) -> dict:
    payload = {
        "jsonrpc": "2.0",
        "id": "tool-call",
        "method": "message:send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": json.dumps(params)}],
            },
            "metadata": {"capability": capability, **params},
        },
    }
    req = Request(
        f"{agent_url}/message:send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(req, timeout=_ACK_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


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


register_tool(ScmCreateBranchTool())
register_tool(ScmPushFilesTool())
register_tool(ScmCreatePRTool())
