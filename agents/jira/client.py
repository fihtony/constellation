"""Jira Cloud REST API v3 client for the v2 boundary adapter.

Lightweight, stdlib-only implementation — no v1 imports required.
Credentials are always sourced from constructor arguments (never from env
at call time), so callers retain full control.

This client intentionally mirrors the v1 REST provider's cloud handling:
for Atlassian Cloud sites it discovers the tenant cloud ID, tries the scoped
gateway first, and falls back to the site-local REST endpoint on 401/403/404.

Jira Cloud REST API v3 docs:
  https://developer.atlassian.com/cloud/jira/platform/rest/v3/
"""
from __future__ import annotations

import base64
import json
import os
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

_TICKET_RE = re.compile(r"\b([A-Z][A-Z0-9]+-\d+)\b")
_TICKET_URL_RE = re.compile(
    r"(https?://[^\s]+/browse/([A-Z][A-Z0-9]+-\d+))", re.IGNORECASE
)

# Inline markdown patterns for ADF conversion
_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MD_CODE_RE = re.compile(r"`([^`]+)`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^\)]+)\)")
_MD_URL_RE = re.compile(r"(?<!\()https?://\S+")


def _parse_inline_markdown(text: str) -> list:
    """Parse a single line of text with inline markdown into ADF inline nodes.

    Handles: **bold**, `code`, [text](url), and bare URLs.
    Returns a list of ADF inline content nodes.
    """
    nodes: list = []
    pos = 0

    # Merge all patterns with their priorities
    pattern = re.compile(
        r"(\*\*(.+?)\*\*)"          # bold
        r"|(`([^`]+)`)"              # code
        r"|(\[([^\]]+)\]\((https?://[^\)]+)\))"  # link
        r"|(https?://\S+)"           # bare URL
    )

    for m in pattern.finditer(text):
        start, end = m.start(), m.end()
        # Flush plain text before this match
        if start > pos:
            nodes.append({"type": "text", "text": text[pos:start]})
        pos = end

        if m.group(1):  # **bold**
            nodes.append({
                "type": "text",
                "text": m.group(2),
                "marks": [{"type": "strong"}],
            })
        elif m.group(3):  # `code`
            nodes.append({
                "type": "text",
                "text": m.group(4),
                "marks": [{"type": "code"}],
            })
        elif m.group(5):  # [text](url)
            nodes.append({
                "type": "text",
                "text": m.group(6),
                "marks": [{"type": "link", "attrs": {"href": m.group(7)}}],
            })
        elif m.group(8):  # bare URL
            url = m.group(8)
            nodes.append({
                "type": "text",
                "text": url,
                "marks": [{"type": "link", "attrs": {"href": url}}],
            })

    # Flush remaining plain text
    if pos < len(text):
        nodes.append({"type": "text", "text": text[pos:]})

    return nodes or [{"type": "text", "text": text}]


def _md_to_adf(text: str) -> dict:
    """Convert a plain-text comment with inline markdown to Atlassian Document Format.

    Supports:
    - Paragraph breaks (blank lines → separate ADF paragraph nodes)
    - **bold** → strong marks
    - ``code`` → code marks
    - [link text](url) → link marks
    - Bare URLs → link marks
    - Lines that are only whitespace → empty paragraph (visual spacing)

    Returns a valid ADF document dict ready for the Jira REST API body field.
    """
    paragraphs = text.split("\n")
    content: list = []

    for line in paragraphs:
        stripped = line.strip()
        if not stripped:
            # Blank line = paragraph break (visual spacing)
            content.append({"type": "paragraph", "content": []})
            continue
        inline = _parse_inline_markdown(stripped)
        content.append({"type": "paragraph", "content": inline})

    if not content:
        content = [{"type": "paragraph", "content": [{"type": "text", "text": ""}]}]

    return {"version": 1, "type": "doc", "content": content}


