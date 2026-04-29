#!/usr/bin/env python3
"""Atlassian Rovo MCP (Jira Cloud) integration tests.

Tests the Jira MCP server at https://mcp.atlassian.com/v1/mcp using the
JSON-RPC 2.0 protocol with SSE streaming support.

Required keys in tests/.env:
  TEST_JIRA_TICKET_URL   Full Jira browse URL (e.g. https://org.atlassian.net/browse/PROJ-1)
  TEST_JIRA_TOKEN        Jira API token
  TEST_JIRA_EMAIL        Atlassian account email

Usage:
    python3 tests/test_jira_mcp.py              # dry-run (no network)
    python3 tests/test_jira_mcp.py --integration [-v]
    python3 tests/test_jira_mcp.py --integration --provider [-v]  # also run JiraMCPProvider tests
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from tests.agent_test_support import Reporter, load_env_file

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_ENV = load_env_file("tests/.env")


def _env(key: str, fallback: str = "") -> str:
    return os.environ.get(key) or _ENV.get(key, fallback)


def _parse_jira_ticket_url(url: str) -> tuple[str, str]:
    url = url.strip()
    if "/browse/" in url:
        parts = url.split("/browse/")
        base = parts[0].rstrip("/")
        key = parts[1].split("/")[0].split("?")[0].strip()
        return base, key
    return url.rstrip("/"), ""


_jira_url = _env("TEST_JIRA_TICKET_URL")
if _jira_url:
    JIRA_BASE_URL, JIRA_TICKET_KEY = _parse_jira_ticket_url(_jira_url)
else:
    JIRA_BASE_URL = "https://your-org.atlassian.net"
    JIRA_TICKET_KEY = "PROJ-1"

JIRA_TICKET_URL = f"{JIRA_BASE_URL}/browse/{JIRA_TICKET_KEY}"
JIRA_CLOUD_HOST = JIRA_BASE_URL.replace("https://", "").replace("http://", "")
JIRA_TENANT_INFO_URL = f"https://{JIRA_CLOUD_HOST}/_edge/tenant_info"
ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp"

# Jira workflow transition IDs (Jira Cloud standard workflow)
_TRANSITION_IN_PROGRESS = "21"
_TRANSITION_TO_DO = "11"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _jira_auth_header() -> str | None:
    token = _env("TEST_JIRA_TOKEN")
    email = _env("TEST_JIRA_EMAIL")
    if not token:
        return None
    if token.startswith("Basic ") or token.startswith("Bearer "):
        return token
    if email:
        encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
        return f"Basic {encoded}"
    return f"Bearer {token}"


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _http_get(url: str, headers: dict | None = None, timeout: int = 15):
    req = Request(url, headers={"Accept": "application/json", **(headers or {})}, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body.strip() else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"error": body[:200]}
    except URLError as exc:
        return 0, {"error": str(exc)}


# ---------------------------------------------------------------------------
# Atlassian Rovo MCP protocol helpers
# ---------------------------------------------------------------------------

def _parse_sse_body(body: str) -> dict | None:
    """Parse an SSE event stream and return the first complete JSON-RPC response."""
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            data_str = line[5:].strip()
            if data_str and data_str != "[DONE]":
                try:
                    parsed = json.loads(data_str)
                    if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
                        return parsed
                except json.JSONDecodeError:
                    pass
    return None


def _atlassian_mcp_post(
    method: str,
    params: dict,
    auth_header: str,
    session_id: str | None = None,
    timeout: int = 30,
):
    """POST a JSON-RPC 2.0 request to the Atlassian Rovo MCP server.

    Handles both direct JSON and SSE streaming responses.
    Returns (http_status, json_rpc_response_dict, response_session_id).
    """
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    data = json.dumps(payload).encode("utf-8")
    hdrs = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": auth_header,
        "User-Agent": "MCP-Client/1.0",
    }
    if session_id:
        hdrs["Mcp-Session-Id"] = session_id
    req = Request(ATLASSIAN_MCP_URL, data=data, headers=hdrs, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            content_type = resp.headers.get("Content-Type", "")
            returned_sid = resp.headers.get("Mcp-Session-Id", session_id)
            body = resp.read().decode("utf-8")
            if "text/event-stream" in content_type:
                rpc_resp = _parse_sse_body(body)
                return resp.status, rpc_resp or {}, returned_sid
            return resp.status, json.loads(body) if body.strip() else {}, returned_sid
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body), session_id
        except Exception:
            return exc.code, {"error": body[:200]}, session_id
    except URLError as exc:
        return 0, {"error": str(exc)}, session_id


def _extract_mcp_text(rpc_resp: dict) -> str:
    """Concatenate all text content items from an MCP tools/call result."""
    result = rpc_resp.get("result", {})
    if isinstance(result, dict):
        parts = [
            item.get("text", "")
            for item in result.get("content", [])
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(parts)
    return str(result)


def _mcp_is_api_token_denied(text: str) -> bool:
    return "you don't have permission to connect via api token" in text.lower()


def _atlassian_mcp_init(auth_header: str, timeout: int = 30) -> str | None:
    """Perform MCP initialize handshake and return the session ID, or None on failure."""
    status, _, session_id = _atlassian_mcp_post(
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "constellation-test", "version": "1.0.0"},
        },
        auth_header,
        timeout=timeout,
    )
    if status == 200 and session_id:
        return session_id
    return None


def _get_jira_cloud_id(timeout: int = 10) -> str | None:
    req = Request(JIRA_TENANT_INFO_URL, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            return data.get("cloudId")
    except Exception:
        return None


def _jira_api_request(
    method: str, path: str, payload: dict | None = None, timeout: int = 15
):
    """Call the Atlassian API gateway (Basic email:token auth)."""
    cloud_id = _get_jira_cloud_id()
    if not cloud_id:
        return 0, {"error": "cloudId not resolved"}
    auth = _jira_auth_header()
    if not auth:
        return 0, {"error": "no auth header"}
    url = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3{path}"
    hdrs: dict = {"Accept": "application/json", "Authorization": auth}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = Request(url, data=data, headers=hdrs, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body.strip() else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"error": body[:200]}
    except URLError as exc:
        return 0, {"error": str(exc)}


# ---------------------------------------------------------------------------
# Test functions
# ---------------------------------------------------------------------------

def test_jira_url_parseable(reporter: Reporter) -> None:
    assert JIRA_TICKET_KEY in JIRA_TICKET_URL, "ticket key not in URL"
    assert JIRA_BASE_URL in JIRA_TICKET_URL, "base URL not in ticket URL"
    reporter.ok(f"Jira ticket URL is well-formed: {JIRA_TICKET_URL}")


def test_atlassian_mcp_tools_list(reporter: Reporter, session_id: str | None) -> None:
    auth = _jira_auth_header()
    if not auth:
        reporter.skip("Atlassian MCP tools/list", "TEST_JIRA_TOKEN not set in tests/.env")
        return
    if not session_id:
        reporter.skip("Atlassian MCP tools/list", "MCP session not initialized")
        return
    status, rpc_resp, _ = _atlassian_mcp_post("tools/list", {}, auth, session_id)
    if status == 200 and "result" in rpc_resp:
        tools = rpc_resp["result"].get("tools", [])
        jira_tools = [t.get("name", "") for t in tools if "jira" in t.get("name", "").lower()]
        reporter.ok(
            f"Atlassian Rovo MCP reachable — {len(tools)} tools, {len(jira_tools)} Jira tools"
        )
    elif status == 401:
        reporter.fail("Atlassian MCP auth rejected (401)", str(rpc_resp)[:200])
    elif status == 403:
        reporter.fail("Atlassian MCP forbidden (403) — check token scopes", str(rpc_resp)[:200])
    elif status == 0:
        reporter.fail("Atlassian MCP unreachable", str(rpc_resp)[:200])
    else:
        reporter.fail(f"Atlassian MCP tools/list returned HTTP {status}", str(rpc_resp)[:200])


def test_atlassian_mcp_user_info(reporter: Reporter, session_id: str | None) -> None:
    auth = _jira_auth_header()
    email = _env("TEST_JIRA_EMAIL")
    if not auth or not email:
        reporter.skip("lookupJiraAccountId", "TEST_JIRA_TOKEN or TEST_JIRA_EMAIL not set in tests/.env")
        return
    if not session_id:
        reporter.skip("lookupJiraAccountId", "MCP session not initialized")
        return
    cloud_id = _get_jira_cloud_id()
    if not cloud_id:
        reporter.skip("lookupJiraAccountId", "Could not resolve cloudId")
        return
    status, rpc_resp, _ = _atlassian_mcp_post(
        "tools/call",
        {"name": "lookupJiraAccountId", "arguments": {"cloudId": cloud_id, "searchString": email}},
        auth, session_id,
    )
    if status == 200 and "result" in rpc_resp:
        if rpc_resp["result"].get("isError"):
            text = _extract_mcp_text(rpc_resp)
            if _mcp_is_api_token_denied(text):
                reporter.skip(
                    "lookupJiraAccountId — API token auth not enabled at org level",
                    "Enable in Atlassian Admin: admin.atlassian.com → Security → MCP Server settings",
                )
            else:
                reporter.fail("lookupJiraAccountId isError=true", text[:200])
        else:
            reporter.ok(f"lookupJiraAccountId succeeded for {email}")
    elif status in (401, 403):
        reporter.fail(f"Atlassian MCP auth error ({status})", str(rpc_resp)[:150])
    elif status == 0:
        reporter.fail("Atlassian MCP unreachable", str(rpc_resp)[:150])
    else:
        reporter.fail(f"lookupJiraAccountId returned HTTP {status}", str(rpc_resp)[:200])


def test_resolve_cloud_id(reporter: Reporter) -> str | None:
    cloud_id = _get_jira_cloud_id()
    if cloud_id:
        reporter.ok(f"cloudId resolved for {JIRA_CLOUD_HOST}: {cloud_id}")
        return cloud_id
    reporter.fail(f"Could not resolve cloudId for {JIRA_CLOUD_HOST}",
                  f"GET {JIRA_TENANT_INFO_URL} failed or returned no cloudId")
    return None


def test_atlassian_jira_get_issue(
    reporter: Reporter, cloud_id: str | None, session_id: str | None
) -> None:
    auth = _jira_auth_header()
    if not auth or not session_id or not cloud_id:
        reporter.skip("getJiraIssue", "missing auth / session / cloudId")
        return
    status, rpc_resp, _ = _atlassian_mcp_post(
        "tools/call",
        {"name": "getJiraIssue", "arguments": {"cloudId": cloud_id, "issueIdOrKey": JIRA_TICKET_KEY}},
        auth, session_id,
    )
    if status == 200 and "result" in rpc_resp:
        if rpc_resp["result"].get("isError"):
            text = _extract_mcp_text(rpc_resp)
            if _mcp_is_api_token_denied(text):
                reporter.skip("getJiraIssue — API token auth not enabled at org level")
            else:
                reporter.fail("getJiraIssue isError=true", text[:200])
        else:
            text = _extract_mcp_text(rpc_resp)
            if JIRA_TICKET_KEY in text:
                reporter.ok(f"getJiraIssue succeeded for {JIRA_TICKET_KEY}")
            else:
                reporter.fail(f"getJiraIssue response missing {JIRA_TICKET_KEY}", text[:200])
    elif status in (401, 403):
        reporter.fail(f"getJiraIssue auth error ({status})", str(rpc_resp)[:150])
    elif status == 0:
        reporter.fail("Atlassian MCP unreachable", str(rpc_resp)[:150])
    else:
        reporter.fail(f"getJiraIssue returned HTTP {status}", str(rpc_resp)[:200])


def test_atlassian_jira_search_jql(
    reporter: Reporter, cloud_id: str | None, session_id: str | None
) -> None:
    auth = _jira_auth_header()
    if not auth or not session_id or not cloud_id:
        reporter.skip("searchJiraIssuesUsingJql", "missing auth / session / cloudId")
        return
    status, rpc_resp, _ = _atlassian_mcp_post(
        "tools/call",
        {
            "name": "searchJiraIssuesUsingJql",
            "arguments": {"cloudId": cloud_id, "jql": f"key = {JIRA_TICKET_KEY}", "maxResults": 1},
        },
        auth, session_id,
    )
    if status == 200 and "result" in rpc_resp:
        if rpc_resp["result"].get("isError"):
            text = _extract_mcp_text(rpc_resp)
            if _mcp_is_api_token_denied(text):
                reporter.skip("searchJiraIssuesUsingJql — API token auth not enabled at org level")
            else:
                reporter.fail("searchJiraIssuesUsingJql isError=true", text[:200])
        else:
            text = _extract_mcp_text(rpc_resp)
            if JIRA_TICKET_KEY in text:
                reporter.ok(f"searchJiraIssuesUsingJql found {JIRA_TICKET_KEY}")
            else:
                reporter.fail(f"searchJiraIssuesUsingJql: {JIRA_TICKET_KEY} not in response", text[:200])
    elif status in (401, 403):
        reporter.fail(f"searchJiraIssuesUsingJql auth error ({status})", str(rpc_resp)[:150])
    elif status == 0:
        reporter.fail("Atlassian MCP unreachable", str(rpc_resp)[:150])
    else:
        reporter.fail(f"searchJiraIssuesUsingJql returned HTTP {status}", str(rpc_resp)[:200])


def test_atlassian_jira_transition_state(
    reporter: Reporter, cloud_id: str | None, session_id: str | None
) -> None:
    auth = _jira_auth_header()
    if not auth or not session_id or not cloud_id:
        reporter.skip("transitionJiraIssue", "missing auth / session / cloudId")
        return

    def do_transition(transition_id: str, expected_name: str) -> bool:
        _, rpc_resp, _ = _atlassian_mcp_post(
            "tools/call",
            {
                "name": "transitionJiraIssue",
                "arguments": {
                    "cloudId": cloud_id,
                    "issueIdOrKey": JIRA_TICKET_KEY,
                    "transition": {"id": transition_id},
                },
            },
            auth, session_id,
        )
        if rpc_resp.get("result", {}).get("isError"):
            reporter.fail(
                f"transitionJiraIssue → '{expected_name}' failed",
                _extract_mcp_text(rpc_resp)[:200],
            )
            return False
        return True

    if do_transition(_TRANSITION_IN_PROGRESS, "In Progress"):
        reporter.ok(f"transitionJiraIssue: {JIRA_TICKET_KEY} → In Progress")
    if do_transition(_TRANSITION_TO_DO, "To Do"):
        reporter.ok(f"transitionJiraIssue: {JIRA_TICKET_KEY} → To Do (restored)")


def test_atlassian_jira_change_assignee(
    reporter: Reporter, cloud_id: str | None, session_id: str | None
) -> None:
    auth = _jira_auth_header()
    email = _env("TEST_JIRA_EMAIL")
    if not auth or not session_id or not cloud_id or not email:
        reporter.skip("editJiraIssue assignee", "missing auth / session / cloudId / email")
        return

    _, rpc_id, _ = _atlassian_mcp_post(
        "tools/call",
        {"name": "lookupJiraAccountId", "arguments": {"cloudId": cloud_id, "searchString": email}},
        auth, session_id,
    )
    account_id = None
    try:
        data = json.loads(_extract_mcp_text(rpc_id))
        users = data.get("data", {}).get("users", {}).get("users", [])
        if users:
            account_id = users[0].get("accountId")
    except Exception:
        pass
    if not account_id:
        reporter.skip("editJiraIssue assignee", "could not resolve accountId for token owner")
        return

    _, rpc_assign, _ = _atlassian_mcp_post(
        "tools/call",
        {
            "name": "editJiraIssue",
            "arguments": {
                "cloudId": cloud_id,
                "issueIdOrKey": JIRA_TICKET_KEY,
                "fields": {"assignee": {"id": account_id}},
            },
        },
        auth, session_id,
    )
    if rpc_assign.get("result", {}).get("isError"):
        reporter.fail("editJiraIssue: assign to self failed", _extract_mcp_text(rpc_assign)[:200])
        return
    reporter.ok(f"editJiraIssue: {JIRA_TICKET_KEY} assigned to {email}")

    _, rpc_unassign, _ = _atlassian_mcp_post(
        "tools/call",
        {
            "name": "editJiraIssue",
            "arguments": {
                "cloudId": cloud_id,
                "issueIdOrKey": JIRA_TICKET_KEY,
                "fields": {"assignee": None},
            },
        },
        auth, session_id,
    )
    if rpc_unassign.get("result", {}).get("isError"):
        reporter.fail("editJiraIssue: unassign failed", _extract_mcp_text(rpc_unassign)[:200])
        return
    reporter.ok(f"editJiraIssue: {JIRA_TICKET_KEY} assignee cleared (restored)")


def test_atlassian_jira_comment_lifecycle(
    reporter: Reporter, cloud_id: str | None, session_id: str | None
) -> None:
    """Full comment lifecycle: add → update → delete on the configured ticket."""
    auth = _jira_auth_header()
    if not auth or not session_id or not cloud_id:
        reporter.skip("comment lifecycle", "missing auth / session / cloudId")
        return

    _, rpc_add, _ = _atlassian_mcp_post(
        "tools/call",
        {
            "name": "addCommentToJiraIssue",
            "arguments": {
                "cloudId": cloud_id,
                "issueIdOrKey": JIRA_TICKET_KEY,
                "commentBody": "Constellation test comment — lifecycle test (add)",
            },
        },
        auth, session_id,
    )
    comment_id = None
    if rpc_add.get("result", {}).get("isError"):
        reporter.fail("addCommentToJiraIssue failed", _extract_mcp_text(rpc_add)[:200])
        return
    try:
        data_add = json.loads(_extract_mcp_text(rpc_add))
        comment_id = data_add.get("id")
    except Exception:
        pass
    if not comment_id:
        reporter.fail("addCommentToJiraIssue: could not parse comment id from response")
        return
    reporter.ok(f"addCommentToJiraIssue: comment {comment_id} added to {JIRA_TICKET_KEY}")

    update_body = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [{
                "type": "paragraph",
                "content": [{
                    "type": "text",
                    "text": "Constellation test comment — lifecycle test (updated)",
                }],
            }],
        }
    }
    status_update, _ = _jira_api_request(
        "PUT", f"/issue/{JIRA_TICKET_KEY}/comment/{comment_id}", update_body
    )
    if status_update in (200, 204):
        reporter.ok(f"PUT comment/{comment_id}: updated on {JIRA_TICKET_KEY}")
    else:
        reporter.fail(f"PUT comment/{comment_id} returned HTTP {status_update}")

    status_del, _ = _jira_api_request("DELETE", f"/issue/{JIRA_TICKET_KEY}/comment/{comment_id}")
    if status_del == 204:
        reporter.ok(f"DELETE comment/{comment_id}: deleted from {JIRA_TICKET_KEY}")
    else:
        reporter.fail(f"DELETE comment/{comment_id} returned HTTP {status_del}")


# ---------------------------------------------------------------------------
# JiraMCPProvider class-level tests
# ---------------------------------------------------------------------------

def _build_mcp_provider():
    """Instantiate a JiraMCPProvider from tests/.env config."""
    from jira.providers.mcp import JiraMCPProvider
    return JiraMCPProvider(
        jira_base_url=JIRA_BASE_URL,
        jira_token=_env("TEST_JIRA_TOKEN"),
        jira_email=_env("TEST_JIRA_EMAIL"),
        jira_auth_mode="basic",
        mcp_url=ATLASSIAN_MCP_URL,
        timeout=30,
    )


def run_provider_tests(reporter: Reporter) -> None:
    reporter.section("JiraMCPProvider — Atlassian Rovo MCP (provider class)")

    token = _env("TEST_JIRA_TOKEN")
    if not token:
        reporter.skip("All JiraMCPProvider tests", "TEST_JIRA_TOKEN not set in tests/.env")
        return

    try:
        provider = _build_mcp_provider()
    except Exception as exc:
        reporter.fail("Failed to instantiate JiraMCPProvider", str(exc))
        return

    # PROV-01  get_myself (REST fallback)
    reporter.step("PROV-01  provider.get_myself()")
    try:
        user, result = provider.get_myself()
        if result == "ok" and isinstance(user, dict) and user.get("accountId"):
            reporter.ok(f"get_myself() → {user.get('emailAddress') or user.get('displayName')}")
        else:
            reporter.fail("get_myself() failed", f"result={result} user={str(user)[:150]}")
    except Exception as exc:
        reporter.fail("get_myself() raised exception", str(exc))

    # PROV-02  fetch_issue
    reporter.step(f"PROV-02  provider.fetch_issue({JIRA_TICKET_KEY})")
    issue_fetched = None
    try:
        issue_fetched, result = provider.fetch_issue(JIRA_TICKET_KEY)
        if result == "fetched" and isinstance(issue_fetched, dict):
            summary_text = (issue_fetched.get("fields") or {}).get("summary", "")
            reporter.ok(f"fetch_issue({JIRA_TICKET_KEY}) → {summary_text[:80]}")
        else:
            reporter.fail(f"fetch_issue({JIRA_TICKET_KEY}) failed", f"result={result}")
    except Exception as exc:
        reporter.fail(f"fetch_issue({JIRA_TICKET_KEY}) raised exception", str(exc))

    # PROV-03  search_issues
    reporter.step(f"PROV-03  provider.search_issues('key = {JIRA_TICKET_KEY}')")
    try:
        search_body, result = provider.search_issues(f"key = {JIRA_TICKET_KEY}", max_results=1)
        if result == "ok":
            total = 0
            if isinstance(search_body, dict):
                total = len(search_body.get("issues", []))
            elif isinstance(search_body, list):
                total = len(search_body)
            reporter.ok(f"search_issues() returned {total} issue(s)")
        else:
            reporter.fail("search_issues() failed", f"result={result} body={str(search_body)[:150]}")
    except Exception as exc:
        reporter.fail("search_issues() raised exception", str(exc))

    # PROV-04  get_transitions (REST fallback)
    reporter.step(f"PROV-04  provider.get_transitions({JIRA_TICKET_KEY})")
    transitions = []
    current_status = ""
    try:
        transitions, result = provider.get_transitions(JIRA_TICKET_KEY)
        if result == "ok" and transitions:
            names = [t.get("name") for t in transitions if isinstance(t, dict)]
            reporter.ok(f"get_transitions() → {len(transitions)} transitions: {names[:5]}")
            # Try to find current status from fetched issue
            if isinstance(issue_fetched, dict):
                current_status = (issue_fetched.get("fields") or {}).get("status", {}).get("name", "")
        else:
            reporter.fail("get_transitions() failed", f"result={result}")
    except Exception as exc:
        reporter.fail("get_transitions() raised exception", str(exc))

    # PROV-05  transition_issue + restore
    if transitions and current_status:
        # Pick a non-current transition target
        target_name = None
        for t in transitions:
            if not isinstance(t, dict):
                continue
            to_name = (t.get("to") or {}).get("name", "")
            if to_name and to_name.lower() != current_status.lower():
                target_name = t.get("name")
                break
        if not target_name and transitions:
            target_name = transitions[0].get("name")

        if target_name:
            reporter.step(f"PROV-05  provider.transition_issue({JIRA_TICKET_KEY}, '{target_name}') + restore")
            try:
                tid, result = provider.transition_issue(JIRA_TICKET_KEY, target_name)
                if tid:
                    reporter.ok(f"transition_issue({JIRA_TICKET_KEY}) → {result}")
                    # Restore
                    tid2, result2 = provider.transition_issue(JIRA_TICKET_KEY, current_status)
                    if tid2:
                        reporter.ok(f"Status restored to: {current_status}")
                    else:
                        reporter.fail(f"Restore transition to '{current_status}' failed", f"result={result2}")
                else:
                    reporter.fail(f"transition_issue to '{target_name}' failed", f"result={result}")
            except Exception as exc:
                reporter.fail("transition_issue() raised exception", str(exc))
        else:
            reporter.skip("PROV-05 transition", "No suitable non-current transition found")
    else:
        reporter.skip("PROV-05 transition", "No transitions available or current status unknown")

    # PROV-06  add_comment
    reporter.step(f"PROV-06  provider.add_comment({JIRA_TICKET_KEY})")
    comment_id = None
    try:
        comment_id, result = provider.add_comment(
            JIRA_TICKET_KEY, "[Agent Test] Constellation JiraMCPProvider test comment — add"
        )
        if comment_id and result == "added":
            reporter.ok(f"add_comment({JIRA_TICKET_KEY}) → comment {comment_id}")
        else:
            reporter.fail(f"add_comment({JIRA_TICKET_KEY}) failed", f"result={result}")
            comment_id = None
    except Exception as exc:
        reporter.fail("add_comment() raised exception", str(exc))
        comment_id = None

    # PROV-07  update_comment (REST fallback)
    if comment_id:
        reporter.step(f"PROV-07  provider.update_comment({JIRA_TICKET_KEY}, {comment_id})")
        try:
            cid2, result = provider.update_comment(
                JIRA_TICKET_KEY, comment_id,
                "[Agent Test] Constellation JiraMCPProvider test comment — updated"
            )
            if cid2 and result == "updated":
                reporter.ok(f"update_comment({comment_id}) succeeded (REST fallback)")
            else:
                reporter.fail(f"update_comment({comment_id}) failed", f"result={result}")
        except Exception as exc:
            reporter.fail("update_comment() raised exception", str(exc))

    # PROV-08  delete_comment (REST fallback, cleanup)
    if comment_id:
        reporter.step(f"PROV-08  provider.delete_comment({JIRA_TICKET_KEY}, {comment_id})")
        try:
            cid3, result = provider.delete_comment(JIRA_TICKET_KEY, comment_id)
            if cid3 and result == "deleted":
                reporter.ok(f"delete_comment({comment_id}) succeeded (REST fallback) — cleaned up")
            else:
                reporter.fail(f"delete_comment({comment_id}) failed", f"result={result}")
        except Exception as exc:
            reporter.fail("delete_comment() raised exception", str(exc))


# ---------------------------------------------------------------------------
# Suite runner
# ---------------------------------------------------------------------------

def run_jira_tests(reporter: Reporter) -> None:
    reporter.section(f"Atlassian Rovo MCP — {ATLASSIAN_MCP_URL}")
    auth = _jira_auth_header()
    session_id = None
    if auth:
        session_id = _atlassian_mcp_init(auth)
        if session_id:
            reporter.ok(f"MCP session initialized: {session_id[:20]}...")
        else:
            reporter.fail("MCP session init failed — check TEST_JIRA_TOKEN / TEST_JIRA_EMAIL in tests/.env")
    else:
        reporter.skip("MCP session init", "TEST_JIRA_TOKEN not set in tests/.env")

    test_atlassian_mcp_tools_list(reporter, session_id)
    test_atlassian_mcp_user_info(reporter, session_id)
    cloud_id = test_resolve_cloud_id(reporter)
    test_atlassian_jira_get_issue(reporter, cloud_id, session_id)
    test_atlassian_jira_search_jql(reporter, cloud_id, session_id)
    test_atlassian_jira_transition_state(reporter, cloud_id, session_id)
    test_atlassian_jira_change_assignee(reporter, cloud_id, session_id)
    test_atlassian_jira_comment_lifecycle(reporter, cloud_id, session_id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--integration", action="store_true",
                        help="Run live integration tests (requires tests/.env credentials)")
    parser.add_argument("--provider", action="store_true",
                        help="Also run JiraMCPProvider class-level tests (implies --integration)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    reporter = Reporter(verbose=args.verbose)

    print("\n" + "=" * 60)
    print("  Jira MCP Integration Tests  (Atlassian Rovo MCP)")
    print("=" * 60)
    print(f"  Ticket : {JIRA_TICKET_URL}")
    print(f"  MCP URL: {ATLASSIAN_MCP_URL}")

    reporter.section("Static / dry-run checks")
    test_jira_url_parseable(reporter)

    run_integration = args.integration or args.provider
    if not run_integration:
        print("\n\033[93mIntegration tests skipped — pass --integration to run live checks.\033[0m")
    else:
        run_jira_tests(reporter)
        if args.provider:
            run_provider_tests(reporter)

    print(f"\nPassed: {reporter.passed}  Failed: {reporter.failed}  Skipped: {reporter.skipped}")
    return 0 if reporter.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
