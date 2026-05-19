"""SCM REST client for the v2 boundary adapter.

Supports three backends, selected via SCM_BACKEND env var or auto-detected:
  bitbucket   — Bitbucket Server REST 1.0 (default)
  github-rest — GitHub REST API v3
  github-mcp  — GitHub MCP server over HTTP (requires SCM_MCP_URL, advanced)

Auto-detection from SCM_BASE_URL (when SCM_BACKEND is not set):
  bitbucket.* host → bitbucket
  github.com host  → github-rest

Credentials are always sourced from constructor arguments.
"""
from __future__ import annotations

import base64
import json
import os
import re
import ssl
from urllib.error import HTTPError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

_BITBUCKET_HOST_RE = re.compile(r"bitbucket", re.IGNORECASE)
_GITHUB_HOST_RE = re.compile(r"github\.com", re.IGNORECASE)

GITHUB_API_BASE = "https://api.github.com"


def _detect_provider(base_url: str) -> str:
    """Auto-detect SCM provider from base URL."""
    host = urlparse(base_url).netloc.lower()
    if "bitbucket" in host:
        return "bitbucket"
    if "github.com" in host:
        return "github-rest"
    return "bitbucket"  # default for self-hosted


def _parse_bb_project_repo(repo_url: str) -> tuple[str, str, str]:
    """Parse host, project key, and repo slug from a Bitbucket Server browse URL.

    Supports both project repos and personal (user) repos:
      ``https://bitbucket.corp.com/projects/PROJ/repos/my-repo/browse``
        → (``https://bitbucket.corp.com``, ``PROJ``, ``my-repo``)
      ``https://bitbucket.corp.com/users/jdoe/repos/my-repo/browse``
        → (``https://bitbucket.corp.com``, ``~jdoe``, ``my-repo``)

    The ``~username`` notation is the Bitbucket Server convention for personal
    project keys in REST API calls.
    """
    parsed = urlparse(repo_url)
    host = f"{parsed.scheme}://{parsed.netloc}"
    parts = parsed.path.strip("/").split("/")

    project = ""
    repo = ""

    # Try /projects/<KEY>/repos/<slug> first
    try:
        proj_idx = parts.index("projects")
        project = parts[proj_idx + 1]
        repos_idx = parts.index("repos")
        repo = parts[repos_idx + 1]
        return host, project, repo
    except (ValueError, IndexError):
        pass

    # Try /users/<username>/repos/<slug> (personal repos)
    try:
        user_idx = parts.index("users")
        username = parts[user_idx + 1]
        repos_idx = parts.index("repos")
        repo = parts[repos_idx + 1]
        project = f"~{username}"
        return host, project, repo
    except (ValueError, IndexError):
        pass

    return host, project, repo


