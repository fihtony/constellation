"""SCM REST client for the v2 boundary adapter.

Supports Bitbucket Server and GitHub.  Provider is auto-detected from the
base URL (``bitbucket.*`` → Bitbucket; ``github.com`` → GitHub).

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


def _detect_provider(base_url: str) -> str:
    """Auto-detect SCM provider from base URL."""
    host = urlparse(base_url).netloc.lower()
    if "bitbucket" in host:
        return "bitbucket"
    if "github.com" in host:
        return "github"
    return "bitbucket"  # default for self-hosted


def _parse_bb_project_repo(repo_url: str) -> tuple[str, str, str]:
    """Parse host, project key, and repo slug from a Bitbucket Server browse URL.

    E.g. ``https://bitbucket.corp.com/projects/PROJ/repos/my-repo/browse``
       → (``https://bitbucket.corp.com``, ``PROJ``, ``my-repo``)
    """
    parsed = urlparse(repo_url)
    host = f"{parsed.scheme}://{parsed.netloc}"
    parts = parsed.path.strip("/").split("/")
    # Expected: projects/<KEY>/repos/<slug>[/...]
    try:
        proj_idx = parts.index("projects")
        project = parts[proj_idx + 1]
        repos_idx = parts.index("repos")
        repo = parts[repos_idx + 1]
    except (ValueError, IndexError):
        project = ""
        repo = ""
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