def _parse_base_url_and_key(ticket_url: str) -> tuple[str, str]:
    """Parse Jira base URL and ticket key from a full browse URL.

    E.g. ``https://org.atlassian.net/browse/PROJ-123``
       → (``https://org.atlassian.net``, ``PROJ-123``)
    """
    parsed = urlparse(ticket_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path
    key = path.split("/browse/")[-1].strip("/").split("?")[0].split("#")[0]
    return base, key


class JiraClient:
    """Direct Jira Cloud REST API v3 client.

    Parameters
    ----------
    base_url:
        Jira site root, e.g. ``https://org.atlassian.net``.
    token:
        Atlassian API token (generated at id.atlassian.com).
    email:
        Atlassian account email (used for Basic auth).
    auth_mode:
        ``basic`` (default), ``bearer``, or ``auto``.
    corp_ca_bundle:
        Optional path to a corporate CA bundle for on-prem Jira.
    cloud_id:
        Optional pre-resolved Atlassian cloud ID.
    api_base_url:
        Optional explicit REST API base URL override.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        email: str = "",
        auth_mode: str = "basic",
        corp_ca_bundle: str = "",
        cloud_id: str = "",
        api_base_url: str = "",
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token.strip()
        self._email = email.strip()
        self._auth_mode = auth_mode.strip().lower()
        self._ca_bundle = corp_ca_bundle
        self._discovered_cloud_id = cloud_id.strip()
        if api_base_url:
            self._api = api_base_url.rstrip("/")
        else:
            self._api = f"{self._base}/rest/api/3"

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------

    @classmethod
    def from_ticket_url(
        cls,
        ticket_url: str,
        token: str,
        email: str = "",
        auth_mode: str = "basic",
        corp_ca_bundle: str = "",
        cloud_id: str = "",
        api_base_url: str = "",
    ) -> "JiraClient":
        """Construct a client from a full Jira browse URL."""
        base, _ = _parse_base_url_and_key(ticket_url)
        return cls(
            base,
            token,
            email,
            auth_mode,
            corp_ca_bundle,
            cloud_id=cloud_id,
            api_base_url=api_base_url,
        )

    @staticmethod
    def parse_ticket_key(ticket_url: str) -> str:
        """Extract the ticket key from a Jira browse URL."""
        _, key = _parse_base_url_and_key(ticket_url)
        return key

    # ------------------------------------------------------------------
    # High-level helpers
    # ------------------------------------------------------------------

    def fetch_ticket(self, key: str) -> tuple[dict | None, str]:
        """Fetch a single Jira issue. Returns (issue_dict | None, status)."""
        status, data = self.request("GET", f"issue/{key}")
        if status == 200:
            return data, "ok"
        return None, self._status_message(status, data)

    def search(
        self,
        jql: str,
        max_results: int = 10,
        fields: list[str] | None = None,
    ) -> tuple[dict, str]:
        """Search issues via JQL.

        Uses the newer ``/search/jql`` endpoint (migrated from the deprecated
        ``/search`` endpoint per Atlassian CHANGE-2046).
        Returns (search_result_dict, status).
        """
        params: dict = {"jql": jql, "maxResults": max_results}
        if fields:
            params["fields"] = ",".join(fields)

        status, data = self._post_search(jql, max_results, fields)
        if status == 200:
            issues = data.get("issues", []) if isinstance(data, dict) else []
            if issues and all(not issue.get("key") for issue in issues if isinstance(issue, dict)):
                expanded_issues = self._expand_issue_documents(issues)
                if expanded_issues:
                    normalized = dict(data)
                    normalized["issues"] = expanded_issues
                    return normalized, "ok"
                legacy_status, legacy_data = self.request(
                    "GET",
                    f"search?{urlencode(params)}",
                )
                if legacy_status == 200:
                    return legacy_data, "ok"
            return data, "ok"
        if status == 404:
            legacy_status, legacy_data = self.request(
                "GET",
                f"search?{urlencode(params)}",
            )
            if legacy_status == 200:
                return legacy_data, "ok"
            return {}, self._status_message(legacy_status, legacy_data)
        return {}, self._status_message(status, data)

    def _post_search(
        self,
        jql: str,
        max_results: int = 10,
        fields: list[str] | None = None,
    ) -> tuple[int, dict]:
        """POST to /search/jql (Atlassian CHANGE-2046 new endpoint)."""
        payload: dict = {"jql": jql, "maxResults": max_results}
        if fields:
            payload["fields"] = fields
        return self.request("POST", "search/jql", payload)

    def get_myself(self) -> tuple[dict, str]:
        """Return (user_dict, status) for the authenticated account."""
        status, data = self.request("GET", "myself")
        if status == 200:
            return data, "ok"
        return {}, self._status_message(status, data)

    def add_comment(self, key: str, text: str) -> tuple[dict, str]:
        """Add a rich-text comment to a ticket using Atlassian Document Format (ADF).

        Converts simple inline-markdown patterns (**bold**, `code`, [text](url))
        and paragraph breaks to proper ADF nodes so comments render visually in
        Jira Cloud instead of showing raw markdown syntax.
        """
        adf_body = _md_to_adf(text)
        status, data = self.request("POST", f"issue/{key}/comment", {"body": adf_body})
        if status in (200, 201):
            return data, "ok"
        return {}, self._status_message(status, data)

    def get_transitions(self, key: str) -> tuple[list, str]:
        """List available transitions for a ticket."""
        status, data = self.request("GET", f"issue/{key}/transitions")
        if status == 200:
            return data.get("transitions", []), "ok"
        return [], self._status_message(status, data)

    def list_comments(self, key: str, max_results: int = 50) -> tuple[list, str]:
        """List comments on a ticket."""
        status, data = self.request(
            "GET", f"issue/{key}/comment?maxResults={max_results}"
        )
        if status == 200:
            return data.get("comments", []), "ok"
        return [], self._status_message(status, data)

    def _expand_issue_documents(self, issues: list[dict]) -> list[dict]:
        """Hydrate issue-id-only search results into full issue documents."""
        expanded: list[dict] = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if issue.get("key"):
                expanded.append(issue)
                continue
            issue_id = str(issue.get("id") or "").strip()
            if not issue_id:
                continue
            status, detail = self.request("GET", f"issue/{issue_id}")
            if status == 200 and isinstance(detail, dict):
                expanded.append(detail)
        return expanded

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _status_message(status: int, body: dict) -> str:
        if status:
            return f"HTTP {status}"
        return str(body.get("error", "request_failed"))

    def _ssl_ctx(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if self._ca_bundle and os.path.isfile(self._ca_bundle):
            ctx.load_verify_locations(self._ca_bundle)
        return ctx

    @staticmethod
    def _looks_like_cloud(url: str) -> bool:
        return urlparse(url or "").netloc.lower().endswith(".atlassian.net")

    def discover_cloud_id(self) -> str:
        """Discover and cache the Atlassian Cloud ID for scoped API calls."""
        if self._discovered_cloud_id:
            return self._discovered_cloud_id
        if not self._looks_like_cloud(self._base):
            return ""

        req = Request(
            f"{self._base}/_edge/tenant_info",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urlopen(req, context=self._ssl_ctx(), timeout=10) as resp:
                raw = resp.read().decode("utf-8")
                body = json.loads(raw) if raw.strip() else {}
        except Exception:
            return ""

        cloud_id = str(body.get("cloudId") or body.get("cloudid") or "").strip()
        if cloud_id:
            self._discovered_cloud_id = cloud_id
        return self._discovered_cloud_id

    def _scoped_api_base_url(self) -> str:
        cloud_id = self.discover_cloud_id()
        if not cloud_id:
            return ""
        return f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"

    def _candidate_api_base_urls(self) -> list[str]:
        candidates: list[str] = []
        if self._looks_like_cloud(self._api):
            scoped = self._scoped_api_base_url()
            if scoped:
                candidates.append(scoped)
        if self._api and self._api not in candidates:
            candidates.append(self._api)
        return candidates

    def _auth_header(self) -> str | None:
        token = self._token
        if not token:
            return None
        if token.lower().startswith(("basic ", "bearer ")):
            return token
        use_basic = self._auth_mode == "basic" or (
            self._auth_mode == "auto" and bool(self._email)
        )
        if use_basic and self._email:
            creds = base64.b64encode(
                f"{self._email}:{token}".encode("utf-8")
            ).decode("ascii")
            return f"Basic {creds}"
        return f"Bearer {token}"

    def _request_once(
        self,
        api_base_url: str,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: int = 20,
    ) -> tuple[int, dict]:
        url = f"{api_base_url.rstrip('/')}/{path.lstrip('/')}"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
        headers: dict[str, str] = {"Accept": "application/json"}
        if data:
            headers["Content-Type"] = "application/json; charset=utf-8"
        auth = self._auth_header()
        if auth:
            headers["Authorization"] = auth
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, context=self._ssl_ctx(), timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                body = json.loads(raw) if raw.strip() else {}
                return resp.status, body
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw) if raw.strip() else {}
            except Exception:
                body = {"error": raw[:500]}
            return exc.code, body
        except URLError as exc:
            return 0, {"error": str(exc.reason)}

    def request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: int = 20,
    ) -> tuple[int, dict]:
        """Issue a Jira REST request with cloud scoped-gateway retry."""
        last_status, last_body = 0, {}
        candidates = self._candidate_api_base_urls()
        if not candidates:
            return 0, {"error": "Jira API base URL is not configured"}

        for index, api_base_url in enumerate(candidates):
            status, body = self._request_once(
                api_base_url,
                method,
                path,
                payload=payload,
                timeout=timeout,
            )
            last_status, last_body = status, body
            should_retry = (
                index == 0
                and len(candidates) > 1
                and status in (401, 403, 404)
            )
            if not should_retry:
                return status, body
        return last_status, last_body

    def _request(
        self,
        method: str,
        path: str,
        payload: dict | None = None,
        timeout: int = 20,
    ) -> dict:
        status, body = self.request(method, path.lstrip("/"), payload, timeout=timeout)
        if status in (200, 201, 204):
            return body
        raise RuntimeError(f"{self._status_message(status, body)}: {body}")

    def _get(self, path: str, timeout: int = 20) -> dict:
        return self._request("GET", path, timeout=timeout)

    def _post(self, path: str, payload: dict, timeout: int = 20) -> dict:
        return self._request("POST", path, payload, timeout=timeout)
