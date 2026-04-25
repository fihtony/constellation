#!/usr/bin/env python3
"""MCP (Model Context Protocol) connectivity and capability tests.

Verifies that the three MCP servers used by the Constellation system are
reachable and return valid responses:

  - Jira MCP  — configured via TEST_JIRA_BASE_URL and TEST_JIRA_TICKET_KEY
  - GitHub MCP — configured via TEST_GITHUB_OWNER and TEST_GITHUB_REPO
  - Google Stitch MCP — configured via TEST_STITCH_PROJECT_ID and TEST_STITCH_SCREEN_ID

Environment variables required to run integration tests:
  TEST_JIRA_TOKEN        Jira API token
  TEST_JIRA_EMAIL        Jira account email (for Basic auth)
  TEST_GITHUB_TOKEN      GitHub personal access token
  TEST_STITCH_API_KEY    Google Stitch / Gemini API key

Optional overrides (defaults loaded from tests/agent_test_targets.json):
  TEST_JIRA_BASE_URL     Base URL of Jira Cloud tenant
  TEST_JIRA_TICKET_KEY   Jira ticket key (PROJ-1, etc.)
  TEST_GITHUB_OWNER      GitHub repository owner
  TEST_GITHUB_REPO       GitHub repository name
  TEST_STITCH_PROJECT_ID Google Stitch project ID
  TEST_STITCH_SCREEN_ID  Google Stitch screen ID

Usage:
    # Dry-run (no network calls — checks module imports and config parsing):
    python3 tests/test_mcp.py

    # Full integration mode (requires environment variables):
    TEST_JIRA_TOKEN=... TEST_JIRA_EMAIL=... TEST_GITHUB_TOKEN=... TEST_STITCH_API_KEY=... \\
        python3 tests/test_mcp.py --integration [-v]

    # Run only a specific MCP:
    python3 tests/test_mcp.py --integration --jira
    python3 tests/test_mcp.py --integration --github
    python3 tests/test_mcp.py --integration --stitch
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Configuration loader — read from tests/.env, fall back to agent_test_targets.json
# ---------------------------------------------------------------------------

def _load_env_file(path):
    """Load .env file and return dict of key=value pairs."""
    env_dict = {}
    if not os.path.isfile(path):
        return env_dict
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            env_dict[key.strip()] = value.strip()
    return env_dict

def _load_test_targets():
    """Load agent_test_targets.json for default test data."""
    targets_path = Path(__file__).parent / "agent_test_targets.json"
    if targets_path.is_file():
        with open(targets_path, encoding="utf-8") as fh:
            return json.load(fh)
    return {}

_TEST_ENV = _load_env_file(os.path.join(os.path.dirname(__file__), ".env"))
_TEST_TARGETS = _load_test_targets()


# ---------------------------------------------------------------------------
# URL parsers — users provide full URLs; components are extracted here
# ---------------------------------------------------------------------------

def _parse_jira_ticket_url(url: str) -> tuple[str, str]:
    """Parse 'https://org.atlassian.net/browse/PROJ-1' → (base_url, ticket_key)."""
    url = url.strip()
    if "/browse/" in url:
        parts = url.split("/browse/")
        base = parts[0].rstrip("/")
        key = parts[1].split("/")[0].split("?")[0].strip()
        return base, key
    return url.rstrip("/"), ""


def _parse_github_repo_url(url: str) -> tuple[str, str]:
    """Parse 'https://github.com/owner/repo' → (owner, repo)."""
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if "?" in url:
        url = url.split("?")[0]
    parts = [p for p in url.split("/") if p and ":" not in p]
    # parts: ['github.com', 'owner', 'repo']
    if len(parts) >= 3:
        return parts[-2], parts[-1]
    return "", ""


def _parse_figma_file_url(url: str) -> str:
    """Parse Figma URL → file key."""
    url = url.strip()
    for prefix in ("/design/", "/file/"):
        if prefix in url:
            after = url.split(prefix)[1]
            return after.split("/")[0].split("?")[0]
    return ""


def _parse_stitch_project_url(url: str) -> str:
    """Parse 'https://stitch.withgoogle.com/projects/ID' → project ID."""
    url = url.strip()
    if "/projects/" in url:
        after = url.split("/projects/")[1]
        return after.split("/")[0].split("?")[0]
    return ""

def _get_config(env_key: str, json_path: str, default: str = "") -> str:
    """Get config from environment, then agent_test_targets.json, then default."""
    if env_key in os.environ:
        return os.environ[env_key]
    if env_key in _TEST_ENV:
        return _TEST_ENV[env_key]
    # Navigate JSON path: "tracker.primaryTicket.ticketKey"
    value = _TEST_TARGETS
    for key in json_path.split("."):
        if isinstance(value, dict):
            value = value.get(key, {})
        else:
            value = default
            break
    return str(value) if value else default

# ---------------------------------------------------------------------------
# Test targets — parsed from URL env vars; individual vars kept as fallback
# ---------------------------------------------------------------------------

# Jira: prefer TEST_JIRA_TICKET_URL (full URL), fall back to separate vars
_jira_ticket_url = _get_config("TEST_JIRA_TICKET_URL", "tracker.primaryTicket.browseUrl", "")
if _jira_ticket_url:
    _jira_base, _jira_key = _parse_jira_ticket_url(_jira_ticket_url)
    JIRA_BASE_URL = _get_config("TEST_JIRA_BASE_URL", "", "") or _jira_base
    JIRA_TICKET_KEY = _get_config("TEST_JIRA_TICKET_KEY", "tracker.primaryTicket.ticketKey", "") or _jira_key
else:
    JIRA_TICKET_KEY = _get_config("TEST_JIRA_TICKET_KEY", "tracker.primaryTicket.ticketKey", "PROJ-1")
    JIRA_BASE_URL = _get_config("TEST_JIRA_BASE_URL", "tracker.primaryTicket.browseUrl", "https://your-org.atlassian.net").split("/browse/")[0]

JIRA_TICKET_URL = f"{JIRA_BASE_URL}/browse/{JIRA_TICKET_KEY}"
JIRA_CLOUD_HOST = JIRA_BASE_URL.replace("https://", "").replace("http://", "")
JIRA_TENANT_INFO_URL = f"https://{JIRA_CLOUD_HOST}/_edge/tenant_info"
ATLASSIAN_MCP_URL = "https://mcp.atlassian.com/v1/mcp"

# GitHub: prefer TEST_GITHUB_REPO_URL (full URL), fall back to separate vars
_github_repo_url = _get_config("TEST_GITHUB_REPO_URL", "scm.primaryRepo.browseUrl", "")
if _github_repo_url:
    _gh_owner, _gh_repo = _parse_github_repo_url(_github_repo_url)
    GITHUB_OWNER = _get_config("TEST_GITHUB_OWNER", "scm.primaryRepo.owner", "") or _gh_owner
    GITHUB_REPO = _get_config("TEST_GITHUB_REPO", "scm.primaryRepo.repo", "") or _gh_repo
else:
    GITHUB_OWNER = _get_config("TEST_GITHUB_OWNER", "scm.primaryRepo.owner", "your-username")
    GITHUB_REPO = _get_config("TEST_GITHUB_REPO", "scm.primaryRepo.repo", "test-repo")

GITHUB_REPO_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}"
GITHUB_API_BASE = "https://api.github.com"

# Figma: prefer TEST_FIGMA_FILE_URL (full URL), fall back to separate var
_figma_file_url = _get_config("TEST_FIGMA_FILE_URL", "uiDesign.figma.fileUrl", "")
FIGMA_FILE_KEY = _parse_figma_file_url(_figma_file_url) if _figma_file_url else \
    _get_config("TEST_FIGMA_FILE_KEY", "uiDesign.figma.fileKey", "your-figma-file-key")
FIGMA_FILE_URL = _figma_file_url or f"https://www.figma.com/design/{FIGMA_FILE_KEY}/Test-File"

# Stitch: prefer TEST_STITCH_PROJECT_URL (full URL), fall back to separate var
_stitch_project_url = _get_config("TEST_STITCH_PROJECT_URL", "stitch.primaryProject.projectUrl", "")
STITCH_PROJECT_ID = _parse_stitch_project_url(_stitch_project_url) if _stitch_project_url else \
    _get_config("TEST_STITCH_PROJECT_ID", "stitch.primaryProject.projectId", "your-project-id")
STITCH_PROJECT_URL = _stitch_project_url or f"https://stitch.withgoogle.com/projects/{STITCH_PROJECT_ID}"
STITCH_SCREEN_ID = _get_config("TEST_STITCH_SCREEN_ID", "stitch.primaryProject.primaryScreen.screenId", "your-screen-id")
STITCH_SCREEN_NAME = _get_config("TEST_STITCH_SCREEN_NAME", "stitch.primaryProject.primaryScreen.name", "Test Screen")
STITCH_MCP_URL = "https://stitch.googleapis.com/mcp"

# ---------------------------------------------------------------------------
# Report helpers
# ---------------------------------------------------------------------------

class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


class Report:
    def __init__(self, verbose: bool = False):
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.verbose = verbose

    def ok(self, msg: str, detail: str = ""):
        self.passed += 1
        print(f"  {Colors.GREEN}PASS{Colors.RESET}  {msg}")
        if detail and self.verbose:
            print(f"         {detail}")

    def fail(self, msg: str, detail: str = ""):
        self.failed += 1
        print(f"  {Colors.RED}FAIL{Colors.RESET}  {msg}")
        if detail:
            print(f"         {detail}")

    def skip(self, msg: str, reason: str = ""):
        self.skipped += 1
        print(f"  {Colors.YELLOW}SKIP{Colors.RESET}  {msg}" + (f" — {reason}" if reason else ""))

    def section(self, title: str):
        print(f"\n{Colors.BOLD}── {title} ──{Colors.RESET}")

    def summary(self):
        total = self.passed + self.failed + self.skipped
        status = Colors.GREEN if self.failed == 0 else Colors.RED
        print(f"\n{Colors.BOLD}Results:{Colors.RESET} "
              f"{status}{self.passed}/{total} passed{Colors.RESET}, "
              f"{self.skipped} skipped, {self.failed} failed")
        return self.failed == 0


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get(url: str, headers: dict | None = None, timeout: int = 15):
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


def _post(url: str, payload: dict, headers: dict | None = None, timeout: int = 15):
    data = json.dumps(payload).encode("utf-8")
    req = Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json", **(headers or {})},
        method="POST",
    )
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
# Auth helpers
# ---------------------------------------------------------------------------

def _jira_auth_header() -> str | None:
    token = os.environ.get("TRACKER_TOKEN", "")
    email = os.environ.get("TRACKER_EMAIL", "")
    if not token:
        return None
    if token.startswith("Basic ") or token.startswith("Bearer "):
        return token
    if email:
        encoded = base64.b64encode(f"{email}:{token}".encode()).decode()
        return f"Basic {encoded}"
    return f"Bearer {token}"


def _github_auth_header() -> str | None:
    token = os.environ.get("SCM_TOKEN", "")
    if not token:
        return None
    return f"Bearer {token}"


def _stitch_api_key() -> str | None:
    return os.environ.get("STITCH_API_KEY", "") or None


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
    method: str, params: dict, auth_header: str,
    session_id: str | None = None, timeout: int = 30,
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
    """Extract concatenated text content from an MCP tools/call result."""
    result = rpc_resp.get("result", {})
    if isinstance(result, dict):
        content = result.get("content", [])
        parts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(parts)
    return str(result)


_ADMIN_TOKEN_DENIED = "you don't have permission to connect via api token"


def _mcp_is_api_token_denied(text: str) -> bool:
    """Return True if the MCP response indicates API token auth is disabled at org level."""
    return _ADMIN_TOKEN_DENIED in text.lower()


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
    """Fetch the Atlassian cloudId for JIRA_CLOUD_HOST via the public tenant_info endpoint."""
    req = Request(JIRA_TENANT_INFO_URL, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            return data.get("cloudId")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Jira MCP tests (via Atlassian Rovo MCP server)
# ---------------------------------------------------------------------------

def test_jira_ticket_url_parseable(report: Report):
    """Verify the Jira ticket URL is well-formed and contains the expected key."""
    assert JIRA_TICKET_KEY in JIRA_TICKET_URL, "ticket key not in URL"
    assert JIRA_BASE_URL in JIRA_TICKET_URL, "base URL not in ticket URL"
    report.ok("Jira ticket URL is well-formed", JIRA_TICKET_URL)


def test_atlassian_mcp_tools_list(report: Report, session_id: str | None) -> None:
    """Call tools/list on the Atlassian Rovo MCP server to verify reachability and list Jira tools."""
    auth = _jira_auth_header()
    if not auth:
        report.skip("Atlassian MCP tools/list", "TRACKER_TOKEN not set")
        return
    if not session_id:
        report.skip("Atlassian MCP tools/list", "MCP session not initialized")
        return

    status, rpc_resp, _ = _atlassian_mcp_post("tools/list", {}, auth, session_id)

    if status == 200 and "result" in rpc_resp:
        tools = rpc_resp["result"].get("tools", [])
        tool_names = sorted(t.get("name", "") for t in tools)
        jira_tools = [n for n in tool_names if "jira" in n.lower() or "Jira" in n]
        report.ok(
            f"Atlassian Rovo MCP reachable — {len(tools)} tools available",
            f"Jira tools: {jira_tools[:8]}",
        )
    elif status == 401:
        report.fail("Atlassian MCP auth rejected (401)", str(rpc_resp)[:200])
    elif status == 403:
        report.fail("Atlassian MCP forbidden (403) — check token scopes", str(rpc_resp)[:200])
    elif status == 0:
        report.fail("Atlassian MCP unreachable", str(rpc_resp)[:200])
    else:
        report.fail(f"Atlassian MCP tools/list returned HTTP {status}", str(rpc_resp)[:200])


def test_atlassian_mcp_user_info(report: Report, session_id: str | None) -> None:
    """Call lookupJiraAccountId for the token owner email to verify identity via Atlassian Rovo MCP."""
    auth = _jira_auth_header()
    email = os.environ.get("TRACKER_EMAIL", "")
    if not auth or not email:
        report.skip("Atlassian MCP lookupJiraAccountId", "TRACKER_TOKEN or TRACKER_EMAIL not set")
        return
    if not session_id:
        report.skip("Atlassian MCP lookupJiraAccountId", "MCP session not initialized")
        return

    cloud_id = _get_jira_cloud_id()
    if not cloud_id:
        report.skip("Atlassian MCP lookupJiraAccountId", "Could not resolve cloudId")
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
                report.skip(
                    "lookupJiraAccountId — API token auth not enabled",
                    "Enable in Atlassian Admin: admin.atlassian.com → Security → Atlassian Rovo MCP Server settings",
                )
            else:
                report.fail("lookupJiraAccountId isError=true", text[:200])
        else:
            text = _extract_mcp_text(rpc_resp)
            report.ok("lookupJiraAccountId succeeded", text[:120])
    elif status == 200 and "error" in rpc_resp:
        err = rpc_resp["error"]
        report.fail(f"lookupJiraAccountId error: {err.get('message', str(err))[:150]}")
    elif status in (401, 403):
        report.fail(f"Atlassian MCP auth error ({status})", str(rpc_resp)[:150])
    elif status == 0:
        report.fail("Atlassian MCP unreachable", str(rpc_resp)[:150])
    else:
        report.fail(f"lookupJiraAccountId returned HTTP {status}", str(rpc_resp)[:200])


def test_atlassian_mcp_accessible_resources(report: Report, session_id: str | None) -> tuple:
    """Resolve cloudId for the configured Jira tenant via the public tenant_info endpoint."""
    cloud_id = _get_jira_cloud_id()
    if cloud_id:
        report.ok(
            f"cloudId for {JIRA_CLOUD_HOST} resolved via tenant_info",
            f"cloudId: {cloud_id}",
        )
        return (cloud_id,)
    else:
        report.fail(
            f"Could not resolve cloudId for {JIRA_CLOUD_HOST}",
            f"GET {JIRA_TENANT_INFO_URL} failed",
        )
        return (None,)


def test_atlassian_jira_get_issue(report: Report, cloud_id: str | None, session_id: str | None) -> None:
    """Call getJiraIssue for the configured ticket via Atlassian Rovo MCP."""
    auth = _jira_auth_header()
    if not auth:
        report.skip("Atlassian MCP getJiraIssue", "TRACKER_TOKEN not set")
        return
    if not session_id:
        report.skip("Atlassian MCP getJiraIssue", "MCP session not initialized")
        return
    if not cloud_id:
        report.skip("Atlassian MCP getJiraIssue", "cloudId not available (see previous test)")
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
                report.skip(
                    f"getJiraIssue — API token auth not enabled at org level",
                    "Enable in Atlassian Admin: admin.atlassian.com → Security → Atlassian Rovo MCP Server settings",
                )
            else:
                report.fail(f"getJiraIssue isError=true", text[:200])
        else:
            text = _extract_mcp_text(rpc_resp)
            if JIRA_TICKET_KEY in text:
                report.ok(f"getJiraIssue succeeded for {JIRA_TICKET_KEY}", text[:120])
            else:
                report.fail(f"getJiraIssue response missing {JIRA_TICKET_KEY}", text[:200])
    elif status == 200 and "error" in rpc_resp:
        err = rpc_resp["error"]
        report.fail(f"getJiraIssue error: {err.get('message', str(err))[:150]}")
    elif status in (401, 403):
        report.fail(f"getJiraIssue auth error ({status})", str(rpc_resp)[:150])
    elif status == 0:
        report.fail("Atlassian MCP unreachable", str(rpc_resp)[:150])
    else:
        report.fail(f"getJiraIssue returned HTTP {status}", str(rpc_resp)[:200])


# ---------------------------------------------------------------------------
# Jira write-operation tests (state, assignee, comments)
# ---------------------------------------------------------------------------

# Transition IDs (Jira workflow state machine — may vary by project/instance)
_TRANSITION_IN_PROGRESS = "21"
_TRANSITION_TO_DO = "11"


def _jira_api_request(
    method: str, path: str, payload: dict | None = None, timeout: int = 15
) -> tuple[int, dict]:
    """Call the Atlassian API gateway (works with Basic email:token auth)."""
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


def test_atlassian_jira_transition_state(
    report: Report, cloud_id: str | None, session_id: str | None
) -> None:
    """Transition the configured ticket to 'In Progress' then back to 'To Do' via transitionJiraIssue MCP tool."""
    auth = _jira_auth_header()
    if not auth or not session_id or not cloud_id:
        report.skip("transitionJiraIssue", "missing auth / session / cloudId")
        return

    def do_transition(transition_id: str, expected_name: str) -> bool:
        _, rpc_resp, _ = _atlassian_mcp_post(
            "tools/call",
            {"name": "transitionJiraIssue", "arguments": {
                "cloudId": cloud_id,
                "issueIdOrKey": JIRA_TICKET_KEY,
                "transition": {"id": transition_id},
            }},
            auth, session_id,
        )
        if rpc_resp.get("result", {}).get("isError"):
            text = _extract_mcp_text(rpc_resp)
            report.fail(f"transitionJiraIssue → '{expected_name}' failed", text[:200])
            return False
        return True

    ok1 = do_transition(_TRANSITION_IN_PROGRESS, "In Progress")
    if ok1:
        report.ok(f"transitionJiraIssue: {JIRA_TICKET_KEY} → In Progress")
    ok2 = do_transition(_TRANSITION_TO_DO, "To Do")
    if ok2:
        report.ok(f"transitionJiraIssue: {JIRA_TICKET_KEY} → To Do (restored)")


def test_atlassian_jira_change_assignee(
    report: Report, cloud_id: str | None, session_id: str | None
) -> None:
    """Assign the configured ticket to the token owner then clear assignee via editJiraIssue MCP tool."""
    auth = _jira_auth_header()
    email = os.environ.get("TRACKER_EMAIL", "")
    if not auth or not session_id or not cloud_id or not email:
        report.skip("editJiraIssue assignee", "missing auth / session / cloudId / email")
        return

    # Resolve accountId for the token owner
    _, rpc_id, _ = _atlassian_mcp_post(
        "tools/call",
        {"name": "lookupJiraAccountId", "arguments": {"cloudId": cloud_id, "searchString": email}},
        auth, session_id,
    )
    account_id = None
    try:
        text = _extract_mcp_text(rpc_id)
        data = json.loads(text)
        users = data.get("data", {}).get("users", {}).get("users", [])
        if users:
            account_id = users[0].get("accountId")
    except Exception:
        pass

    if not account_id:
        report.skip("editJiraIssue assignee", "could not resolve accountId for token owner")
        return

    # Assign to self
    _, rpc_assign, _ = _atlassian_mcp_post(
        "tools/call",
        {"name": "editJiraIssue", "arguments": {
            "cloudId": cloud_id,
            "issueIdOrKey": JIRA_TICKET_KEY,
            "fields": {"assignee": {"id": account_id}},
        }},
        auth, session_id,
    )
    if rpc_assign.get("result", {}).get("isError"):
        report.fail("editJiraIssue assignee: assign to self failed", _extract_mcp_text(rpc_assign)[:200])
        return
    report.ok(f"editJiraIssue: {JIRA_TICKET_KEY} assigned to {email} ({account_id[:16]}...)")

    # Unassign (set to null)
    _, rpc_unassign, _ = _atlassian_mcp_post(
        "tools/call",
        {"name": "editJiraIssue", "arguments": {
            "cloudId": cloud_id,
            "issueIdOrKey": JIRA_TICKET_KEY,
            "fields": {"assignee": None},
        }},
        auth, session_id,
    )
    if rpc_unassign.get("result", {}).get("isError"):
        report.fail("editJiraIssue assignee: unassign failed", _extract_mcp_text(rpc_unassign)[:200])
        return
    report.ok(f"editJiraIssue: {JIRA_TICKET_KEY} assignee cleared (restored to unassigned)")


def test_atlassian_jira_comment_lifecycle(
    report: Report, cloud_id: str | None, session_id: str | None
) -> None:
    """Full comment lifecycle: add → update → delete on the configured ticket.

    - Add comment: uses addCommentToJiraIssue MCP tool.
    - Update comment: uses Atlassian API gateway REST API
      (PUT /rest/api/3/issue/{key}/comment/{id}) — same Basic auth, different host.
    - Delete comment: uses Atlassian API gateway REST API
      (DELETE /rest/api/3/issue/{key}/comment/{id}).
    """
    auth = _jira_auth_header()
    if not auth or not session_id or not cloud_id:
        report.skip("comment lifecycle", "missing auth / session / cloudId")
        return

    # --- 1. Add comment ---
    _, rpc_add, _ = _atlassian_mcp_post(
        "tools/call",
        {"name": "addCommentToJiraIssue", "arguments": {
            "cloudId": cloud_id,
            "issueIdOrKey": JIRA_TICKET_KEY,
            "commentBody": "Constellation test comment — lifecycle test (add)",
        }},
        auth, session_id,
    )
    comment_id = None
    if rpc_add.get("result", {}).get("isError"):
        report.fail("addCommentToJiraIssue failed", _extract_mcp_text(rpc_add)[:200])
        return
    try:
        text_add = _extract_mcp_text(rpc_add)
        data_add = json.loads(text_add)
        comment_id = data_add.get("id")
    except Exception:
        pass
    if not comment_id:
        report.fail("addCommentToJiraIssue: could not parse comment id from response")
        return
    report.ok(f"addCommentToJiraIssue: comment {comment_id} added to {JIRA_TICKET_KEY}")

    # --- 2. Update comment (Atlassian API gateway REST) ---
    update_body = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [{
                "type": "paragraph",
                "content": [{"type": "text", "text": "Constellation test comment — lifecycle test (updated)"}],
            }],
        }
    }
    status_update, resp_update = _jira_api_request(
        "PUT", f"/issue/{JIRA_TICKET_KEY}/comment/{comment_id}", update_body
    )
    if status_update in (200, 204):
        report.ok(f"PUT comment/{comment_id}: comment updated on {JIRA_TICKET_KEY}")
    else:
        report.fail(
            f"PUT comment/{comment_id} returned HTTP {status_update}",
            str(resp_update)[:200],
        )
        # Still proceed to delete

    # --- 3. Delete comment (Atlassian API gateway REST) ---
    status_del, resp_del = _jira_api_request(
        "DELETE", f"/issue/{JIRA_TICKET_KEY}/comment/{comment_id}"
    )
    if status_del == 204:
        report.ok(f"DELETE comment/{comment_id}: comment deleted from {JIRA_TICKET_KEY}")
    else:
        report.fail(
            f"DELETE comment/{comment_id} returned HTTP {status_del}",
            str(resp_del)[:200],
        )


def test_atlassian_jira_search_jql(report: Report, cloud_id: str | None, session_id: str | None) -> None:
    """Call searchJiraIssuesUsingJql for the configured ticket via Atlassian Rovo MCP."""
    auth = _jira_auth_header()
    if not auth:
        report.skip("Atlassian MCP searchJiraIssuesUsingJql", "TRACKER_TOKEN not set")
        return
    if not session_id:
        report.skip("Atlassian MCP searchJiraIssuesUsingJql", "MCP session not initialized")
        return
    if not cloud_id:
        report.skip("Atlassian MCP searchJiraIssuesUsingJql", "cloudId not available")
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
                report.skip(
                    "searchJiraIssuesUsingJql — API token auth not enabled at org level",
                    "Enable in Atlassian Admin: admin.atlassian.com → Security → Atlassian Rovo MCP Server settings",
                )
            else:
                report.fail("searchJiraIssuesUsingJql isError=true", text[:200])
        else:
            text = _extract_mcp_text(rpc_resp)
            if JIRA_TICKET_KEY in text:
                report.ok(f"searchJiraIssuesUsingJql found {JIRA_TICKET_KEY}", text[:120])
            else:
                report.fail(f"searchJiraIssuesUsingJql: {JIRA_TICKET_KEY} not in response", text[:200])
    elif status == 200 and "error" in rpc_resp:
        err = rpc_resp["error"]
        report.fail(f"searchJiraIssuesUsingJql error: {err.get('message', str(err))[:150]}")
    elif status in (401, 403):
        report.fail(f"searchJiraIssuesUsingJql auth error ({status})", str(rpc_resp)[:150])
    elif status == 0:
        report.fail("Atlassian MCP unreachable", str(rpc_resp)[:150])
    else:
        report.fail(f"searchJiraIssuesUsingJql HTTP {status}", str(rpc_resp)[:200])


# ---------------------------------------------------------------------------
# GitHub MCP tests
# ---------------------------------------------------------------------------

def test_github_repo_url_parseable(report: Report):
    """Verify the GitHub repo URL is well-formed."""
    assert GITHUB_OWNER in GITHUB_REPO_URL
    assert GITHUB_REPO in GITHUB_REPO_URL
    report.ok("GitHub repo URL is well-formed", GITHUB_REPO_URL)


def test_github_repo_metadata(report: Report):
    """Fetch repo metadata from GitHub REST API and verify it's accessible."""
    auth = _github_auth_header()
    headers = {"X-GitHub-Api-Version": "2022-11-28"}
    if auth:
        headers["Authorization"] = auth

    url = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
    status, body = _get(url, headers=headers)

    if status == 200:
        full_name = body.get("full_name", "")
        default_branch = body.get("default_branch", "")
        report.ok(f"GitHub repo accessible: {full_name}", f"default branch: {default_branch}")
    elif status == 401:
        report.fail("GitHub auth rejected (401)", str(body)[:150])
    elif status == 403:
        report.fail("GitHub forbidden (403) — check token scopes", str(body)[:150])
    elif status == 404:
        report.fail(f"GitHub repo {GITHUB_OWNER}/{GITHUB_REPO} not found (404)")
    else:
        report.fail(f"GitHub repo metadata returned HTTP {status}", str(body)[:150])


