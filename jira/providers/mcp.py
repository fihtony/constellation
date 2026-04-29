"""Jira provider backed by the Atlassian Rovo MCP server.

Uses https://mcp.atlassian.com/v1/mcp (MCP Streamable HTTP transport, JSON-RPC 2.0).
Authenticates via Basic auth: Authorization: Basic base64(email:token).
Every tool call requires a `cloudId` argument (Jira Cloud tenant ID).

Operations not available via MCP tools fall back to direct Jira REST API calls
using the same credentials.
"""

from __future__ import annotations

import base64
import json
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from jira.providers.base import JiraProvider
from jira.providers.rest import JiraRESTProvider

ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp"
MCP_PROTOCOL_VERSION = "2025-03-26"


# ---------------------------------------------------------------------------
# Low-level HTTP MCP session
# ---------------------------------------------------------------------------

class _AtlassianMCPSession:
    """HTTP JSON-RPC 2.0 session for the Atlassian Rovo MCP server."""

    def __init__(self, auth_header: str, url: str = ATLASSIAN_MCP_URL, timeout: int = 30):
        self._auth_header = auth_header
        self._url = url
        self._timeout = timeout
        self._req_id = 0
        self._session_id: str | None = None
        self._alive = False

    def start(self) -> None:
        """Perform MCP initialize handshake."""
        resp = self._rpc("initialize", {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "constellation-jira-agent", "version": "1.0"},
        })
        if "error" in resp and not resp.get("result"):
            raise RuntimeError(f"MCP init failed: {resp['error']}")
        # Send initialized notification (fire-and-forget)
        self._notify("notifications/initialized", {})
        self._alive = True

    def _headers(self) -> dict:
        h = {
            "Authorization": self._auth_header,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "User-Agent": "constellation-jira-agent/1.0",
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
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        try:
            data = json.dumps(payload).encode("utf-8")
            req = Request(self._url, data=data, headers=self._headers(), method="POST")
            with urlopen(req, timeout=10):
                pass
        except Exception:
            pass

    def _parse_sse(self, raw: str, expected_id: int | None) -> dict:
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

    def call(self, tool_name: str, arguments: dict, timeout: int = 30) -> dict:
        return self._rpc(
            "tools/call", {"name": tool_name, "arguments": arguments}, timeout
        )

    def tools_list(self) -> list:
        resp = self._rpc("tools/list", {})
        return (resp.get("result") or {}).get("tools", [])

    def stop(self) -> None:
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


def _parse_json(text: str):
    text = (text or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def _is_api_token_denied(text: str) -> bool:
    return "you don't have permission to connect via api token" in text.lower()


# ---------------------------------------------------------------------------
# JiraMCPProvider
# ---------------------------------------------------------------------------

class JiraMCPProvider(JiraProvider):
    """Jira provider backed by the Atlassian Rovo MCP server.

    Operations not available via MCP tools fall back to direct Jira REST API calls.
    """

    def __init__(
        self,
        jira_base_url: str,
        jira_token: str,
        jira_email: str = "",
        jira_auth_mode: str = "basic",
        jira_cloud_id: str = "",
        jira_api_base_url: str = "",
        corp_ca_bundle: str = "",
        mcp_url: str = ATLASSIAN_MCP_URL,
        timeout: int = 30,
    ):
        self._jira_token = jira_token
        self._jira_email = jira_email
        self._jira_base_url = jira_base_url
        self._mcp_url = mcp_url
        self._timeout = timeout
        self._session: _AtlassianMCPSession | None = None
        self._lock = threading.Lock()
        # Cloud ID (required for all MCP tool calls)
        self._cloud_id = jira_cloud_id.strip()
        # REST provider for fallback operations
        self._rest = JiraRESTProvider(
            jira_base_url=jira_base_url,
            jira_token=jira_token,
            jira_email=jira_email,
            jira_auth_mode=jira_auth_mode,
            jira_cloud_id=jira_cloud_id,
            jira_api_base_url=jira_api_base_url,
            corp_ca_bundle=corp_ca_bundle,
        )

    # ------------------------------------------------------------------
    # Auth / session management
    # ------------------------------------------------------------------

    def _build_auth_header(self) -> str:
        token = (self._jira_token or "").strip()
        if token.lower().startswith(("basic ", "bearer ")):
            return token
        email = (self._jira_email or "").strip()
        if email:
            encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
            return f"Basic {encoded}"
        return f"Bearer {token}"

    def _get_session(self) -> _AtlassianMCPSession:
        if self._session and self._session._alive:
            return self._session
        if self._session:
            try:
                self._session.stop()
            except Exception:
                pass
        auth = self._build_auth_header()
        self._session = _AtlassianMCPSession(auth, url=self._mcp_url, timeout=self._timeout)
        self._session.start()
        return self._session

    def _call(self, tool: str, args: dict, timeout: int = 30) -> dict:
        """Thread-safe MCP tool call with auto-reconnect on failure."""
        with self._lock:
            try:
                return self._get_session().call(tool, args, timeout)
            except Exception:
                self._session = None
                return self._get_session().call(tool, args, timeout)

    def close(self) -> None:
        with self._lock:
            if self._session:
                self._session.stop()
                self._session = None

    def get_cloud_id(self) -> str:
        if not self._cloud_id:
            self._cloud_id = self._rest.discover_cloud_id()
        return self._cloud_id

    def get_tools_list(self) -> list:
        """Return the list of MCP tools available on the server."""
        with self._lock:
            try:
                return self._get_session().tools_list()
            except Exception:
                return []

    # ------------------------------------------------------------------
    # JiraProvider interface
    # ------------------------------------------------------------------

    def get_myself(self) -> tuple[dict, str]:
        # The Atlassian Rovo MCP server does not expose a direct "get current user"
        # tool; fall back to Jira REST API.
        return self._rest.get_myself()

    def fetch_issue(self, ticket_key: str) -> tuple[dict | None, str]:
        if not ticket_key:
            return None, "no_ticket_key"
        cloud_id = self.get_cloud_id()
        if not cloud_id:
            return self._rest.fetch_issue(ticket_key)
        resp = self._call("getJiraIssue", {
            "cloudId": cloud_id,
            "issueIdOrKey": ticket_key,
        })
        if _is_error(resp):
            text = _extract_text(resp)
            if _is_api_token_denied(text):
                # MCP token auth not enabled — fallback to REST
                return self._rest.fetch_issue(ticket_key)
            return None, f"fetch_failed: {text[:150]}"
        text = _extract_text(resp)
        data = _parse_json(text)
        if isinstance(data, dict):
            return data, "fetched"
        return None, "fetch_failed"

    def search_issues(
        self, jql: str, max_results: int = 10, fields: list | None = None
    ) -> tuple[dict, str]:
        if not jql:
            return {"error": "missing_jql"}, "missing_jql"
        cloud_id = self.get_cloud_id()
        if not cloud_id:
            return self._rest.search_issues(jql, max_results, fields)
        args: dict = {
            "cloudId": cloud_id,
            "jql": jql,
            "maxResults": max(1, min(int(max_results or 10), 100)),
        }
        if fields:
            args["fields"] = fields
        resp = self._call("searchJiraIssuesUsingJql", args)
        if _is_error(resp):
            text = _extract_text(resp)
            if _is_api_token_denied(text):
                return self._rest.search_issues(jql, max_results, fields)
            return {"error": text[:200]}, f"error: {text[:100]}"
        text = _extract_text(resp)
        data = _parse_json(text)
        if isinstance(data, dict):
            return data, "ok"
        # MCP may return issues as a list directly
        if isinstance(data, list):
            return {"issues": data, "total": len(data)}, "ok"
        return {"raw": text[:500]}, "ok"

    def get_transitions(self, ticket_key: str) -> tuple[list, str]:
        # Atlassian Rovo MCP does not expose a getJiraTransitions tool;
        # fall back to REST.
        return self._rest.get_transitions(ticket_key)

    def transition_issue(
        self, ticket_key: str, transition_name: str
    ) -> tuple[str | None, str]:
        # Get available transitions via REST to resolve the name → ID
        transitions, result = self._rest.get_transitions(ticket_key)
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

        cloud_id = self.get_cloud_id()
        if not cloud_id:
            return self._rest.transition_issue(ticket_key, transition_name)

        resp = self._call("transitionJiraIssue", {
            "cloudId": cloud_id,
            "issueIdOrKey": ticket_key,
            "transition": {"id": tid},
        })
        if _is_error(resp):
            text = _extract_text(resp)
            if _is_api_token_denied(text):
                return self._rest.transition_issue(ticket_key, transition_name)
            return None, f"transition_failed: {text[:150]}"
        return tid, f"transitioned_to:{transition_label}"

    def create_issue(
        self,
        project_key: str,
        summary: str,
        issue_type: str,
        description: str = "",
        fields: dict | None = None,
    ) -> tuple[dict, str]:
        # Atlassian Rovo MCP createJiraIssue tool may exist; try it first,
        # fall back to REST on error.
        cloud_id = self.get_cloud_id()
        if not cloud_id:
            return self._rest.create_issue(project_key, summary, issue_type, description, fields)

        issue_fields: dict = {}
        if fields:
            issue_fields.update(fields)
        if description and "description" not in issue_fields:
            issue_fields["description"] = description
        args: dict = {
            "cloudId": cloud_id,
            "projectKey": project_key,
            "summary": summary,
            "issueType": issue_type,
        }
        if issue_fields.get("description"):
            args["description"] = str(issue_fields["description"])
        resp = self._call("createJiraIssue", args)
        if _is_error(resp):
            text = _extract_text(resp)
            # Fall back to REST if tool not found or permission denied
            return self._rest.create_issue(project_key, summary, issue_type, description, fields)
        text = _extract_text(resp)
        data = _parse_json(text)
        if isinstance(data, dict):
            return data, "created"
        return self._rest.create_issue(project_key, summary, issue_type, description, fields)

    def update_issue_fields(
        self, ticket_key: str, fields: dict
    ) -> tuple[dict | None, str]:
        if not fields:
            return None, "missing_fields"
        cloud_id = self.get_cloud_id()
        if not cloud_id:
            return self._rest.update_issue_fields(ticket_key, fields)
        resp = self._call("editJiraIssue", {
            "cloudId": cloud_id,
            "issueIdOrKey": ticket_key,
            "fields": fields,
        })
        if _is_error(resp):
            text = _extract_text(resp)
            if _is_api_token_denied(text):
                return self._rest.update_issue_fields(ticket_key, fields)
            return None, f"update_failed: {text[:150]}"
        return {"ticketKey": ticket_key}, "updated"

    def change_assignee(
        self, ticket_key: str, account_id: str | None
    ) -> tuple[str | None, str]:
        cloud_id = self.get_cloud_id()
        if not cloud_id:
            return self._rest.change_assignee(ticket_key, account_id)
        assignee_value = {"id": account_id} if account_id else None
        resp = self._call("editJiraIssue", {
            "cloudId": cloud_id,
            "issueIdOrKey": ticket_key,
            "fields": {"assignee": assignee_value},
        })
        if _is_error(resp):
            text = _extract_text(resp)
            if _is_api_token_denied(text):
                return self._rest.change_assignee(ticket_key, account_id)
            return None, f"assignee_failed: {text[:150]}"
        return account_id, "assigned"

    def add_comment(
        self, ticket_key: str, text: str, adf_body: dict | None = None
    ) -> tuple[str | None, str]:
        if adf_body and isinstance(adf_body, dict):
            return self._rest.add_comment(ticket_key, text, adf_body)
        cloud_id = self.get_cloud_id()
        if not cloud_id:
            return self._rest.add_comment(ticket_key, text, adf_body)
        # addCommentToJiraIssue accepts plain text only (not ADF)
        comment_text = text or ""
        if adf_body:
            # Extract plain text from ADF for MCP
            try:
                for block in adf_body.get("content", []):
                    for inline in block.get("content", []):
                        if inline.get("type") == "text":
                            comment_text = inline.get("text", text)
                            break
            except Exception:
                pass
        resp = self._call("addCommentToJiraIssue", {
            "cloudId": cloud_id,
            "issueIdOrKey": ticket_key,
            "commentBody": comment_text,
        })
        if _is_error(resp):
            text_resp = _extract_text(resp)
            if _is_api_token_denied(text_resp):
                return self._rest.add_comment(ticket_key, text, adf_body)
            return None, f"add_failed: {text_resp[:150]}"
        # Extract comment ID from response JSON
        resp_text = _extract_text(resp)
        data = _parse_json(resp_text)
        comment_id = ""
        if isinstance(data, dict):
            comment_id = str(data.get("id", ""))
        return comment_id or "unknown", "added"

    def update_comment(
        self,
        ticket_key: str,
        comment_id: str,
        new_text: str,
        adf_body: dict | None = None,
    ) -> tuple[str | None, str]:
        if adf_body and isinstance(adf_body, dict):
            return self._rest.update_comment(ticket_key, comment_id, new_text, adf_body)
        # Atlassian Rovo MCP does not expose a comment update tool;
        # fall back to REST.
        return self._rest.update_comment(ticket_key, comment_id, new_text, adf_body)

    def delete_comment(
        self, ticket_key: str, comment_id: str
    ) -> tuple[str | None, str]:
        # Atlassian Rovo MCP does not expose a comment delete tool;
        # fall back to REST.
        return self._rest.delete_comment(ticket_key, comment_id)

    @property
    def backend_name(self) -> str:
        return "mcp"
