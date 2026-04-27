"""GitHub MCP provider for the SCM agent.

Uses the official remote GitHub MCP server over HTTP (Streamable HTTP transport).
Connects to https://api.githubcopilot.com/mcp/ with a GitHub PAT token.
No local installation required — uses the cloud-hosted server.

Implements the same SCMProvider interface as GitHubProvider so the SCM agent
can switch back-ends via SCM_BACKEND=mcp.

The provider maintains a session (Mcp-Session-Id) across calls so the remote
server can maintain per-session state.  A threading.Lock serialises concurrent
requests.  If the session becomes invalid, the next call re-initialises.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from scm.providers.base import SCMProvider

GITHUB_MCP_URL = "https://api.githubcopilot.com/mcp/"
MCP_PROTOCOL_VERSION = "2024-11-05"
JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


# ---------------------------------------------------------------------------
# Low-level HTTP MCP client
# ---------------------------------------------------------------------------

class _HTTPMCPSession:
    """HTTP JSON-RPC 2.0 session for the remote GitHub MCP server (Streamable HTTP)."""

    def __init__(self, token: str, url: str = GITHUB_MCP_URL, timeout: int = 60):
        self._token = token
        self._url = url
        self._timeout = timeout
        self._req_id = 0
        self._session_id: str | None = None
        self._alive = False

    def start(self) -> None:
        """Perform the MCP initialize handshake."""
        resp = self._rpc("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "constellation-scm-agent", "version": "1.0"},
        })
        if "error" in resp:
            raise RuntimeError(f"MCP init failed: {resp['error']}")
        self._notify("notifications/initialized", {})
        self._alive = True

    def _headers(self) -> dict:
        h = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _post(self, payload: dict, timeout: int | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = Request(self._url, data=data, headers=self._headers(), method="POST")
        try:
            with urlopen(req, timeout=timeout or self._timeout) as resp:
                sid = resp.getheader("Mcp-Session-Id")
                if sid:
                    self._session_id = sid
                ct = resp.getheader("Content-Type", "")
                raw = resp.read().decode("utf-8")
                if not raw.strip():
                    return {}
                if "text/event-stream" in ct:
                    return self._parse_sse(raw, payload.get("id"))
                return json.loads(raw)
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            self._alive = False
            try:
                return json.loads(raw)
            except Exception:
                return {"error": {"code": exc.code, "message": raw[:300]}}
        except URLError as exc:
            self._alive = False
            return {"error": {"code": -1, "message": str(exc)}}

    def _rpc(self, method: str, params: dict, timeout: int | None = None) -> dict:
        self._req_id += 1
        return self._post({
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
            "params": params,
        }, timeout)

    def _notify(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            data = json.dumps(payload).encode("utf-8")
            req = Request(self._url, data=data, headers=self._headers(), method="POST")
            with urlopen(req, timeout=10):
                pass
        except Exception:
            pass

    def _parse_sse(self, raw: str, expected_id: int | None) -> dict:
        """Extract JSON-RPC response from a Server-Sent Events stream."""
        for line in raw.splitlines():
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if not data_str or data_str == "[DONE]":
                continue
            try:
                data = json.loads(data_str)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            if expected_id is None or item.get("id") == expected_id:
                                return item
                elif isinstance(data, dict):
                    if expected_id is None or data.get("id") == expected_id:
                        return data
            except json.JSONDecodeError:
                pass
        return {"error": {"code": -1, "message": "no matching response in SSE stream"}}

    def call(self, tool_name: str, arguments: dict, timeout: int = 60) -> dict:
        return self._rpc("tools/call", {"name": tool_name, "arguments": arguments}, timeout)

    def tools_list(self) -> list:
        resp = self._rpc("tools/list", {})
        return (resp.get("result") or {}).get("tools", [])

    def stop(self) -> None:
        """Delete the session on the remote server (best-effort)."""
        self._alive = False
        if self._session_id:
            try:
                req = Request(self._url, headers=self._headers(), method="DELETE")
                with urlopen(req, timeout=5):
                    pass
            except Exception:
                pass
            self._session_id = None


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _extract_text(resp: dict) -> str:
    result = resp.get("result") or {}
    if isinstance(result, dict):
        parts = [
            item.get("text", "")
            for item in result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(parts)
    return str(result)


def _is_error(resp: dict) -> bool:
    return bool((resp.get("result") or {}).get("isError")) or "error" in resp


def _parse_json(text: str) -> Any:
    """Best-effort JSON parse from MCP text response."""
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Sometimes the MCP server wraps in markdown code blocks
        m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
        if m:
            try:
                return json.loads(m.group(1))
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# GitHub MCP Provider
# ---------------------------------------------------------------------------

class GitHubMCPProvider(SCMProvider):
    """SCM provider backed by the remote GitHub MCP server (cloud HTTP)."""

    def __init__(
        self,
        token: str = "",
        timeout: int = 60,
        author_name: str = "SCM Agent",
        author_email: str = "scm-agent@local",
    ):
        self._token = token.strip()
        self._timeout = timeout
        self._author_name = author_name
        self._author_email = author_email
        self._session: _HTTPMCPSession | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Session management
    # ------------------------------------------------------------------

    def _get_session(self) -> _HTTPMCPSession:
        """Return the active MCP session, re-initialising as needed."""
        if self._session and self._session._alive:
            return self._session
        if self._session:
            try:
                self._session.stop()
            except Exception:
                pass
        self._session = _HTTPMCPSession(self._token, timeout=self._timeout)
        self._session.start()
        return self._session

    def _call(self, tool: str, args: dict, timeout: int = 60) -> dict:
        """Thread-safe MCP tool call with auto-reconnect on failure."""
        with self._lock:
            try:
                return self._get_session().call(tool, args, timeout)
            except Exception:
                self._session = None
                return self._get_session().call(tool, args, timeout)

    def close(self) -> None:
        """Delete the remote MCP session (best-effort)."""
        with self._lock:
            if self._session:
                self._session.stop()
                self._session = None

    # ------------------------------------------------------------------
    # Repository discovery
    # ------------------------------------------------------------------

    def search_repos(self, query: str, limit: int = 10) -> tuple[list[dict], str]:
        if not query.strip():
            return [], "missing_query"
        resp = self._call("search_repositories", {
            "query": query,
            "perPage": min(limit, 30),
        })
        if _is_error(resp):
            return [], f"error: {_extract_text(resp)[:100]}"
        data = _parse_json(_extract_text(resp))
        if isinstance(data, dict) and "items" in data:
            return [self._normalize_repo(r) for r in data["items"]], "ok"
        if isinstance(data, list):
            return [self._normalize_repo(r) for r in data], "ok"
        return [], "ok"

    def get_repo(self, owner: str, repo: str) -> tuple[dict, str]:
        resp = self._call("search_repositories", {
            "query": f"repo:{owner}/{repo}",
            "perPage": 1,
        })
        if _is_error(resp):
            return {}, f"error: {_extract_text(resp)[:100]}"
        data = _parse_json(_extract_text(resp))
        items = []
        if isinstance(data, dict) and "items" in data:
            items = data["items"]
        elif isinstance(data, list):
            items = data
        if items:
            return self._normalize_repo(items[0]), "ok"
        return {}, "not_found"

    def _normalize_repo(self, r: dict) -> dict:
        return {
            "provider": "github",
            "owner": (r.get("owner") or {}).get("login", ""),
            "repo": r.get("name", ""),
            "fullName": r.get("full_name", ""),
            "description": r.get("description", ""),
            "defaultBranch": r.get("default_branch", "main"),
            "cloneUrl": r.get("clone_url", ""),
            "htmlUrl": r.get("html_url", ""),
            "private": r.get("private", False),
            "language": r.get("language", ""),
        }

    # ------------------------------------------------------------------
    # Branches
    # ------------------------------------------------------------------

    def list_branches(self, owner: str, repo: str) -> tuple[list[dict], str]:
        resp = self._call("list_branches", {"owner": owner, "repo": repo, "perPage": 100})
        if _is_error(resp):
            return [], f"error: {_extract_text(resp)[:100]}"
        data = _parse_json(_extract_text(resp))
        if not isinstance(data, list):
            return [], "error_parse"
        branches = [
            {
                "name": b.get("name", ""),
                "sha": (b.get("commit") or {}).get("sha", ""),
                "default": False,
            }
            for b in data
        ]
        # mark default
        _, repo_info = self.get_repo(owner, repo)
        default_name = repo_info.get("defaultBranch", "") if isinstance(repo_info, dict) else ""
        for b in branches:
            if b["name"] == default_name:
                b["default"] = True
        return branches, "ok"

    def create_branch(self, owner: str, repo: str, branch: str, from_ref: str) -> tuple[dict, str]:
        resp = self._call("create_branch", {
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "from_branch": from_ref,
        })
        if _is_error(resp):
            return {}, f"create_failed: {_extract_text(resp)[:150]}"
        return {
            "name": branch,
            "htmlUrl": f"https://github.com/{owner}/{repo}/tree/{branch}",
        }, "created"

    # ------------------------------------------------------------------
    # Pull requests
    # ------------------------------------------------------------------

    def list_prs(self, owner: str, repo: str, state: str = "open") -> tuple[list[dict], str]:
        s = (state or "open").lower()
        if s not in ("open", "closed", "all"):
            s = "open"
        resp = self._call("list_pull_requests", {
            "owner": owner, "repo": repo, "state": s, "perPage": 50,
        })
        if _is_error(resp):
            return [], f"error: {_extract_text(resp)[:100]}"
        data = _parse_json(_extract_text(resp))
        if isinstance(data, list):
            return [self._normalize_pr(pr) for pr in data], "ok"
        return [], "ok"

    def get_pr(self, owner: str, repo: str, pr_id: int | str) -> tuple[dict, str]:
        resp = self._call("pull_request_read", {
            "owner": owner, "repo": repo,
            "pullNumber": int(pr_id),
            "method": "get",
        })
        if _is_error(resp):
            return {}, f"error: {_extract_text(resp)[:100]}"
        data = _parse_json(_extract_text(resp))
        if isinstance(data, dict):
            return self._normalize_pr(data), "ok"
        return {}, "error_parse"

    def create_pr(
        self,
        owner: str,
        repo: str,
        from_branch: str,
        to_branch: str,
        title: str,
        description: str = "",
    ) -> tuple[dict, str]:
        resp = self._call("create_pull_request", {
            "owner": owner,
            "repo": repo,
            "title": title,
            "body": description,
            "head": from_branch,
            "base": to_branch,
        })
        if _is_error(resp):
            return {}, f"create_failed: {_extract_text(resp)[:150]}"
        data = _parse_json(_extract_text(resp)) or {}
        # Remote MCP returns {"id": "...", "url": "https://.../pull/5"}
        # Parse the PR number from the url and fetch full details
        pr_url = data.get("url", "") or data.get("html_url", "")
        pr_number: int | None = None
        if pr_url and "/pull/" in pr_url:
            try:
                pr_number = int(pr_url.rstrip("/").rsplit("/pull/", 1)[1])
            except (ValueError, IndexError):
                pass
        if pr_number is None and isinstance(data.get("id"), (int, float)):
            pr_number = int(data["id"])
        if pr_number:
            pr_full, status = self.get_pr(owner, repo, pr_number)
            if status == "ok":
                return pr_full, "created"
        # Minimal fallback if get_pr fails
        if pr_number:
            return {
                "provider": "github",
                "id": pr_number,
                "htmlUrl": pr_url,
                "fromBranch": from_branch,
                "toBranch": to_branch,
                "title": title,
            }, "created"
        return {}, "error_parse"

    def _normalize_pr(self, pr: dict) -> dict:
        head = pr.get("head") or {}
        base = pr.get("base") or {}
        head_repo = head.get("repo") or {}
        linked_issues = JIRA_KEY_RE.findall(
            f"{pr.get('title', '')} {pr.get('body', '')} {head.get('ref', '')}"
        )
        return {
            "provider": "github",
            "id": pr.get("number"),
            "title": pr.get("title", ""),
            "description": pr.get("body", ""),
            "state": pr.get("state", ""),
            "fromBranch": head.get("ref", ""),
            "toBranch": base.get("ref", ""),
            "htmlUrl": pr.get("html_url", ""),
            "cloneUrl": head_repo.get("clone_url", ""),
            "linkedJiraIssues": list(dict.fromkeys(linked_issues)),
            "author": (pr.get("user") or {}).get("login", ""),
            "createdAt": pr.get("created_at", ""),
        }

    # ------------------------------------------------------------------
    # PR comments
    # ------------------------------------------------------------------

    def add_pr_comment(
        self,
        owner: str,
        repo: str,
        pr_id: int | str,
        text: str,
        file_path: str = "",
        line: int | None = None,
    ) -> tuple[dict, str]:
        if file_path and line is not None:
            # Inline review comment: need the latest commit SHA
            pr_resp = self._call("pull_request_read", {
                "owner": owner, "repo": repo,
                "pullNumber": int(pr_id), "method": "get",
            })
            pr_data = _parse_json(_extract_text(pr_resp)) or {}
            commit_id = (pr_data.get("head") or {}).get("sha", "")
            if not commit_id:
                return {}, "error_commit_not_found"
            # Create a single-comment review
            resp = self._call("pull_request_review_write", {
                "owner": owner, "repo": repo,
                "pullNumber": int(pr_id),
                "method": "create",
                "body": text,
                "commitID": commit_id,
                "event": "COMMENT",
            })
        else:
            # General PR comment via Issues API
            resp = self._call("add_issue_comment", {
                "owner": owner, "repo": repo,
                "issue_number": int(pr_id),
                "body": text,
            })
        if _is_error(resp):
            return {}, f"create_failed: {_extract_text(resp)[:150]}"
        data = _parse_json(_extract_text(resp)) or {}
        return {
            "id": data.get("id"),
            "body": data.get("body", ""),
            "htmlUrl": data.get("html_url", ""),
        }, "created"

    def list_pr_comments(self, owner: str, repo: str, pr_id: int | str) -> tuple[list[dict], str]:
        resp = self._call("issue_read", {
            "owner": owner, "repo": repo,
            "issue_number": int(pr_id),
            "method": "get_comments",
        })
        if _is_error(resp):
            return [], f"error: {_extract_text(resp)[:100]}"
        data = _parse_json(_extract_text(resp))
        if isinstance(data, list):
            return [
                {
                    "id": c.get("id"),
                    "body": c.get("body", ""),
                    "author": (c.get("user") or {}).get("login", ""),
                }
                for c in data
            ], "ok"
        return [], "ok"

    # ------------------------------------------------------------------
    # Git operations
    # ------------------------------------------------------------------

    def get_clone_url(self, owner: str, repo: str) -> str:
        return f"https://github.com/{owner}/{repo}.git"

    def push_files(
        self,
        owner: str,
        repo: str,
        branch: str,
        base_branch: str,
        files: list[dict],
        commit_message: str,
        files_to_delete: list[str] | None = None,
    ) -> tuple[dict, str]:
        if not files:
            return {}, "no_files"

        # Ensure the branch exists (create from base_branch if needed)
        branches, _ = self.list_branches(owner, repo)
        branch_names = [b["name"] for b in branches]
        if branch not in branch_names:
            _, create_status = self.create_branch(owner, repo, branch, base_branch)
            if not create_status.startswith("created"):
                return {}, f"branch_create_failed: {create_status}"

        # Build the files array for push_files MCP tool
        mcp_files = []
        for f in files:
            path = f.get("path", "").lstrip("/")
            if not path:
                continue
            content = f.get("content", "")
            if isinstance(content, bytes):
                try:
                    content = content.decode("utf-8")
                except Exception:
                    import base64 as _b64
                    content = _b64.b64encode(content).decode("ascii")
            mcp_files.append({"path": path, "content": content})

        if not mcp_files:
            return {}, "no_valid_files"

        resp = self._call("push_files", {
            "owner": owner,
            "repo": repo,
            "branch": branch,
            "files": mcp_files,
            "message": commit_message,
        }, timeout=60)

        if _is_error(resp):
            return {}, f"push_failed: {_extract_text(resp)[:200]}"

        return {
            "branch": branch,
            "message": commit_message,
            "htmlUrl": f"https://github.com/{owner}/{repo}/tree/{branch}",
        }, "pushed"

    # ------------------------------------------------------------------
    # Provider identity
    # ------------------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return "github-mcp"
