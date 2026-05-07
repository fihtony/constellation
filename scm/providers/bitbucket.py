"""Bitbucket Server REST API provider for the SCM agent."""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from common.env_utils import build_isolated_git_env
from scm.providers.base import SCMProvider

JIRA_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")


class BitbucketProvider(SCMProvider):
    """SCM provider that talks to a self-hosted Bitbucket Server REST API."""

    def __init__(
        self,
        base_url: str = "",
        token: str = "",
        username: str = "",
        auth_mode: str = "auto",
        default_project: str = "",
        ca_bundle: str = "",
        author_name: str = "SCM Agent",
        author_email: str = "scm-agent@local",
    ):
        # Derive REST API root from base_url
        # e.g. https://bitbucket.example.com/projects/MYPROJ
        #   -> https://bitbucket.example.com/rest/api/1.0
        host = base_url.split("/projects/")[0].rstrip("/") if "/projects/" in base_url else base_url.rstrip("/")
        self._host = host
        self._rest_api = f"{host}/rest/api/1.0"
        self._token = token.strip()
        self._username = username.strip()
        self._auth_mode = auth_mode.strip().lower()
        self._default_project = default_project.strip()
        self._ca_bundle = ca_bundle
        self._author_name = author_name
        self._author_email = author_email

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ssl_ctx(self):
        ctx = ssl.create_default_context()
        if self._ca_bundle and os.path.isfile(self._ca_bundle):
            ctx.load_verify_locations(self._ca_bundle)
        return ctx

    def _auth_header(self) -> str | None:
        token = self._token
        if not token:
            return None
        if token.lower().startswith(("basic ", "bearer ")):
            return token
        use_basic = self._auth_mode == "basic" or (
            self._auth_mode == "auto" and bool(self._username)
        )
        if use_basic:
            if not self._username:
                return None
            encoded = base64.b64encode(f"{self._username}:{token}".encode()).decode("ascii")
            return f"Basic {encoded}"
        return f"Bearer {token}"

    def _request(
        self, method: str, path: str, payload: dict | None = None, timeout: int = 20,
        _retries: int = 2,
    ) -> tuple[int, dict]:
        url = f"{self._rest_api.rstrip('/')}/{path.lstrip('/')}"
        headers = {"Accept": "application/json"}
        auth = self._auth_header()
        if auth:
            headers["Authorization"] = auth
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(url, data=data, headers=headers, method=method)
        last_status, last_body = 0, {}
        for attempt in range(1 + _retries):
            if attempt > 0:
                import time as _time
                _time.sleep(2 ** attempt)  # 2s, 4s back-off
            try:
                with urlopen(req, timeout=timeout, context=self._ssl_ctx()) as resp:
                    raw = resp.read().decode("utf-8")
                    return resp.status, json.loads(raw) if raw.strip() else {}
            except HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                try:
                    last_status, last_body = exc.code, json.loads(raw)
                except Exception:
                    last_status, last_body = exc.code, {"error": raw[:500]}
                if exc.code < 500 or attempt == _retries:
                    return last_status, last_body
                # 5xx — retry
                continue
            except URLError as exc:
                last_status, last_body = 0, {"error": str(exc)}
                if attempt == _retries:
                    return last_status, last_body
                continue
        return last_status, last_body

    def _default_branch(self, project: str, repo: str) -> str:
        status, body = self._request("GET", f"projects/{project}/repos/{quote(repo)}/branches/default")
        if status == 200:
            return body.get("displayId", "develop")
        return "develop"

    # ------------------------------------------------------------------
    # Repository discovery
    # ------------------------------------------------------------------

    def search_repos(self, query: str, limit: int = 10) -> tuple[list[dict], str]:
        project = self._default_project
        if not project:
            return [], "missing_default_project"
        status, body = self._request(
            "GET", f"projects/{project}/repos?limit=100"
        )
        if status != 200:
            return [], f"error_{status}"
        repos = body.get("values", [])
        # Simple keyword match
        tokens = [t.lower() for t in query.split() if t]
        scored = []
        for r in repos:
            name = r.get("slug", "").lower()
            desc = r.get("description", "").lower()
            score = sum(1 for t in tokens if t in name or t in desc)
            if score > 0 or not tokens:
                scored.append((score, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [self._normalize_repo(r, project) for _, r in scored[:limit]], "ok"

    def get_repo(self, owner: str, repo: str) -> tuple[dict, str]:
        project = owner or self._default_project
        status, body = self._request("GET", f"projects/{project}/repos/{quote(repo)}")
        if status == 200:
            return self._normalize_repo(body, project), "ok"
        return body, f"error_{status}"

    def _normalize_repo(self, r: dict, project: str) -> dict:
        slug = r.get("slug", "")
        clone_links = {
            link.get("name", ""): link.get("href", "")
            for link in r.get("links", {}).get("clone", [])
        }
        clone_url = clone_links.get("http", clone_links.get("https", ""))
        browse_url = next(
            (link["href"] for link in r.get("links", {}).get("self", []) if link.get("href")),
            f"{self._host}/projects/{project}/repos/{slug}/browse",
        )
        return {
            "provider": "bitbucket",
            "owner": project,
            "repo": slug,
            "fullName": f"{project}/{slug}",
            "description": r.get("description", ""),
            "defaultBranch": "",  # fetched on demand
            "cloneUrl": clone_url,
            "htmlUrl": browse_url,
            "private": r.get("public", True) is False,
            "language": "",
        }

    # ------------------------------------------------------------------
    # Branches
    # ------------------------------------------------------------------

    def list_branches(self, owner: str, repo: str) -> tuple[list[dict], str]:
        project = owner or self._default_project
        status, body = self._request(
            "GET", f"projects/{project}/repos/{quote(repo)}/branches?limit=50"
        )
        if status == 200:
            default = self._default_branch(project, repo)
            branches = [
                {
                    "name": b.get("displayId", ""),
                    "sha": (b.get("latestCommit") or ""),
                    "default": b.get("displayId") == default,
                }
                for b in body.get("values", [])
            ]
            return branches, "ok"
        return [], f"error_{status}"

    def create_branch(self, owner: str, repo: str, branch: str, from_ref: str) -> tuple[dict, str]:
        project = owner or self._default_project
        status, body = self._request(
            "POST",
            f"projects/{project}/repos/{quote(repo)}/branches",
            {"name": branch, "startPoint": from_ref},
        )
        if status in (200, 201):
            return {
                "name": branch,
                "sha": body.get("latestCommit", ""),
                "htmlUrl": f"{self._host}/projects/{project}/repos/{repo}/browse?at=refs/heads/{branch}",
            }, "created"
        return body, f"create_failed_{status}"

    # ------------------------------------------------------------------
    # Pull requests
    # ------------------------------------------------------------------

    def list_prs(self, owner: str, repo: str, state: str = "open") -> tuple[list[dict], str]:
        project = owner or self._default_project
        bb_state = "OPEN" if state.lower() == "open" else "MERGED" if state.lower() in ("merged", "closed") else "ALL"
        status, body = self._request(
            "GET",
            f"projects/{project}/repos/{quote(repo)}/pull-requests?state={bb_state}&limit=50",
        )
        if status == 200:
            return [self._normalize_pr(pr, project, repo) for pr in body.get("values", [])], "ok"
        return [], f"error_{status}"

    def get_pr(self, owner: str, repo: str, pr_id: int | str) -> tuple[dict, str]:
        project = owner or self._default_project
        status, body = self._request(
            "GET", f"projects/{project}/repos/{quote(repo)}/pull-requests/{int(pr_id)}"
        )
        if status == 200:
            return self._normalize_pr(body, project, repo), "ok"
        return body, f"error_{status}"

    def create_pr(
        self,
        owner: str,
        repo: str,
        from_branch: str,
        to_branch: str,
        title: str,
        description: str = "",
    ) -> tuple[dict, str]:
        project = owner or self._default_project
        payload = {
            "title": title,
            "description": description,
            "fromRef": {"id": f"refs/heads/{from_branch}", "repository": {"slug": repo, "project": {"key": project}}},
            "toRef": {"id": f"refs/heads/{to_branch}", "repository": {"slug": repo, "project": {"key": project}}},
            "reviewers": [],
        }
        status, body = self._request(
            "POST", f"projects/{project}/repos/{quote(repo)}/pull-requests", payload
        )
        if status in (200, 201):
            return self._normalize_pr(body, project, repo), "created"
        # Bitbucket Server returns 409 when a PR for this source branch already exists.
        # The body contains {"errors": [{"existingPullRequest": {...}}]} — extract it.
        if status == 409:
            errors = body.get("errors") or [] if isinstance(body, dict) else []
            for err in errors:
                existing = err.get("existingPullRequest") or {}
                if existing:
                    return self._normalize_pr(existing, project, repo), "already_exists"
        return body, f"create_failed_{status}"

    def _normalize_pr(self, pr: dict, project: str, repo: str) -> dict:
        from_ref = pr.get("fromRef") or {}
        to_ref = pr.get("toRef") or {}
        author = (pr.get("author") or {}).get("user") or {}
        pr_id = pr.get("id", "")
        linked = JIRA_KEY_RE.findall(
            f"{pr.get('title', '')} {pr.get('description', '')} {from_ref.get('displayId', '')}"
        )
        links = pr.get("links") or {}
        self_links = links.get("self") or []
        html_url = self_links[0].get("href", "") if self_links else \
            f"{self._host}/projects/{project}/repos/{repo}/pull-requests/{pr_id}"
        return {
            "provider": "bitbucket",
            "id": pr_id,
            "title": pr.get("title", ""),
            "description": pr.get("description", ""),
            "state": pr.get("state", "").lower(),
            "fromBranch": from_ref.get("displayId", ""),
            "toBranch": to_ref.get("displayId", ""),
            "htmlUrl": html_url,
            "cloneUrl": self._git_clone_url(project, repo),
            "linkedJiraIssues": list(dict.fromkeys(linked)),
            "author": author.get("displayName", ""),
            "createdAt": str(pr.get("createdDate", "")),
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
        project = owner or self._default_project
        payload: dict = {"text": text}
        if file_path and line is not None:
            payload["anchor"] = {
                "line": line,
                "lineType": "CONTEXT",
                "fileType": "TO",
                "path": file_path,
            }
        status, body = self._request(
            "POST",
            f"projects/{project}/repos/{quote(repo)}/pull-requests/{int(pr_id)}/comments",
            payload,
        )
        if status in (200, 201):
            return {"id": body.get("id"), "text": body.get("text", "")}, "created"
        return body, f"create_failed_{status}"

    def list_pr_comments(self, owner: str, repo: str, pr_id: int | str) -> tuple[list[dict], str]:
        project = owner or self._default_project
        status, body = self._request(
            "GET",
            f"projects/{project}/repos/{quote(repo)}/pull-requests/{int(pr_id)}/activities?limit=100",
        )
        if status != 200:
            return [], f"error_{status}"
        comments = []
        for activity in body.get("values", []):
            if activity.get("action") != "COMMENTED":
                continue
            comment = activity.get("comment") or {}
            author = (comment.get("author") or {})
            comments.append({
                "id": comment.get("id"),
                "body": comment.get("text", ""),
                "author": author.get("displayName", ""),
            })
        return comments, "ok"

    # ------------------------------------------------------------------
    # Git operations
    # ------------------------------------------------------------------

    def _git_clone_url(self, project: str, repo: str) -> str:
        return f"{self._host}/scm/{project.lower()}/{repo}.git"

    def get_clone_url(self, owner: str, repo: str) -> str:
        return self._git_clone_url(owner or self._default_project, repo)

    def _git_config_args(self) -> list[str]:
        args = ["-c", "credential.helper="]
        auth = self._auth_header()
        if auth:
            args.extend(["-c", f"http.extraHeader=Authorization: {auth}"])
        if self._ca_bundle and os.path.isfile(self._ca_bundle):
            args.extend(["-c", f"http.sslCAInfo={self._ca_bundle}"])
        return args

    def _run_git(self, args: list[str], cwd: str | None = None, timeout: int = 180) -> tuple[bool, dict]:
        command = ["git", *self._git_config_args(), *args]
        completed = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            env=build_isolated_git_env(scope="scm-bitbucket"),
        )
        output = (completed.stdout or completed.stderr or "").strip()
        if completed.returncode != 0:
            return False, {"command": command, "returncode": completed.returncode, "output": output}
        return True, {"command": command, "output": output}

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
        project = owner or self._default_project
        clone_url = self._git_clone_url(project, repo)
        workspace = tempfile.mkdtemp(prefix=f"scm-push-{repo}-")
        repo_dir = os.path.join(workspace, repo)
        try:
            # Retry clone for transient 5xx server errors.
            ok, detail = False, {}
            for _clone_attempt in range(3):
                ok, detail = self._run_git(["clone", "--depth", "1", "--branch", base_branch, clone_url, repo_dir])
                if ok:
                    break
                output = detail.get("output", "")
                if "500" not in output and "RPC failed" not in output:
                    break
                import time as _time
                _time.sleep(4)
                # Remove partial clone directory before retrying
                shutil.rmtree(repo_dir, ignore_errors=True)
            if not ok:
                return detail, "clone_failed"
            self._run_git(["config", "user.name", self._author_name], cwd=repo_dir)
            self._run_git(["config", "user.email", self._author_email], cwd=repo_dir)
            ok, _ = self._run_git(["checkout", "-b", branch], cwd=repo_dir)
            if not ok:
                self._run_git(["checkout", branch], cwd=repo_dir)
            root = Path(repo_dir).resolve()
            written: list[str] = []
            for f in files or []:
                rel = f.get("path", "").lstrip("/")
                if not rel:
                    continue
                dest = (root / rel).resolve()
                if root not in dest.parents and dest != root:
                    raise ValueError(f"unsafe path: {rel}")
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_text(f.get("content", ""), encoding="utf-8")
                written.append(rel)
            deleted: list[str] = []
            for d in files_to_delete or []:
                rel = d.lstrip("/")
                dest = (root / rel).resolve()
                if root not in dest.parents:
                    continue
                if dest.is_file():
                    dest.unlink()
                    self._run_git(["rm", "--cached", "--ignore-unmatch", rel], cwd=repo_dir)
                    deleted.append(rel)
            if written:
                self._run_git(["add", "--", *written], cwd=repo_dir)
            ok, detail = self._run_git(["commit", "-m", commit_message], cwd=repo_dir)
            if not ok:
                return detail, "commit_failed"
            # Retry push up to 3 times to handle transient 5xx server errors.
            ok, detail = False, {}
            for _push_attempt in range(3):
                ok, detail = self._run_git(["push", "--force", "-u", "origin", branch], cwd=repo_dir)
                if ok:
                    break
                output = detail.get("output", "")
                if "500" not in output and "RPC failed" not in output:
                    break
                import time as _time
                _time.sleep(4)
            if not ok:
                return detail, "push_failed"
            return {
                "branch": branch,
                "message": commit_message,
                "htmlUrl": f"{self._host}/projects/{project}/repos/{repo}/browse?at=refs/heads/{branch}",
                "files": written,
                "deletedFiles": deleted,
            }, "pushed"
        except Exception as exc:
            return {"error": str(exc)}, "push_failed"
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    # ------------------------------------------------------------------
    # Provider identity
    # ------------------------------------------------------------------

    @property
    def provider_name(self) -> str:
        return "bitbucket"

    # ------------------------------------------------------------------
    # Remote read operations (stubs — Bitbucket Server REST API support
    # is limited; these return not_supported so callers can fall back
    # to clone-based access or the LLM dispatch path).
    # ------------------------------------------------------------------

    def read_remote_file(
        self, owner: str, repo: str, path: str, ref: str = ""
    ) -> tuple[str, str]:
        """Read file via Bitbucket Server raw-content endpoint."""
        project = owner or self._default_project
        branch_or_commit = ref or "HEAD"
        # Bitbucket Server: GET /rest/api/1.0/projects/{K}/repos/{R}/raw/{path}?at={ref}
        endpoint = f"projects/{project}/repos/{quote(repo)}/raw/{path.lstrip('/')}?at={quote(branch_or_commit)}"
        url = f"{self._rest_api.rstrip('/')}/{endpoint}"
        headers: dict[str, str] = {}
        auth = self._auth_header()
        if auth:
            headers["Authorization"] = auth
        import time as _time
        for _attempt in range(3):
            req = Request(url, headers=headers)
            try:
                with urlopen(req, timeout=20, context=self._ssl_ctx()) as resp:
                    raw = resp.read()
                    return raw.decode("utf-8", errors="replace"), "ok"
            except HTTPError as exc:
                if exc.code < 500 or _attempt == 2:
                    return "", f"error_{exc.code}"
                _time.sleep(4)
                continue
            except URLError as exc:
                return "", f"url_error: {exc}"
        return "", "error_500"

    def list_remote_dir(
        self, owner: str, repo: str, path: str = "", ref: str = ""
    ) -> tuple[list[dict], str]:
        """List directory entries via Bitbucket Server files endpoint."""
        project = owner or self._default_project
        base_path = path.lstrip("/") if path else ""
        endpoint = f"projects/{project}/repos/{quote(repo)}/files/{base_path}" if base_path else \
            f"projects/{project}/repos/{quote(repo)}/files"
        if ref:
            endpoint += f"?at={quote(ref)}&limit=200"
        else:
            endpoint += "?limit=200"
        status, body = self._request("GET", endpoint)
        if status != 200:
            return [], f"error_{status}"
        values = body.get("values") or []
        entries = [
            {
                "name": entry if isinstance(entry, str) else entry.get("name", ""),
                "path": f"{base_path}/{entry}".lstrip("/") if isinstance(entry, str)
                        else entry.get("path", ""),
                "type": "unknown",
                "size": 0,
                "htmlUrl": "",
            }
            for entry in values
        ]
        return entries, "ok"

    def search_code(
        self, owner: str, repo: str, query: str, limit: int = 20
    ) -> tuple[list[dict], str]:
        """Code search is not natively supported on Bitbucket Server REST API."""
        return [], "not_supported"

    def compare_refs(
        self,
        owner: str,
        repo: str,
        base: str,
        head: str,
        stat_only: bool = False,
    ) -> tuple[dict, str]:
        """Compare two refs via Bitbucket Server compare endpoint (stat only)."""
        project = owner or self._default_project
        endpoint = (
            f"projects/{project}/repos/{quote(repo)}/compare/commits"
            f"?from={quote(head)}&to={quote(base)}&limit=100"
        )
        status, body = self._request("GET", endpoint)
        if status != 200:
            return {}, f"error_{status}"
        values = body.get("values") or []
        return {
            "aheadBy": len(values),
            "behindBy": 0,
            "totalChangedFiles": 0,
            "additions": 0,
            "deletions": 0,
            "files": [],
            "diff": "",
            "status": "compared",
        }, "ok"

    def get_default_branch(self, owner: str, repo: str) -> tuple[dict, str]:
        """Return the default branch for a Bitbucket Server repository."""
        project = owner or self._default_project
        default_branch = self._default_branch(project, repo)
        return {
            "defaultBranch": default_branch,
            "protectedBranches": [default_branch],
        }, "ok"

    def get_branch_rules(self, owner: str, repo: str) -> tuple[dict, str]:
        """Return branch rules combining local policy defaults.
        Bitbucket Server branch-permissions API requires admin access.
        """
        from common.task_permissions import _DEFAULT_PROTECTED_BRANCH_PATTERNS

        project = owner or self._default_project
        default_branch = self._default_branch(project, repo)
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
            "apiProtectionRules": {},
            "rules": rules,
            "source": "local_policy",
        }, "ok"