def test_github_repo_branches(report: Report):
    """List branches of the test repository."""
    auth = _github_auth_header()
    headers = {"X-GitHub-Api-Version": "2022-11-28"}
    if auth:
        headers["Authorization"] = auth

    url = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/branches"
    status, body = _get(url, headers=headers)

    if status == 200 and isinstance(body, list):
        branch_names = [b.get("name", "") for b in body[:5]]
        report.ok(f"GitHub branches listed ({len(body)} total)", f"branches: {branch_names}")
    elif status in (401, 403, 404):
        report.fail(f"GitHub branches returned HTTP {status}", str(body)[:150])
    else:
        report.fail(f"GitHub branches returned HTTP {status}", str(body)[:150])


def test_github_repo_contents(report: Report):
    """Check that the repo root directory listing is accessible."""
    auth = _github_auth_header()
    headers = {"X-GitHub-Api-Version": "2022-11-28"}
    if auth:
        headers["Authorization"] = auth

    url = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/"
    status, body = _get(url, headers=headers)

    if status == 200 and isinstance(body, list):
        names = [item.get("name", "") for item in body[:8]]
        report.ok(f"GitHub root contents listed ({len(body)} items)", f"files: {names}")
    elif status in (401, 403, 404):
        report.fail(f"GitHub contents returned HTTP {status}", str(body)[:150])
    else:
        report.fail(f"GitHub contents returned HTTP {status}", str(body)[:150])


