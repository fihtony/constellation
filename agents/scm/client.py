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
            import warnings
            warnings.warn(
                "SCM_BACKEND=github-mcp: full MCP support not yet available in v2 adapter; "
                "falling back to github-rest.",
                stacklevel=2,
            )
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

