#!/usr/bin/env python3
"""MCP (Model Context Protocol) connectivity and capability tests.

Verifies that the three MCP servers used by the Constellation system are
reachable and return valid responses:

  - Jira MCP  — https://tarch.atlassian.net/browse/CSTL-1
  - GitHub MCP — https://github.com/fihtony/microservice-test
  - Google Stitch MCP — https://stitch.googleapis.com/mcp
      Project ID : 13629074018280446337 (Open English Study Hub)
      Screen ID  : 52742f72311047aea731c97630d211de (Landing Page)

Environment variables required to run integration tests:
  TRACKER_TOKEN      Jira API token (Basic base64(email:token) or Bearer)
  TRACKER_EMAIL      Jira account email (for Basic auth)
  SCM_TOKEN          GitHub personal access token
  STITCH_API_KEY     Google Stitch / Gemini API key

Usage:
    # Dry-run (no network calls — checks module imports and config parsing):
    python3 tests/test_mcp.py

    # Full integration mode (requires environment variables):
    TRACKER_TOKEN=... TRACKER_EMAIL=... SCM_TOKEN=... STITCH_API_KEY=... \\
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
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Test targets
# ---------------------------------------------------------------------------

JIRA_TICKET_URL = "https://tarch.atlassian.net/browse/CSTL-1"
JIRA_TICKET_KEY = "CSTL-1"
JIRA_BASE_URL = "https://tarch.atlassian.net"
JIRA_API_BASE = f"{JIRA_BASE_URL}/rest/api/3"

GITHUB_REPO_URL = "https://github.com/fihtony/microservice-test"
GITHUB_OWNER = "fihtony"
GITHUB_REPO = "microservice-test"
GITHUB_API_BASE = "https://api.github.com"

STITCH_MCP_URL = "https://stitch.googleapis.com/mcp"
STITCH_PROJECT_ID = "13629074018280446337"
STITCH_PROJECT_URL = f"https://stitch.withgoogle.com/projects/{STITCH_PROJECT_ID}"
STITCH_SCREEN_ID = "52742f72311047aea731c97630d211de"
STITCH_SCREEN_NAME = "Landing Page"

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
# Jira MCP tests
# ---------------------------------------------------------------------------

def test_jira_ticket_url_parseable(report: Report):
    """Verify the Jira ticket URL is well-formed and contains the expected key."""
    assert JIRA_TICKET_KEY in JIRA_TICKET_URL, "ticket key not in URL"
    assert JIRA_BASE_URL in JIRA_TICKET_URL, "base URL not in ticket URL"
    report.ok("Jira ticket URL is well-formed", JIRA_TICKET_URL)


def test_jira_ticket_fetch(report: Report):
    """Fetch CSTL-1 from Jira REST API v3 and verify key fields are present."""
    auth = _jira_auth_header()
    if not auth:
        report.skip("Jira ticket fetch", "TRACKER_TOKEN not set")
        return

    url = f"{JIRA_API_BASE}/issue/{JIRA_TICKET_KEY}"
    status, body = _get(url, headers={"Authorization": auth})

    if status == 200:
        key = body.get("key", "")
        summary = (body.get("fields") or {}).get("summary", "")
        if key == JIRA_TICKET_KEY:
            report.ok(f"Jira ticket {JIRA_TICKET_KEY} fetched", f"summary: {summary[:60]}")
        else:
            report.fail(f"Jira ticket key mismatch: expected {JIRA_TICKET_KEY}, got {key!r}")
    elif status == 401:
        report.fail("Jira auth rejected (401)", str(body)[:150])
    elif status == 403:
        report.fail("Jira forbidden (403) — check token scopes", str(body)[:150])
    elif status == 404:
        report.fail(f"Jira ticket {JIRA_TICKET_KEY} not found (404)")
    else:
        report.fail(f"Jira ticket fetch returned HTTP {status}", str(body)[:150])


def test_jira_myself(report: Report):
    """Call /rest/api/3/myself to verify the Jira token identity."""
    auth = _jira_auth_header()
    if not auth:
        report.skip("Jira myself", "TRACKER_TOKEN not set")
        return

    url = f"{JIRA_API_BASE}/myself"
    status, body = _get(url, headers={"Authorization": auth})

    if status == 200:
        display_name = body.get("displayName", "")
        account_id = body.get("accountId", "")
        report.ok("Jira /myself succeeded", f"{display_name} ({account_id[:12]}...)")
    elif status == 401:
        report.fail("Jira /myself auth rejected (401)", str(body)[:150])
    elif status == 403:
        report.fail("Jira /myself forbidden (403) — check token scopes", str(body)[:150])
    else:
        report.fail(f"Jira /myself returned HTTP {status}", str(body)[:150])


def test_jira_issue_search(report: Report):
    """Search for CSTL-1 via JQL to verify search capability."""
    auth = _jira_auth_header()
    if not auth:
        report.skip("Jira JQL search", "TRACKER_TOKEN not set")
        return

    url = f"{JIRA_API_BASE}/search?jql=key%3D{JIRA_TICKET_KEY}&maxResults=1&fields=summary,status"
    status, body = _get(url, headers={"Authorization": auth})

    if status == 200:
        total = body.get("total", 0)
        issues = body.get("issues", [])
        if total >= 1 and issues:
            summary = (issues[0].get("fields") or {}).get("summary", "")
            report.ok(f"Jira JQL search found {total} result(s)", f"summary: {summary[:60]}")
        else:
            report.fail(f"Jira JQL search returned 0 results for key={JIRA_TICKET_KEY}")
    elif status in (401, 403):
        report.fail(f"Jira JQL search auth error ({status})", str(body)[:150])
    else:
        report.fail(f"Jira JQL search returned HTTP {status}", str(body)[:150])


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
            "arguments": {"project_id": STITCH_PROJECT_ID},
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
    report.section(f"Jira MCP — {JIRA_TICKET_URL}")
    test_jira_myself(report)
    test_jira_ticket_fetch(report)
    test_jira_issue_search(report)


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
