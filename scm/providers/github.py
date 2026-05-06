"""GitHub REST API provider for the SCM agent."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from common.env_utils import build_isolated_git_env
from scm.providers.base import SCMProvider

GITHUB_API_BASE = "https://api.github.com"
JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


class GitHubProvider(SCMProvider):
    """SCM provider that talks to the GitHub REST API."""

    def __init__(
        self,
        token: str = "",
        username: str = "",
        author_name: str = "SCM Agent",
        author_email: str = "scm-agent@local",
    ):
        self._token = token.strip()
        self._username = username.strip()
        self._author_name = author_name
        self._author_email = author_email

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _auth_header(self) -> str | None:
        if not self._token:
            return None
        if self._token.lower().startswith(("basic ", "bearer ", "token ")):
            return self._token
        return f"Bearer {self._token}"

    def _request(
        self, method: str, path: str, payload: dict | None = None, timeout: int = 20
    ) -> tuple[int, dict]:
        url = f"{GITHUB_API_BASE}/{path.lstrip('/')}"
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        auth = self._auth_header()
        if auth:
            headers["Authorization"] = auth
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, json.loads(raw) if raw.strip() else {}
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                return exc.code, json.loads(raw)
            except Exception:
                return exc.code, {"error": raw[:500]}
        except URLError as exc:
            return 0, {"error": str(exc)}

    # ------------------------------------------------------------------
    # Repository discovery
    # ------------------------------------------------------------------

    def search_repos(self, query: str, limit: int = 10) -> tuple[list[dict], str]:
        if not query.strip():
            return [], "missing_query"
        q = quote(query)
        status, body = self._request("GET", f"search/repositories?q={q}&per_page={min(limit, 30)}")
        if status == 200:
            items = body.get("items", [])
            return [self._normalize_repo(r) for r in items], "ok"
        return [], f"error_{status}"

    def get_repo(self, owner: str, repo: str) -> tuple[dict, str]:
        status, body = self._request("GET", f"repos/{owner}/{repo}")
        if status == 200:
            return self._normalize_repo(body), "ok"
        return body, f"error_{status}"

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
        status, body = self._request("GET", f"repos/{owner}/{repo}/branches?per_page=100")
        if status == 200 and isinstance(body, list):
            branches = [
                {"name": b["name"], "sha": (b.get("commit") or {}).get("sha", ""), "default": False}
                for b in body
            ]
            # mark default
            _, repo_info = self.get_repo(owner, repo)
            default_branch = ""
            if isinstance(repo_info, dict):
                default_branch = repo_info.get("defaultBranch", "")
            for b in branches:
                if b["name"] == default_branch:
                    b["default"] = True
            return branches, "ok"
        return [], f"error_{status}"

    def create_branch(self, owner: str, repo: str, branch: str, from_ref: str) -> tuple[dict, str]:
        # Resolve sha of from_ref via API
        ref_status, ref_body = self._request("GET", f"repos/{owner}/{repo}/git/ref/heads/{from_ref}")
        if ref_status != 200:
            sha = from_ref
        else:
            sha = (ref_body.get("object") or {}).get("sha", "")
        if not sha:
            return {"error": "could not resolve from_ref sha"}, "error_ref_not_found"

        # Try the REST API first
        status, body = self._request(
            "POST",
            f"repos/{owner}/{repo}/git/refs",
            {"ref": f"refs/heads/{branch}", "sha": sha},
        )
        if status in (200, 201):
            return {
                "name": branch,
                "sha": sha,
                "htmlUrl": f"https://github.com/{owner}/{repo}/tree/{branch}",
            }, "created"

        # Fine-grained PATs may not be allowed to use the refs API (403).
        # Fall back: create the branch via git push using token-embedded URL (same as push_files).
        # Use _run_git_url_auth (no extraHeader) to avoid conflicting Basic+Bearer auth.
        if status == 403 or status == 422:
            import tempfile
            with tempfile.TemporaryDirectory(prefix="scm-branch-") as tmpdir:
                clone_url = self._authed_clone_url(owner, repo)
                ok, _ = self._run_git_url_auth(
                    ["clone", "--depth=1", "--branch", from_ref, clone_url, tmpdir]
                )
                if not ok:
                    return body, f"create_failed_{status}"
                self._run_git_url_auth(["config", "user.email", self._author_email], cwd=tmpdir)
                self._run_git_url_auth(["config", "user.name", self._author_name], cwd=tmpdir)
                ok2, res2 = self._run_git_url_auth(
                    ["push", clone_url, f"HEAD:refs/heads/{branch}"], cwd=tmpdir
                )
                if ok2:
                    return {
                        "name": branch,
                        "sha": sha,
                        "htmlUrl": f"https://github.com/{owner}/{repo}/tree/{branch}",
                    }, "created"
                return res2, "create_failed_git_push"

        return body, f"create_failed_{status}"

    # ------------------------------------------------------------------
    # Pull requests
    # ------------------------------------------------------------------

    def list_prs(self, owner: str, repo: str, state: str = "open") -> tuple[list[dict], str]:
        s = (state or "open").lower()
        if s not in ("open", "closed", "all"):
            s = "open"
        status, body = self._request("GET", f"repos/{owner}/{repo}/pulls?state={s}&per_page=50")
        if status == 200 and isinstance(body, list):
            return [self._normalize_pr(pr) for pr in body], "ok"
        return [], f"error_{status}"

    def get_pr(self, owner: str, repo: str, pr_id: int | str) -> tuple[dict, str]:
        status, body = self._request("GET", f"repos/{owner}/{repo}/pulls/{pr_id}")
        if status == 200:
            return self._normalize_pr(body), "ok"
        return body, f"error_{status}"

    def _find_existing_open_pr(self, owner: str, repo: str, from_branch: str, to_branch: str) -> dict:
        prs, status = self.list_prs(owner, repo, "open")
        if status != "ok":
            return {}
        for pr in prs:
            if pr.get("fromBranch") == from_branch and pr.get("toBranch") == to_branch:
                return pr
        return {}

    def create_pr(
        self,
        owner: str,
        repo: str,
        from_branch: str,
        to_branch: str,
        title: str,
        description: str = "",
    ) -> tuple[dict, str]:
        status, body = self._request(
            "POST",
            f"repos/{owner}/{repo}/pulls",
            {"title": title, "body": description, "head": from_branch, "base": to_branch},
        )
        if status in (200, 201):
            return self._normalize_pr(body), "created"
        if status == 422:
            existing_pr = self._find_existing_open_pr(owner, repo, from_branch, to_branch)
            if existing_pr:
                return existing_pr, "already_exists"
        return body, f"create_failed_{status}"

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
        # Inline comment (on specific file/line)
        if file_path and line is not None:
            pr_info, pr_status = self.get_pr(owner, repo, pr_id)
            if pr_status != "ok":
                return pr_info, pr_status
            commit_id = ""
            # get latest commit on PR head
            pr_raw_status, pr_raw = self._request("GET", f"repos/{owner}/{repo}/pulls/{pr_id}")
            if pr_raw_status == 200:
                commit_id = (pr_raw.get("head") or {}).get("sha", "")
            if not commit_id:
                return {"error": "could not resolve commit sha"}, "error_commit_not_found"
            status, body = self._request(
                "POST",
                f"repos/{owner}/{repo}/pulls/{pr_id}/comments",
                {
                    "body": text,
                    "commit_id": commit_id,
                    "path": file_path,
                    "line": line,
                },
            )
        else:
            status, body = self._request(
                "POST",
                f"repos/{owner}/{repo}/issues/{pr_id}/comments",
                {"body": text},
            )
        if status in (200, 201):
            return {
                "id": body.get("id"),
                "body": body.get("body", ""),
                "htmlUrl": body.get("html_url", ""),
            }, "created"
        return body, f"create_failed_{status}"

    def list_pr_comments(self, owner: str, repo: str, pr_id: int | str) -> tuple[list[dict], str]:
        status, body = self._request("GET", f"repos/{owner}/{repo}/issues/{pr_id}/comments")
        if status == 200 and isinstance(body, list):
            return [
                {"id": c.get("id"), "body": c.get("body", ""), "author": (c.get("user") or {}).get("login", "")}
                for c in body
            ], "ok"
        return [], f"error_{status}"

    # ------------------------------------------------------------------
    # Git operations
    # ------------------------------------------------------------------

    def get_clone_url(self, owner: str, repo: str) -> str:
        return f"https://github.com/{owner}/{repo}.git"

    def _git_env(self) -> dict:
        return build_isolated_git_env(scope="scm-github")

    def _git_config_args(self) -> list[str]:
        """Return git -c args for authentication via http.extraHeader."""
        args = ["-c", "credential.helper="]
        auth = self._auth_header()
        if auth:
            args.extend(["-c", f"http.extraHeader=Authorization: {auth}"])
        return args

    def _run_git(self, args: list[str], cwd: str | None = None, timeout: int = 180) -> tuple[bool, dict]:
        command = ["git", *self._git_config_args(), *args]
        completed = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=self._git_env()
        )
        output = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode != 0:
            return False, {"command": command, "returncode": completed.returncode, "output": output}
        return True, {"command": command, "output": output}

    def _run_git_url_auth(self, args: list[str], cwd: str | None = None, timeout: int = 180) -> tuple[bool, dict]:
        """Run git without http.extraHeader — for operations where auth is embedded in the remote URL.

        Using both token-in-URL and http.extraHeader simultaneously sends conflicting
        Basic + Bearer credentials, which GitHub rejects. Use this method whenever the
        clone URL already contains the token (e.g. from _authed_clone_url).
        """
        command = ["git", "-c", "credential.helper=", *args]
        completed = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=timeout, env=self._git_env()
        )
        output = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode != 0:
            return False, {"returncode": completed.returncode, "output": output}
        return True, {"output": output}

    def _authed_clone_url(self, owner: str, repo: str) -> str:
        """Return clone URL with embedded token for reliable git auth.

        Token is embedded as x-access-token (GitHub fine-grained PAT compatible).
        This runs only inside the agent container and is never exposed externally.
        """
        if self._token:
            return f"https://x-access-token:{self._token}@github.com/{owner}/{repo}.git"
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
        clone_url = self._authed_clone_url(owner, repo)
        with tempfile.TemporaryDirectory(prefix="scm-push-") as tmpdir:
            # Use _run_git_url_auth throughout: auth is embedded in clone_url,
            # adding http.extraHeader on top would send conflicting Basic+Bearer to GitHub.
            ok, result = self._run_git_url_auth(
                ["clone", "--depth=1", "--branch", base_branch, clone_url, tmpdir]
            )
            if not ok:
                return result, "clone_failed"

            # Configure author
            self._run_git_url_auth(["config", "user.email", self._author_email], cwd=tmpdir)
            self._run_git_url_auth(["config", "user.name", self._author_name], cwd=tmpdir)

            # Create or switch to branch
            ok_branch, _ = self._run_git_url_auth(["checkout", "-b", branch], cwd=tmpdir)
            if not ok_branch:
                self._run_git_url_auth(["checkout", branch], cwd=tmpdir)

            # Write files
            for f in files:
                rel_path = f.get("path", "").lstrip("/")
                if not rel_path:
                    continue
                full = Path(tmpdir) / rel_path
                full.parent.mkdir(parents=True, exist_ok=True)
                content = f.get("content", "")
                if isinstance(content, str):
                    full.write_text(content, encoding="utf-8")
                else:
                    full.write_bytes(content)
                self._run_git_url_auth(["add", rel_path], cwd=tmpdir)

            # Delete files
            for d in (files_to_delete or []):
                rel = d.lstrip("/")
                full_del = Path(tmpdir) / rel
                if full_del.exists():
                    full_del.unlink()
                self._run_git_url_auth(["rm", "--cached", "--ignore-unmatch", rel], cwd=tmpdir)

            ok, result = self._run_git_url_auth(
                ["commit", "-m", commit_message], cwd=tmpdir
            )
            if not ok:
                return result, "commit_failed"

            # Push using the authed URL directly (not 'origin') to ensure token auth
            ok, result = self._run_git_url_auth(
                ["push", clone_url, f"HEAD:refs/heads/{branch}"], cwd=tmpdir
            )
            if not ok:
                return result, "push_failed"

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
        return "github"

    # ------------------------------------------------------------------
    # Remote read operations (no local clone required)
    # ------------------------------------------------------------------

    def read_remote_file(
        self, owner: str, repo: str, path: str, ref: str = ""
    ) -> tuple[str, str]:
        """Read a single file from a remote branch/ref via GitHub contents API."""
        endpoint = f"repos/{owner}/{repo}/contents/{path.lstrip('/')}"
        if ref:
            endpoint += f"?ref={quote(ref)}"
        status, body = self._request("GET", endpoint)
        if status != 200:
            return "", f"error_{status}"
        encoding = body.get("encoding", "")
        raw_content = body.get("content", "")
        if encoding == "base64":
            try:
                decoded = base64.b64decode(raw_content.replace("\n", "")).decode("utf-8", errors="replace")
                return decoded, "ok"
            except Exception as exc:
                return "", f"decode_error: {exc}"
        # Plain text fallback (unlikely from GitHub API)
        return raw_content, "ok"

    def list_remote_dir(
        self, owner: str, repo: str, path: str = "", ref: str = ""
    ) -> tuple[list[dict], str]:
        """List directory contents from remote repo via GitHub contents API."""
        endpoint = f"repos/{owner}/{repo}/contents/{path.lstrip('/')}" if path else f"repos/{owner}/{repo}/contents"
        if ref:
            endpoint += f"?ref={quote(ref)}"
        status, body = self._request("GET", endpoint)
        if status != 200:
            return [], f"error_{status}"
        if isinstance(body, list):
            entries = [
                {
                    "name": item.get("name", ""),
                    "path": item.get("path", ""),
                    "type": item.get("type", ""),  # "file" | "dir" | "symlink"
                    "size": item.get("size", 0),
                    "htmlUrl": item.get("html_url", ""),
                }
                for item in body
            ]
            return entries, "ok"
        # Single file response — wrap it
        if isinstance(body, dict) and body.get("type") == "file":
            return [
                {
                    "name": body.get("name", ""),
                    "path": body.get("path", ""),
                    "type": "file",
                    "size": body.get("size", 0),
                    "htmlUrl": body.get("html_url", ""),
                }
            ], "ok"
        return [], "empty_or_unexpected"

    def search_code(
        self, owner: str, repo: str, query: str, limit: int = 20
    ) -> tuple[list[dict], str]:
        """Search code in the repository via GitHub code search API."""
        q = quote(f"{query} repo:{owner}/{repo}")
        per_page = min(max(1, limit), 30)
        status, body = self._request("GET", f"search/code?q={q}&per_page={per_page}")
        if status != 200:
            return [], f"error_{status}"
        items = body.get("items") or []
        results = []
        for item in items:
            fragments = [
                match.get("fragment", "")
                for match in (item.get("text_matches") or [])
                if match.get("fragment")
            ]
            results.append(
                {
                    "path": item.get("path", ""),
                    "htmlUrl": item.get("html_url", ""),
                    "repository": item.get("repository", {}).get("full_name", ""),
                    "fragmentText": "\n---\n".join(fragments) if fragments else "",
                }
            )
        return results, "ok"

    def compare_refs(
        self,
        owner: str,
        repo: str,
        base: str,
        head: str,
        stat_only: bool = False,
    ) -> tuple[dict, str]:
        """Compare two branches or commits via GitHub compare API."""
        endpoint = f"repos/{owner}/{repo}/compare/{quote(base, safe='')}...{quote(head, safe='')}"
        status, body = self._request("GET", endpoint)
        if status != 200:
            return {}, f"error_{status}"
        files_raw = body.get("files") or []
        file_entries = [
            {
                "filename": f.get("filename", ""),
                "status": f.get("status", ""),
                "additions": f.get("additions", 0),
                "deletions": f.get("deletions", 0),
                "changes": f.get("changes", 0),
            }
            for f in files_raw
        ]
        result: dict = {
            "aheadBy": body.get("ahead_by", 0),
            "behindBy": body.get("behind_by", 0),
            "totalChangedFiles": body.get("total_commits", len(file_entries)),
            "additions": sum(f["additions"] for f in file_entries),
            "deletions": sum(f["deletions"] for f in file_entries),
            "files": file_entries,
            "status": body.get("status", ""),  # "ahead" | "behind" | "diverged" | "identical"
        }
        if not stat_only:
            # Include unified diff snippets from patch fields when available
            diffs = [
                f.get("patch", "")
                for f in files_raw
                if f.get("patch")
            ]
            result["diff"] = "\n".join(diffs)
        return result, "ok"

    def get_default_branch(self, owner: str, repo: str) -> tuple[dict, str]:
        """Return the default branch and a list of protected branches."""
        repo_info, status = self.get_repo(owner, repo)
        if status != "ok":
            return {}, status
        default_branch = repo_info.get("defaultBranch", "main")
        # Fetch protected branches from GitHub API
        prot_status, prot_body = self._request(
            "GET", f"repos/{owner}/{repo}/branches?protected=true&per_page=100"
        )
        protected: list[str] = []
        if prot_status == 200 and isinstance(prot_body, list):
            protected = [b["name"] for b in prot_body if b.get("name")]
        # Ensure the default branch is always listed as protected
        if default_branch and default_branch not in protected:
            protected.insert(0, default_branch)
        return {
            "defaultBranch": default_branch,
            "protectedBranches": protected,
        }, "ok"

    def get_branch_rules(self, owner: str, repo: str) -> tuple[dict, str]:
        """Return branch protection rules, combining API data with local policy defaults."""
        from common.task_permissions import _DEFAULT_PROTECTED_BRANCH_PATTERNS

        # Try fetching branch protection for the default branch
        repo_info, _ = self.get_repo(owner, repo)
        default_branch = repo_info.get("defaultBranch", "main") if isinstance(repo_info, dict) else "main"
        prot_status, prot_body = self._request(
            "GET", f"repos/{owner}/{repo}/branches/{quote(default_branch)}/protection"
        )
        api_rules: dict = {}
        if prot_status == 200 and isinstance(prot_body, dict):
            api_rules = {
                "requirePRReviews": bool(prot_body.get("required_pull_request_reviews")),
                "dismissStaleReviews": (
                    (prot_body.get("required_pull_request_reviews") or {})
                    .get("dismiss_stale_reviews", False)
                ),
                "requireStatusChecks": bool(prot_body.get("required_status_checks")),
                "enforceAdmins": (prot_body.get("enforce_admins") or {}).get("enabled", False),
                "restrictPushToTeams": bool(prot_body.get("restrictions")),
            }
        rules = [
            {
                "pattern": p,
                "description": "Protected branch — no direct push allowed",
                "source": "local_policy",
            }
            for p in _DEFAULT_PROTECTED_BRANCH_PATTERNS
        ]
        return {
            "defaultBranch": default_branch,
            "localProtectedPatterns": _DEFAULT_PROTECTED_BRANCH_PATTERNS,
            "apiProtectionRules": api_rules,
            "rules": rules,
            "source": "github_api+local_policy",
        }, "ok"