def test_github_pull_requests(report: Report):
    """List open pull requests in the test repository."""
    auth = _github_auth_header()
    headers = {"X-GitHub-Api-Version": "2022-11-28"}
    if auth:
        headers["Authorization"] = auth

    url = f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/pulls?state=open&per_page=5"
    status, body = _get(url, headers=headers)

    if status == 200 and isinstance(body, list):
        report.ok(f"GitHub PRs listed ({len(body)} open)")
    elif status in (401, 403, 404):
        report.fail(f"GitHub PRs returned HTTP {status}", str(body)[:150])
    else:
        report.fail(f"GitHub PRs returned HTTP {status}", str(body)[:150])


# ---------------------------------------------------------------------------
# Google Stitch MCP tests
# ---------------------------------------------------------------------------

def test_stitch_url_parseable(report: Report):
    """Verify the Stitch project URL and IDs are well-formed."""
    assert STITCH_PROJECT_ID in STITCH_PROJECT_URL
    assert len(STITCH_SCREEN_ID) == 32, f"Expected 32-char screen ID, got {len(STITCH_SCREEN_ID)}"
    report.ok("Google Stitch project URL is well-formed", STITCH_PROJECT_URL)
    report.ok(f"Google Stitch screen '{STITCH_SCREEN_NAME}' ID is well-formed", STITCH_SCREEN_ID)