class BitbucketClient:
    """Bitbucket Server REST API 1.0 client.

    Parameters
    ----------
    base_url:
        Bitbucket Server host, e.g. ``https://bitbucket.corp.com``.
    token:
        Personal access token (HTTP Bearer) or password (HTTP Basic).
    username:
        Bitbucket username (required for Basic auth).
    auth_mode:
        ``auto`` → Bearer if username absent, Basic otherwise.
    default_project:
        Default project key used when project is not specified.
    ca_bundle:
        Optional path to corporate CA bundle.
    """

    def __init__(
        self,
        base_url: str,
        token: str = "",
        username: str = "",
        auth_mode: str = "auto",
        default_project: str = "",
        ca_bundle: str = "",
    ) -> None:
        # Strip /projects/... suffix if a full repo URL was passed
        host = base_url.split("/projects/")[0].rstrip("/") if "/projects/" in base_url else base_url.rstrip("/")
        self._host = host
        self._rest = f"{host}/rest/api/1.0"
        self._token = token.strip()
        self._username = username.strip()
        self._auth_mode = auth_mode.strip().lower()
        self._default_project = default_project.strip()
        self._ca_bundle = ca_bundle

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def from_repo_url(
        cls,
        repo_url: str,
        token: str,
        username: str = "",
        auth_mode: str = "auto",
        ca_bundle: str = "",
    ) -> "BitbucketClient":
        """Construct from a full Bitbucket browse URL."""
        host, project, _ = _parse_bb_project_repo(repo_url)
        return cls(host, token, username, auth_mode, project, ca_bundle)

    @staticmethod
    def parse_project_repo(repo_url: str) -> tuple[str, str]:
        """Return (project, repo) from a Bitbucket browse URL."""
        _, project, repo = _parse_bb_project_repo(repo_url)
        return project, repo

    # ------------------------------------------------------------------
    # High-level helpers
    # ------------------------------------------------------------------

    def get_repo(
        self, project: str, repo: str, timeout: int = 20
    ) -> tuple[dict, str]:
        """Fetch repository metadata."""
        try:
            data = self._get(
                f"/projects/{project}/repos/{repo}", timeout=timeout
            )
            return data, "ok"
        except HTTPError as exc:
            return {}, f"HTTP {exc.code}"
        except Exception as exc:
            return {}, str(exc)

    def list_branches(
        self, project: str, repo: str, timeout: int = 20
    ) -> tuple[list[dict], str]:
        """List branches. Returns ([{id, displayId, latestCommit, isDefault}, ...], status)."""
        try:
            data = self._get(
                f"/projects/{project}/repos/{repo}/branches?limit=50",
                timeout=timeout,
            )
            return data.get("values", []), "ok"
        except HTTPError as exc:
            return [], f"HTTP {exc.code}"
        except Exception as exc:
            return [], str(exc)

    def create_branch(
        self,
        project: str,
        repo: str,
        branch: str,
        from_ref: str,
        timeout: int = 20,
    ) -> tuple[dict, str]:
        """Create a new branch."""
        try:
            data = self._post(
                f"/projects/{project}/repos/{repo}/branches",
                {"name": branch, "startPoint": from_ref},
                timeout=timeout,
            )
            return data, "ok"
        except HTTPError as exc:
            return {}, f"HTTP {exc.code}"
        except Exception as exc:
            return {}, str(exc)

    def list_prs(
        self, project: str, repo: str, state: str = "open", timeout: int = 20
    ) -> tuple[list[dict], str]:
        """List pull requests."""
        try:
            data = self._get(
                f"/projects/{project}/repos/{repo}/pull-requests"
                f"?state={state.upper()}&limit=25",
                timeout=timeout,
            )
            return data.get("values", []), "ok"
        except HTTPError as exc:
            return [], f"HTTP {exc.code}"
        except Exception as exc:
            return [], str(exc)

    def create_pr(
        self,
        project: str,
        repo: str,
        from_branch: str,
        to_branch: str,
        title: str,
        description: str = "",
        timeout: int = 20,
    ) -> tuple[dict, str]:
        """Create a pull request."""
        payload = {
            "title": title,
            "description": description,
            "fromRef": {
                "id": f"refs/heads/{from_branch}",
                "repository": {
                    "slug": repo,
                    "project": {"key": project},
                },
            },
            "toRef": {
                "id": f"refs/heads/{to_branch}",
                "repository": {
                    "slug": repo,
                    "project": {"key": project},
                },
            },
            "reviewers": [],
        }
        try:
            data = self._post(
                f"/projects/{project}/repos/{repo}/pull-requests",
                payload,
                timeout=timeout,
            )
            return data, "ok"
        except HTTPError as exc:
            return {}, f"HTTP {exc.code}"
        except Exception as exc:
            return {}, str(exc)

    def get_pr(
        self,
        project: str,
        repo: str,
        pr_id: str | int,
        timeout: int = 15,
    ) -> tuple[dict, str]:
        """Get a single pull request by ID."""
        try:
            data = self._get(
                f"/projects/{project}/repos/{repo}/pull-requests/{pr_id}",
                timeout=timeout,
            )
            return data, "ok"
        except HTTPError as exc:
            return {}, f"HTTP {exc.code}"
        except Exception as exc:
            return {}, str(exc)

    def add_pr_comment(
        self,
        project: str,
        repo: str,
        pr_id: str | int,
        text: str,
        timeout: int = 15,
    ) -> tuple[dict, str]:
        """Add a general comment to a pull request."""
        payload = {"text": text}
        try:
            data = self._post(
                f"/projects/{project}/repos/{repo}/pull-requests/{pr_id}/comments",
                payload,
                timeout=timeout,
            )
            return data, "ok"
        except HTTPError as exc:
            return {}, f"HTTP {exc.code}"
        except Exception as exc:
            return {}, str(exc)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ssl_ctx(self) -> ssl.SSLContext:
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
        if use_basic and self._username:
            creds = base64.b64encode(
                f"{self._username}:{token}".encode()
            ).decode("ascii")
            return f"Basic {creds}"
        return f"Bearer {token}"

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: int = 20,
    ) -> dict:
        url = f"{self._rest}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode() if payload else None
        headers: dict[str, str] = {
            "Accept": "application/json",
            "X-Atlassian-Token": "no-check",
        }
        if data:
            headers["Content-Type"] = "application/json; charset=utf-8"
        auth = self._auth_header()
        if auth:
            headers["Authorization"] = auth
        req = Request(url, data=data, headers=headers, method=method)
        with urlopen(req, context=self._ssl_ctx(), timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            if not raw.strip():
                return {}
            return json.loads(raw)

    def _get(self, path: str, timeout: int = 20) -> dict:
        return self._request("GET", path, timeout=timeout)

    def _post(self, path: str, payload: dict, timeout: int = 20) -> dict:
        return self._request("POST", path, payload, timeout=timeout)


# ---------------------------------------------------------------------------
# GitHub REST API client
# ---------------------------------------------------------------------------

class GitHubClient:
    """GitHub REST API v3 client.

    Interface-compatible with ``BitbucketClient`` so the SCM adapter can swap
    backends without code changes.

    Parameters
    ----------
    token:
        GitHub personal access token (classic or fine-grained PAT).
    """

    def __init__(self, token: str = "") -> None:
        self._token = token.strip()

    # ------------------------------------------------------------------
    # Repository helpers
    # ------------------------------------------------------------------

    def get_repo(self, owner: str, repo: str, timeout: int = 20) -> tuple[dict, str]:
        """Fetch repository metadata."""
        try:
            status, body = self._request("GET", f"repos/{owner}/{repo}", timeout=timeout)
            if status == 200:
                return self._normalize_repo(body), "ok"
            return body, f"http_{status}"
        except Exception as exc:
            return {}, str(exc)

    def list_branches(self, owner: str, repo: str, timeout: int = 20) -> tuple[list[dict], str]:
        """List branches. Returns ([{id, displayId, latestCommit, isDefault}, ...], status)."""
        try:
            status, body = self._request(
                "GET", f"repos/{owner}/{repo}/branches?per_page=50", timeout=timeout
            )
            if status == 200:
                branches = [
                    {
                        "id": f"refs/heads/{b['name']}",
                        "displayId": b["name"],
                        "latestCommit": (b.get("commit") or {}).get("sha", ""),
                        "isDefault": b["name"] in ("main", "master"),
                    }
                    for b in body
                ]
                return branches, "ok"
            return [], f"http_{status}"
        except Exception as exc:
            return [], str(exc)

    def create_branch(
        self, owner: str, repo: str, branch: str, from_ref: str, timeout: int = 20
    ) -> tuple[dict, str]:
        """Create a new branch from *from_ref* (branch name or SHA)."""
        try:
            # Resolve from_ref to a SHA if it looks like a branch name
            sha = from_ref
            if not re.fullmatch(r"[0-9a-f]{40}", from_ref, re.IGNORECASE):
                ref_status, ref_body = self._request(
                    "GET", f"repos/{owner}/{repo}/git/ref/heads/{quote(from_ref)}", timeout=timeout
                )
                if ref_status == 200:
                    sha = ref_body.get("object", {}).get("sha", from_ref)

            status, body = self._request(
                "POST",
                f"repos/{owner}/{repo}/git/refs",
                {"ref": f"refs/heads/{branch}", "sha": sha},
                timeout=timeout,
            )
            if status in (200, 201):
                return {"name": branch, "sha": sha}, "ok"
            return body, f"http_{status}"
        except Exception as exc:
            return {}, str(exc)

    def list_prs(
        self, owner: str, repo: str, state: str = "open", timeout: int = 20
    ) -> tuple[list[dict], str]:
        """List pull requests."""
        try:
            status, body = self._request(
                "GET",
                f"repos/{owner}/{repo}/pulls?state={state}&per_page=25",
                timeout=timeout,
            )
            if status == 200:
                prs = [
                    {
                        "id": pr.get("number"),
                        "title": pr.get("title"),
                        "state": pr.get("state"),
                        "fromRef": pr.get("head", {}).get("ref", ""),
                        "toRef": pr.get("base", {}).get("ref", ""),
                        "links": {"self": [{"href": pr.get("html_url", "")}]},
                    }
                    for pr in body
                ]
                return prs, "ok"
            return [], f"http_{status}"
        except Exception as exc:
            return [], str(exc)

    def create_pr(
        self,
        owner: str,
        repo: str,
        from_branch: str,
        to_branch: str,
        title: str,
        description: str = "",
        timeout: int = 20,
    ) -> tuple[dict, str]:
        """Create a pull request."""
        payload = {
            "title": title,
            "body": description,
            "head": from_branch,
            "base": to_branch,
        }
        try:
            status, body = self._request(
                "POST", f"repos/{owner}/{repo}/pulls", payload, timeout=timeout
            )
            if status in (200, 201):
                return {
                    "id": body.get("number"),
                    "title": body.get("title"),
                    "links": {"self": [{"href": body.get("html_url", "")}]},
                }, "ok"
            return body, f"http_{status}"
        except Exception as exc:
            return {}, str(exc)

    def get_pr(
        self, owner: str, repo: str, pr_id: str | int, timeout: int = 15
    ) -> tuple[dict, str]:
        """Get a single pull request by number."""
        try:
            status, body = self._request(
                "GET", f"repos/{owner}/{repo}/pulls/{pr_id}", timeout=timeout
            )
            if status == 200:
                return {
                    "id": body.get("number"),
                    "title": body.get("title"),
                    "state": body.get("state"),
                    "links": {"self": [{"href": body.get("html_url", "")}]},
                }, "ok"
            return {}, f"http_{status}"
        except Exception as exc:
            return {}, str(exc)

    def add_pr_comment(
        self,
        owner: str,
        repo: str,
        pr_id: str | int,
        text: str,
        timeout: int = 15,
    ) -> tuple[dict, str]:
        """Add a comment to a pull request (via issues API)."""
        try:
            status, body = self._request(
                "POST",
                f"repos/{owner}/{repo}/issues/{pr_id}/comments",
                {"body": text},
                timeout=timeout,
            )
            if status in (200, 201):
                return {"id": body.get("id"), "body": text}, "ok"
            return {}, f"http_{status}"
        except Exception as exc:
            return {}, str(exc)

    def update_pr(
        self,
        owner: str,
        repo: str,
        pr_id: str | int,
        body: str | None = None,
        title: str | None = None,
        timeout: int = 15,
    ) -> tuple[dict, str]:
        """Update a pull request (title and/or body)."""
        payload: dict = {}
        if body is not None:
            payload["body"] = body
        if title is not None:
            payload["title"] = title
        if not payload:
            return {}, "no_changes"
        try:
            status, resp = self._request(
                "PATCH", f"repos/{owner}/{repo}/pulls/{pr_id}", payload, timeout=timeout
            )
            if status in (200, 201):
                return {
                    "id": resp.get("number"),
                    "url": resp.get("html_url", ""),
                }, "ok"
            return resp, f"http_{status}"
        except Exception as exc:
            return {}, str(exc)

    # SCREENSHOT_RELEASE_TAG is the release used as a permanent image CDN.
    # Fine-grained PATs cannot use the issue-assets upload endpoint (HTTP 422
    # "Bad Size"), but the Release Assets upload endpoint works correctly with
    # raw application/octet-stream content.
    SCREENSHOT_RELEASE_TAG = "screenshot-assets"

    def _find_or_create_screenshot_release(
        self, owner: str, repo: str, timeout: int = 20
    ) -> tuple[int, str]:
        """Return (release_id, status) for the screenshot-assets release.

        Creates the release if it does not exist yet.
        """
        status, body = self._request(
            "GET", f"repos/{owner}/{repo}/releases/tags/{self.SCREENSHOT_RELEASE_TAG}",
            timeout=timeout
        )
        if status == 200 and isinstance(body, dict):
            return body.get("id", 0), "found"

        # Create the release (prerelease so it does not appear prominently)
        create_status, create_body = self._request(
            "POST", f"repos/{owner}/{repo}/releases", {
                "tag_name": self.SCREENSHOT_RELEASE_TAG,
                "name": "Screenshot Assets",
                "body": "Auto-generated release for hosting PR screenshots. Do not delete.",
                "draft": False,
                "prerelease": True,
            }, timeout=timeout
        )
        if create_status in (200, 201) and isinstance(create_body, dict):
            return create_body.get("id", 0), "created"
        return 0, f"http_{create_status}"

    def upload_issue_image(
        self,
        owner: str,
        repo: str,
        issue_number: int,
        image_path: str,
        filename: str = "",
        timeout: int = 60,
    ) -> tuple[dict, str]:
        """Upload a screenshot image as a GitHub release asset for CDN hosting.

        Uses the Release Assets upload API (``uploads.github.com/repos/{owner}/
        {repo}/releases/{id}/assets``) with ``Content-Type: application/octet-stream``.
        This approach works with fine-grained PATs and does NOT require committing
        image files to the PR branch.

        The returned ``href`` is a ``github.com/{owner}/{repo}/releases/download/
        screenshot-assets/{filename}`` URL that can be embedded in Markdown.

        The Authorization header is set but never logged per security policy §4.
        """
        import os as _os
        fname = filename or _os.path.basename(image_path)

        # Include PR/issue number as prefix to allow same-named files across PRs
        if issue_number:
            unique_fname = f"pr{issue_number}-{fname}"
        else:
            unique_fname = fname

        try:
            with open(image_path, "rb") as fh:
                file_data = fh.read()
        except OSError as exc:
            return {"error": str(exc)}, f"read_error: {exc}"

        # Step 1: Find or create the screenshot release
        release_id, rel_status = self._find_or_create_screenshot_release(
            owner, repo, timeout=20
        )
        if not release_id:
            return {"error": f"Could not find/create screenshot release: {rel_status}"}, rel_status

        # Step 2: Upload as release asset (octet-stream, no multipart)
        upload_url = (
            f"https://uploads.github.com/repos/{owner}/{repo}"
            f"/releases/{release_id}/assets?name={quote(unique_fname)}"
        )
        headers = {
            "Content-Type": "application/octet-stream",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Length": str(len(file_data)),
        }
        auth = self._auth_header()
        if auth:
            headers["Authorization"] = auth  # NOT logged (security policy §4)
        req = Request(upload_url, data=file_data, headers=headers, method="POST")
        try:
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                result = json.loads(raw) if raw.strip() else {}
                cdn_url = result.get("browser_download_url", "")
                return {"href": cdn_url, "asset_id": result.get("id", 0)}, "ok"
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                err_body = json.loads(raw)
            except Exception:
                err_body = {"error": raw[:300]}
            # Handle "already_exists" — the asset was uploaded in a previous run.
            # Fetch the existing asset URL instead of failing.
            errors = err_body.get("errors", [])
            if exc.code == 422 and any(
                e.get("code") == "already_exists" for e in errors
            ):
                existing_url = self._get_existing_release_asset_url(
                    owner, repo, release_id, unique_fname, timeout=20
                )
                if existing_url:
                    return {"href": existing_url, "asset_id": 0, "reused": True}, "ok"
            return err_body, f"http_{exc.code}"
        except Exception as exc:
            return {"error": str(exc)}, str(exc)

    def _get_existing_release_asset_url(
        self,
        owner: str,
        repo: str,
        release_id: int,
        filename: str,
        timeout: int = 15,
    ) -> str:
        """Return the browser_download_url for an existing release asset by name."""
        status, body = self._request(
            "GET", f"repos/{owner}/{repo}/releases/{release_id}/assets", timeout=timeout
        )
        if status == 200 and isinstance(body, list):
            for asset in body:
                if asset.get("name") == filename:
                    return asset.get("browser_download_url", "")
        return ""

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
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: int = 20,
    ) -> tuple[int, dict | list]:
        url = f"{GITHUB_API_BASE}/{path.lstrip('/')}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
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

    @staticmethod
    def _normalize_repo(r: dict) -> dict:
        return {
            "provider": "github",
            "owner": (r.get("owner") or {}).get("login", ""),
            "repo": r.get("name", ""),
            "slug": r.get("full_name", ""),
            "cloneUrl": r.get("clone_url", ""),
            "defaultBranch": r.get("default_branch", "main"),
            "description": r.get("description", ""),
        }


