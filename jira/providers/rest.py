"""Jira REST API v3 provider."""

from __future__ import annotations

import base64
import json
import os
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from jira.providers.base import JiraProvider


class JiraRESTProvider(JiraProvider):
    """Jira provider backed by the Jira REST API v3."""

    def __init__(
        self,
        jira_base_url: str,
        jira_token: str,
        jira_email: str = "",
        jira_auth_mode: str = "basic",
        jira_cloud_id: str = "",
        jira_api_base_url: str = "",
        corp_ca_bundle: str = "",
    ):
        self._jira_base_url = jira_base_url.rstrip("/")
        self._jira_token = jira_token
        self._jira_email = jira_email
        self._jira_auth_mode = jira_auth_mode.strip().lower()
        self._corp_ca_bundle = corp_ca_bundle
        self._discovered_cloud_id = jira_cloud_id.strip()

        if jira_api_base_url:
            self._jira_api_base_url = jira_api_base_url.rstrip("/")
        else:
            self._jira_api_base_url = f"{self._jira_base_url}/rest/api/3"

    # ------------------------------------------------------------------
    # SSL / auth helpers
    # ------------------------------------------------------------------

    def _ssl_ctx(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        if self._corp_ca_bundle and os.path.isfile(self._corp_ca_bundle):
            ctx.load_verify_locations(self._corp_ca_bundle)
        return ctx

    def _auth_header(self) -> str | None:
        token = (self._jira_token or "").strip()
        if not token:
            return None
        if token.lower().startswith(("basic ", "bearer ")):
            return token
        use_basic = self._jira_auth_mode == "basic" or (
            self._jira_auth_mode == "auto" and bool(self._jira_email.strip())
        )
        if use_basic:
            user = self._jira_email.strip()
            if not user:
                return None
            encoded = base64.b64encode(f"{user}:{token}".encode("utf-8")).decode("ascii")
            return f"Basic {encoded}"
        return f"Bearer {token}"

    # ------------------------------------------------------------------
    # Cloud ID discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_like_cloud(url: str) -> bool:
        return urlparse(url or "").netloc.lower().endswith(".atlassian.net")

    def discover_cloud_id(self) -> str:
        if self._discovered_cloud_id:
            return self._discovered_cloud_id
        if not self._looks_like_cloud(self._jira_base_url):
            return ""
        req = Request(
            f"{self._jira_base_url}/_edge/tenant_info",
            headers={"Accept": "application/json"},
            method="GET",
        )
        try:
            with urlopen(req, timeout=10, context=self._ssl_ctx()) as resp:
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

    def candidate_api_base_urls(self) -> list[str]:
        primary = self._jira_api_base_url
        candidates: list[str] = []
        if self._looks_like_cloud(primary):
            scoped = self._scoped_api_base_url()
            if scoped:
                candidates.append(scoped)
        if primary and primary not in candidates:
            candidates.append(primary)
        return candidates

    # ------------------------------------------------------------------
    # Raw HTTP request
    # ------------------------------------------------------------------

    def _request_once(
        self, api_base_url: str, method: str, path: str, payload=None
    ) -> tuple[int, dict]:
        url = f"{api_base_url.rstrip('/')}/{path.lstrip('/')}"
        headers: dict = {"Accept": "application/json"}
        auth = self._auth_header()
        if auth:
            headers["Authorization"] = auth
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=20, context=self._ssl_ctx()) as resp:
                raw = resp.read().decode("utf-8")
                body = json.loads(raw) if raw.strip() else {}
                return resp.status, body
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw)
            except Exception:
                body = {"error": raw[:500]}
            return exc.code, body
        except URLError as exc:
            return 0, {"error": str(exc.reason)}

    def request(self, method: str, path: str, payload=None) -> tuple[int, dict]:
        """Generic Jira REST API call with scoped-gateway retry."""
        last_status, last_body = 0, {}
        candidates = self.candidate_api_base_urls()
        for index, api_base_url in enumerate(candidates):
            status, body = self._request_once(api_base_url, method, path, payload)
            last_status, last_body = status, body
            should_retry = (
                index == 0
                and len(candidates) > 1
                and status in (401, 403, 404)
            )
            if not should_retry:
                return status, body
        return last_status, last_body

    # ------------------------------------------------------------------
    # ADF / field helpers
    # ------------------------------------------------------------------

    @staticmethod
    def text_to_adf(text: str) -> dict:
        value = str(text or "").strip()
        if not value:
            return {"type": "doc", "version": 1, "content": []}
        return {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": value}],
                }
            ],
        }

    @staticmethod
    def normalize_fields(fields: dict) -> dict:
        normalized = dict(fields) if isinstance(fields, dict) else {}
        if isinstance(normalized.get("description"), str):
            normalized["description"] = JiraRESTProvider.text_to_adf(
                normalized["description"]
            )
        return normalized

    # ------------------------------------------------------------------
    # JiraProvider interface
    # ------------------------------------------------------------------

    def get_myself(self) -> tuple[dict, str]:
        status, body = self.request("GET", "myself")
        if status == 200:
            return body, "ok"
        return body, f"error_{status}"

    def fetch_issue(self, ticket_key: str) -> tuple[dict | None, str]:
        if not ticket_key:
            return None, "no_ticket_key"
        status, body = self.request("GET", f"issue/{ticket_key}")
        if status == 200:
            return body, "fetched"
        return body, "fetch_failed"

    def search_issues(
        self, jql: str, max_results: int = 10, fields: list | None = None
    ) -> tuple[dict, str]:
        if not jql:
            return {"error": "missing_jql"}, "missing_jql"
        payload: dict = {
            "jql": jql,
            "maxResults": max(1, min(int(max_results or 10), 100)),
        }
        if fields:
            payload["fields"] = fields
        status, body = self.request("POST", "search/jql", payload)
        if status == 200:
            return body, "ok"
        return body, f"error_{status}"

    def get_transitions(self, ticket_key: str) -> tuple[list, str]:
        if not ticket_key:
            return [], "no_ticket_key"
        status, body = self.request("GET", f"issue/{ticket_key}/transitions")
        if status == 200:
            return body.get("transitions", []), "ok"
        return [], f"error_{status}"

    def transition_issue(
        self, ticket_key: str, transition_name: str
    ) -> tuple[str | None, str]:
        transitions, result = self.get_transitions(ticket_key)
        if result != "ok":
            return None, f"could_not_fetch_transitions: {result}"
        target_lower = transition_name.strip().lower()
        match = None
        for t in transitions:
            if not isinstance(t, dict):
                continue
            name = t.get("name", "")
            if name.lower() == target_lower or name.lower().startswith(target_lower):
                match = t
                break
        if not match:
            available = [t.get("name") for t in transitions if isinstance(t, dict)]
            return None, f"transition_not_found (available: {available})"
        tid = match.get("id")
        if not tid:
            return None, "transition_missing_id"
        transition_label = match.get("name", transition_name)
        status, body = self.request(
            "POST",
            f"issue/{ticket_key}/transitions",
            {"transition": {"id": tid}},
        )
        if status in (200, 204):
            return tid, f"transitioned_to:{transition_label}"
        return None, f"transition_failed_{status}"

    def create_issue(
        self,
        project_key: str,
        summary: str,
        issue_type: str,
        description: str = "",
        fields: dict | None = None,
    ) -> tuple[dict, str]:
        payload_fields = self.normalize_fields(fields or {})
        payload_fields.setdefault("project", {"key": project_key})
        payload_fields.setdefault("summary", summary)
        payload_fields.setdefault("issuetype", {"name": issue_type})
        if description and "description" not in payload_fields:
            payload_fields["description"] = self.text_to_adf(description)
        status, body = self.request("POST", "issue", {"fields": payload_fields})
        if status == 201:
            return body, "created"
        return body, f"create_failed_{status}"

    def update_issue_fields(
        self, ticket_key: str, fields: dict
    ) -> tuple[dict | None, str]:
        payload_fields = self.normalize_fields(fields)
        if not payload_fields:
            return None, "missing_fields"
        status, body = self.request(
            "PUT", f"issue/{ticket_key}", {"fields": payload_fields}
        )
        if status in (200, 204):
            return {"ticketKey": ticket_key}, "updated"
        return body, f"update_failed_{status}"

    def change_assignee(
        self, ticket_key: str, account_id: str | None
    ) -> tuple[str | None, str]:
        payload = {"accountId": account_id}
        status, body = self.request("PUT", f"issue/{ticket_key}/assignee", payload)
        if status in (200, 204):
            return account_id, "assigned"
        return None, f"assignee_failed_{status}"

    def add_comment(
        self, ticket_key: str, text: str, adf_body: dict | None = None
    ) -> tuple[str | None, str]:
        if adf_body and isinstance(adf_body, dict):
            body_content = adf_body
        else:
            body_content = {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": str(text or "")}],
                    }
                ],
            }
        status, body = self.request(
            "POST", f"issue/{ticket_key}/comment", {"body": body_content}
        )
        if status == 201:
            return body.get("id", ""), "added"
        return None, f"add_failed_{status}"

    def update_comment(
        self,
        ticket_key: str,
        comment_id: str,
        new_text: str,
        adf_body: dict | None = None,
    ) -> tuple[str | None, str]:
        if adf_body and isinstance(adf_body, dict):
            body_content = adf_body
        else:
            body_content = {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": str(new_text or "")}],
                    }
                ],
            }
        status, body = self.request(
            "PUT",
            f"issue/{ticket_key}/comment/{comment_id}",
            {"body": body_content},
        )
        if status == 200:
            return comment_id, "updated"
        return None, f"update_failed_{status}"

    def delete_comment(
        self, ticket_key: str, comment_id: str
    ) -> tuple[str | None, str]:
        status, body = self.request(
            "DELETE", f"issue/{ticket_key}/comment/{comment_id}"
        )
        if status in (200, 204):
            return comment_id, "deleted"
        return None, f"delete_failed_{status}"

    @property
    def backend_name(self) -> str:
        return "rest"