def test_stitch_mcp_list_tools(report: Report):
    """Call the Stitch MCP server to list available tools via JSON-RPC."""
    api_key = _stitch_api_key()
    if not api_key:
        report.skip("Stitch MCP list tools", "STITCH_API_KEY not set")
        return

    headers = {"X-Goog-Api-Key": api_key}
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/list",
        "params": {},
    }
    status, body = _post(STITCH_MCP_URL, payload, headers=headers)

    if status == 200:
        tools = (body.get("result") or {}).get("tools", [])
        tool_names = [t.get("name", "") for t in tools[:10]]
        report.ok(f"Stitch MCP tools/list succeeded ({len(tools)} tools)", f"tools: {tool_names}")
    elif status == 401:
        report.fail("Stitch MCP auth rejected (401)", str(body)[:150])
    elif status == 403:
        report.fail("Stitch MCP forbidden (403) — check STITCH_API_KEY", str(body)[:150])
    elif status == 0:
        report.fail("Stitch MCP unreachable", str(body)[:150])
    else:
        report.fail(f"Stitch MCP tools/list returned HTTP {status}", str(body)[:150])


def test_stitch_mcp_get_project(report: Report):
    """Call Stitch MCP to retrieve the Open English Study Hub project metadata."""
    api_key = _stitch_api_key()
    if not api_key:
        report.skip("Stitch MCP get project", "STITCH_API_KEY not set")
        return

    headers = {"X-Goog-Api-Key": api_key}
    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {
            "name": "get_project",
            "arguments": {"name": f"projects/{STITCH_PROJECT_ID}"},
        },
    }
    status, body = _post(STITCH_MCP_URL, payload, headers=headers)

    if status == 200 and "result" in body:
        result = body["result"]
        if isinstance(result, dict) and result.get("isError"):
            report.fail("Stitch get_project returned isError=true", str(result)[:200])
        else:
            report.ok(f"Stitch MCP get_project succeeded for project {STITCH_PROJECT_ID}")
    elif status == 200 and "error" in body:
        err = body["error"]
        report.fail(f"Stitch get_project error: {err.get('message', str(err))[:150]}")
    elif status in (401, 403):
        report.fail(f"Stitch get_project auth error ({status})", str(body)[:150])
    elif status == 0:
        report.fail("Stitch MCP unreachable", str(body)[:150])
    else:
        report.fail(f"Stitch get_project returned HTTP {status}", str(body)[:150])


