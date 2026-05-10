"""Jira Cloud REST API v3 client for the v2 boundary adapter.

Lightweight, stdlib-only implementation — no v1 imports required.
Credentials are always sourced from constructor arguments (never from env
at call time), so callers retain full control.

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


def _parse_base_url_and_key(ticket_url: str) -> tuple[str, str]:
    """Parse Jira base URL and ticket key from a full browse URL.

    E.g. ``https://org.atlassian.net/browse/PROJ-123``
       → (``https://org.atlassian.net``, ``PROJ-123``)
    """
    parsed = urlparse(ticket_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path  # /browse/PROJ-123
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
        ``basic`` (default) or ``bearer``.
    corp_ca_bundle:
        Optional path to a corporate CA bundle for on-prem Jira.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        email: str = "",
        auth_mode: str = "basic",
        corp_ca_bundle: str = "",
    ) -> None:
        self._base = base_url.rstrip("/")
        self._api = f"{self._base}/rest/api/3"
        self._token = token.strip()
        self._email = email.strip()
        self._auth_mode = auth_mode.strip().lower()
        self._ca_bundle = corp_ca_bundle

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
    ) -> "JiraClient":
        """Construct a client from a full Jira browse URL."""
        base, _ = _parse_base_url_and_key(ticket_url)
        return cls(base, token, email, auth_mode, corp_ca_bundle)

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
        try:
            data = self._get(f"/issue/{key}")
            return data, "ok"
        except HTTPError as exc:
            return None, f"HTTP {exc.code}"
        except Exception as exc:
            return None, str(exc)

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
        try:
            # Preferred: POST to /issue/search/jql (new API, CHANGE-2046)
            data = self._post_search(jql, max_results, fields)
            return data, "ok"
        except HTTPError as exc:
            if exc.code == 404:
                # Fallback: try legacy GET endpoint
                try:
                    data = self._get(f"/search?{urlencode(params)}")
                    return data, "ok"
                except HTTPError as exc2:
                    return {}, f"HTTP {exc2.code}"
            return {}, f"HTTP {exc.code}"
        except Exception as exc:
            return {}, str(exc)

    def _post_search(
        self,
        jql: str,
        max_results: int = 10,
        fields: list[str] | None = None,
    ) -> dict:
        """POST to /search/jql (Atlassian CHANGE-2046 new endpoint)."""
        payload: dict = {"jql": jql, "maxResults": max_results}
        if fields:
            payload["fields"] = fields
        return self._post("/search/jql", payload)

    def get_myself(self) -> tuple[dict, str]:
        """Return (user_dict, status) for the authenticated account."""
        try:
            data = self._get("/myself")
            return data, "ok"
        except HTTPError as exc:
            return {}, f"HTTP {exc.code}"
        except Exception as exc:
            return {}, str(exc)

    def add_comment(
        self, key: str, text: str
    ) -> tuple[dict, str]:
        """Add a plain-text comment to a ticket.

        Uses Atlassian Document Format (ADF) so the comment renders on cloud.
        """
        adf_body = {
            "version": 1,
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": text}],
                }
            ],
        }
        try:
            data = self._post(f"/issue/{key}/comment", {"body": adf_body})
            return data, "ok"
        except HTTPError as exc:
            return {}, f"HTTP {exc.code}"
        except Exception as exc:
            return {}, str(exc)

    def get_transitions(self, key: str) -> tuple[list, str]:
        """List available transitions for a ticket."""
        try:
            data = self._get(f"/issue/{key}/transitions")
            return data.get("transitions", []), "ok"
        except HTTPError as exc:
            return [], f"HTTP {exc.code}"
        except Exception as exc:
            return [], str(exc)

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
        if self._auth_mode == "basic" and self._email:
            creds = base64.b64encode(
                f"{self._email}:{token}".encode()
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
        url = f"{self._api}{path}"
        data = json.dumps(payload, ensure_ascii=False).encode() if payload else None
        headers: dict[str, str] = {"Accept": "application/json"}
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