# ---------------------------------------------------------------------------
# Factory — select client from env / explicit backend argument
# ---------------------------------------------------------------------------

def create_scm_client(
    base_url: str = "",
    token: str = "",
    username: str = "",
    backend: str = "",
    auth_mode: str = "auto",
    default_project: str = "",
    ca_bundle: str = "",
) -> "BitbucketClient | GitHubClient":
    """Return the appropriate SCM client for the configured backend.

    Selection priority:
    1. Explicit *backend* argument (or ``SCM_BACKEND`` env var).
    2. Auto-detect from *base_url* hostname.
    3. Default: ``bitbucket``.

    Supported *backend* values:
    - ``bitbucket``   — Bitbucket Server REST 1.0 (default).
    - ``github-rest`` — GitHub REST API v3.
    - ``github-mcp``  — GitHub MCP (falls back to ``github-rest``; full MCP
                        support requires the ``github_mcp`` provider from v1).
    """
    resolved_backend = (
        backend
        or os.environ.get("SCM_BACKEND", "").lower().strip()
        or _detect_provider(base_url)
    )

    if resolved_backend in ("github-rest", "github", "github-mcp"):
        if resolved_backend == "github-mcp":
            from agents.scm.providers.github_mcp import GitHubMCPProvider
            return GitHubMCPProvider(token=token)
        return GitHubClient(token=token)

    # Default: Bitbucket Server
    return BitbucketClient(
        base_url=base_url,
        token=token,
        username=username,
        auth_mode=auth_mode,
        default_project=default_project,
        ca_bundle=ca_bundle,
    )