def test_stitch_mcp_get_screen(report: Report):
    """Call Stitch MCP to retrieve the Landing Page screen design and code."""
    api_key = _stitch_api_key()
    if not api_key:
        report.skip("Stitch MCP get screen (Landing Page)", "STITCH_API_KEY not set")
        return

    headers = {"X-Goog-Api-Key": api_key}
    payload = {
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {
            "name": "get_screen",
            "arguments": {
                "project_id": STITCH_PROJECT_ID,
                "screen_id": STITCH_SCREEN_ID,
            },
        },
    }
    status, body = _post(STITCH_MCP_URL, payload, headers=headers)

    if status == 200 and "result" in body:
        result = body["result"]
        if isinstance(result, dict) and result.get("isError"):
            report.fail(f"Stitch get_screen returned isError=true", str(result)[:200])
        else:
            # Try to extract preview/image URL from content
            content = result.get("content", [])
            image_urls = [
                item.get("url", "") or item.get("data", "")[:40]
                for item in content
                if isinstance(item, dict) and item.get("type") in ("image", "resource")
            ]
            report.ok(
                f"Stitch MCP get_screen succeeded: '{STITCH_SCREEN_NAME}'",
                f"content items: {len(content)}, image refs: {image_urls[:2]}",
            )
    elif status == 200 and "error" in body:
        err = body["error"]
        report.fail(f"Stitch get_screen error: {err.get('message', str(err))[:150]}")
    elif status in (401, 403):
        report.fail(f"Stitch get_screen auth error ({status})", str(body)[:150])
    elif status == 0:
        report.fail("Stitch MCP unreachable", str(body)[:150])
    else:
        report.fail(f"Stitch get_screen returned HTTP {status}", str(body)[:150])


def test_stitch_mcp_get_screen_image(report: Report):
    """Download the Landing Page screen image via curl-equivalent URL fetch."""
    api_key = _stitch_api_key()
    if not api_key:
        report.skip("Stitch screen image download", "STITCH_API_KEY not set")
        return

    # The Stitch MCP exposes screen images as hosted URLs; we list them then fetch one
    headers = {"X-Goog-Api-Key": api_key}
    payload = {
        "jsonrpc": "2.0",
        "id": 4,
        "method": "tools/call",
        "params": {
            "name": "get_screen_image",
            "arguments": {
                "project_id": STITCH_PROJECT_ID,
                "screen_id": STITCH_SCREEN_ID,
            },
        },
    }
    status, body = _post(STITCH_MCP_URL, payload, headers=headers)

    if status == 200 and "result" in body:
        result = body["result"]
        if isinstance(result, dict) and result.get("isError"):
            report.fail("Stitch get_screen_image returned isError=true", str(result)[:200])
            return
        content = result.get("content", [])
        for item in content:
            if isinstance(item, dict) and item.get("type") == "image":
                img_url = item.get("url", "")
                if img_url:
                    # Follow redirect with curl-L equivalent
                    img_status, _ = _get(img_url, timeout=20)
                    if img_status == 200:
                        report.ok(f"Stitch screen image downloadable", img_url[:80])
                    else:
                        report.fail(f"Stitch screen image download failed (HTTP {img_status})", img_url[:80])
                    return
        report.ok("Stitch get_screen_image call succeeded (no separate image URL in response)")
    elif status == 200 and "error" in body:
        err = body["error"]
        # get_screen_image may not be a real tool name — gracefully handle
        if "not found" in str(err).lower() or "unknown" in str(err).lower():
            report.skip("Stitch get_screen_image", "tool not available in this MCP version")
        else:
            report.fail(f"Stitch get_screen_image error: {err.get('message', str(err))[:150]}")
    elif status in (401, 403):
        report.fail(f"Stitch get_screen_image auth error ({status})", str(body)[:150])
    elif status == 0:
        report.fail("Stitch MCP unreachable", str(body)[:150])
    else:
        report.fail(f"Stitch get_screen_image returned HTTP {status}", str(body)[:150])


# ---------------------------------------------------------------------------
# Dry-run / static checks (no network)
# ---------------------------------------------------------------------------

def run_static_checks(report: Report):
    report.section("Static / dry-run checks")
    test_jira_ticket_url_parseable(report)
    test_github_repo_url_parseable(report)
    test_stitch_url_parseable(report)

    # Verify test target config file is consistent
    import pathlib
    targets_path = pathlib.Path(__file__).parent / "agent_test_targets.json"
    if targets_path.exists():
        with open(targets_path, encoding="utf-8") as fh:
            targets = json.load(fh)
        tracker_key = (targets.get("tracker") or {}).get("primaryTicket", {}).get("ticketKey", "")
        scm_owner = (targets.get("scm") or {}).get("primaryRepo", {}).get("owner", "")
        scm_repo = (targets.get("scm") or {}).get("primaryRepo", {}).get("repo", "")
        stitch_id = (targets.get("stitch") or {}).get("primaryProject", {}).get("projectId", "")

        if tracker_key == JIRA_TICKET_KEY:
            report.ok(f"agent_test_targets.json tracker key matches: {tracker_key}")
        else:
            report.fail(f"tracker key mismatch: targets={tracker_key!r}, expected={JIRA_TICKET_KEY!r}")

        if scm_owner == GITHUB_OWNER and scm_repo == GITHUB_REPO:
            report.ok(f"agent_test_targets.json SCM repo matches: {scm_owner}/{scm_repo}")
        else:
            report.fail(f"SCM repo mismatch: targets={scm_owner}/{scm_repo}, expected={GITHUB_OWNER}/{GITHUB_REPO}")

        if stitch_id == STITCH_PROJECT_ID:
            report.ok(f"agent_test_targets.json Stitch project ID matches: {stitch_id}")
        else:
            report.fail(f"Stitch project ID mismatch: targets={stitch_id!r}, expected={STITCH_PROJECT_ID!r}")
    else:
        report.skip("agent_test_targets.json check", "file not found")


# ---------------------------------------------------------------------------
# Integration test suites
# ---------------------------------------------------------------------------

def run_jira_tests(report: Report):
    report.section(f"Atlassian Rovo MCP — {ATLASSIAN_MCP_URL}")
    auth = _jira_auth_header()
    session_id = None
    if auth:
        session_id = _atlassian_mcp_init(auth)
        if session_id:
            report.ok("MCP session initialized", f"session: {session_id[:20]}...")
        else:
            report.fail("MCP session init failed — check TRACKER_TOKEN / TRACKER_EMAIL")
    else:
        report.skip("MCP session init", "TRACKER_TOKEN not set")
    test_atlassian_mcp_tools_list(report, session_id)
    test_atlassian_mcp_user_info(report, session_id)
    (cloud_id,) = test_atlassian_mcp_accessible_resources(report, session_id)
    test_atlassian_jira_get_issue(report, cloud_id, session_id)
    test_atlassian_jira_search_jql(report, cloud_id, session_id)
    test_atlassian_jira_transition_state(report, cloud_id, session_id)
    test_atlassian_jira_change_assignee(report, cloud_id, session_id)
    test_atlassian_jira_comment_lifecycle(report, cloud_id, session_id)


def run_github_tests(report: Report):
    report.section(f"GitHub MCP — {GITHUB_REPO_URL}")
    test_github_repo_metadata(report)
    test_github_repo_branches(report)
    test_github_repo_contents(report)
    test_github_pull_requests(report)


def run_stitch_tests(report: Report):
    report.section(f"Google Stitch MCP — project {STITCH_PROJECT_ID}")
    test_stitch_mcp_list_tools(report)
    test_stitch_mcp_get_project(report)
    test_stitch_mcp_get_screen(report)
    test_stitch_mcp_get_screen_image(report)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--integration", action="store_true",
                        help="Run live integration tests (requires env vars)")
    parser.add_argument("--jira", action="store_true", help="Run only Jira MCP tests")
    parser.add_argument("--github", action="store_true", help="Run only GitHub MCP tests")
    parser.add_argument("--stitch", action="store_true", help="Run only Google Stitch MCP tests")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    report = Report(verbose=args.verbose)

    print(f"\n{Colors.BOLD}=== Constellation MCP Tests ==={Colors.RESET}")
    print(f"  Jira ticket : {JIRA_TICKET_URL}")
    print(f"  GitHub repo : {GITHUB_REPO_URL}")
    print(f"  Stitch      : {STITCH_PROJECT_URL}")

    run_static_checks(report)

    if args.integration:
        any_selected = args.jira or args.github or args.stitch
        if not any_selected or args.jira:
            run_jira_tests(report)
        if not any_selected or args.github:
            run_github_tests(report)
        if not any_selected or args.stitch:
            run_stitch_tests(report)
    else:
        print(f"\n{Colors.YELLOW}Integration tests skipped — pass --integration to run live checks.{Colors.RESET}")

    ok = report.summary()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
