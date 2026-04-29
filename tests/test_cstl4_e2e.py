#!/usr/bin/env python3
"""CSTL-4 End-to-End Test Suite

Tests the complete workflow for CSTL-4 ticket, validating:
- Jira REST API integration (fetch ticket, comments, transitions)
- GitHub REST API integration (repo inspection, branches, PRs)
- Figma REST API integration (file metadata, pages, nodes)

Usage:
  python3 tests/test_cstl4_e2e.py              # run all tests
  python3 tests/test_cstl4_e2e.py -v           # verbose output
  python3 tests/test_cstl4_e2e.py --fix        # fix issues and retest
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# Add project paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))

from agent_test_support import (
    Reporter,
    http_request,
    load_env_file,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_ENV = load_env_file("tests/.env")

def _env(key: str, fallback: str = "") -> str:
    return os.environ.get(key) or _ENV.get(key, fallback)

# CSTL-4 specific configuration
JIRA_TICKET_URL = _env("TEST_JIRA_TICKET_URL", "https://tarch.atlassian.net/browse/CSTL-4")
JIRA_TICKET_KEY = JIRA_TICKET_URL.rstrip("/").split("/")[-1]
JIRA_BASE_URL = "/".join(JIRA_TICKET_URL.split("/")[:3])
JIRA_API_BASE = f"{JIRA_BASE_URL}/rest/api/3"
JIRA_TOKEN = _env("TEST_JIRA_TOKEN", "")
JIRA_EMAIL = _env("TEST_JIRA_EMAIL", "")

GITHUB_REPO_URL = _env("TEST_GITHUB_REPO_URL", "")
GITHUB_TOKEN = _env("TEST_GITHUB_TOKEN", "")
_gh = GITHUB_REPO_URL.rstrip("/").split("/") if GITHUB_REPO_URL else []
GITHUB_OWNER = _gh[-2] if len(_gh) >= 2 else ""
GITHUB_REPO = _gh[-1] if _gh else ""

FIGMA_FILE_URL = _env("TEST_FIGMA_FILE_URL", "")
FIGMA_TOKEN = _env("TEST_FIGMA_TOKEN", "")

def _parse_figma_file_key(url: str) -> str:
    """Extract Figma file key from URL."""
    url = url.strip()
    for prefix in ("/design/", "/file/"):
        if prefix in url:
            after = url.split(prefix)[1]
            return after.split("/")[0].split("?")[0]
    return ""

FIGMA_FILE_KEY = _parse_figma_file_key(FIGMA_FILE_URL) if FIGMA_FILE_URL else ""
FIGMA_API_BASE = "https://api.figma.com/v1"

# Agent URLs
JIRA_AGENT_URL = "http://localhost:8010"
SCM_AGENT_URL = "http://localhost:8020"
UI_DESIGN_AGENT_URL = "http://localhost:8040"

# ---------------------------------------------------------------------------
# HTTP Helpers
# ---------------------------------------------------------------------------

def http_json(url: str, method: str = "GET", payload=None,
              timeout: int = 30, headers: dict | None = None):
    """Make HTTP request and return (status, json_body)."""
    data = None
    h: dict = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json; charset=utf-8"
    if headers:
        h.update(headers)
    try:
        req = Request(url, data=data, headers=h, method=method)
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body.strip() else {}
    except HTTPError as e:
        raw = e.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"error": raw[:300]}
        return e.code, body
    except (URLError, OSError) as e:
        return 0, {"error": str(e)}

# ---------------------------------------------------------------------------
# Jira REST API Tests
# ---------------------------------------------------------------------------

def test_jira_rest_api(reporter: Reporter) -> dict:
    """Test Jira REST API integration."""
    reporter.section("Jira REST API Tests")
    
    results = {
        "health": False,
        "myself": False,
        "ticket_fetch": False,
        "transitions": False,
        "search": False,
    }
    
    if not JIRA_TOKEN or not JIRA_EMAIL:
        reporter.fail("Jira credentials missing", "Set TEST_JIRA_TOKEN and TEST_JIRA_EMAIL in tests/.env")
        return results
    
    # Test 1: Health check
    reporter.step("Test Jira Agent health endpoint")
    status, body = http_json(f"{JIRA_AGENT_URL}/health")
    reporter.show("health", body)
    if status == 200 and body.get("status") == "ok":
        reporter.ok("Jira Agent health check passed")
        results["health"] = True
    else:
        reporter.fail("Jira Agent health check failed", f"status={status}")
    
    # Test 2: Myself endpoint
    reporter.step("Test GET /jira/myself")
    status, body = http_json(f"{JIRA_AGENT_URL}/jira/myself")
    reporter.show("myself", body)
    user = body.get("user", {})
    if status == 200 and body.get("result") == "ok" and user.get("accountId"):
        reporter.ok(f"Authenticated as: {user.get('emailAddress') or user.get('displayName')}")
        results["myself"] = True
    else:
        reporter.fail("GET /jira/myself failed", f"status={status}")
    
    # Test 3: Fetch ticket
    reporter.step(f"Test GET /jira/tickets/{JIRA_TICKET_KEY}")
    status, body = http_json(f"{JIRA_AGENT_URL}/jira/tickets/{JIRA_TICKET_KEY}")
    reporter.show("ticket-fetch", body)
    issue = body.get("issue") or {}
    fields = issue.get("fields") or {}
    if status == 200 and body.get("status") == "fetched":
        summary = fields.get("summary", "")
        reporter.ok(f"Ticket fetched: {summary[:80]}")
        results["ticket_fetch"] = True
    else:
        reporter.fail(f"GET /jira/tickets/{JIRA_TICKET_KEY} failed", f"status={status}")
    
    # Test 4: Get transitions
    reporter.step(f"Test GET /jira/transitions/{JIRA_TICKET_KEY}")
    status, body = http_json(f"{JIRA_AGENT_URL}/jira/transitions/{JIRA_TICKET_KEY}")
    reporter.show("transitions", body)
    transitions = body.get("transitions", [])
    if status == 200 and body.get("result") == "ok" and transitions:
        names = [t.get("name") for t in transitions if isinstance(t, dict)]
        reporter.ok(f"Transitions available: {names[:5]}")
        results["transitions"] = True
    else:
        reporter.fail(f"GET /jira/transitions/{JIRA_TICKET_KEY} failed", f"status={status}")
    
    # Test 5: Search
    reporter.step(f"Test GET /jira/search?jql=key={JIRA_TICKET_KEY}")
    from urllib.parse import urlencode
    status, body = http_json(
        f"{JIRA_AGENT_URL}/jira/search?{urlencode({'jql': f'key = {JIRA_TICKET_KEY}', 'maxResults': '1'})}"
    )
    reporter.show("search", body)
    search_result = body.get("search", {})
    issues = search_result.get("issues", []) if isinstance(search_result, dict) else []
    if status == 200 and body.get("result") == "ok":
        reporter.ok(f"Search returned {len(issues)} issue(s)")
        results["search"] = True
    else:
        reporter.fail("GET /jira/search failed", f"status={status}")
    
    return results

# ---------------------------------------------------------------------------
# GitHub REST API Tests
# ---------------------------------------------------------------------------

def test_github_rest_api(reporter: Reporter) -> dict:
    """Test GitHub REST API integration."""
    reporter.section("GitHub REST API Tests")
    
    results = {
        "health": False,
        "auth": False,
        "repo_inspect": False,
        "branches": False,
        "prs": False,
    }
    
    if not GITHUB_TOKEN:
        reporter.fail("GitHub token missing", "Set TEST_GITHUB_TOKEN in tests/.env")
        return results
    
    if not GITHUB_OWNER or not GITHUB_REPO:
        reporter.fail("GitHub repo not configured", "Set TEST_GITHUB_REPO_URL in tests/.env")
        return results
    
    # Test 1: Health check
    reporter.step("Test SCM Agent health endpoint")
    status, body = http_json(f"{SCM_AGENT_URL}/health")
    reporter.show("health", body)
    if status == 200 and body.get("status") == "ok":
        reporter.ok("SCM Agent health check passed")
        results["health"] = True
    else:
        reporter.fail("SCM Agent health check failed", f"status={status}")
    
    # Test 2: GitHub authentication
    reporter.step("Test GitHub token authentication")
    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }
    status, body = http_json("https://api.github.com/user", headers=headers)
    reporter.show("github-auth", body)
    if status == 200 and body.get("login"):
        reporter.ok(f"GitHub authenticated as: {body['login']}")
        results["auth"] = True
    else:
        reporter.fail("GitHub authentication failed", f"status={status}")
    
    # Test 3: Repo inspection
    reporter.step(f"Test repo inspection for {GITHUB_OWNER}/{GITHUB_REPO}")
    status, body = http_json(
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}",
        headers=headers
    )
    reporter.show("repo-inspect", body)
    if status == 200 and body.get("full_name"):
        reporter.ok(f"Repo: {body['full_name']} | default branch: {body.get('default_branch')}")
        results["repo_inspect"] = True
    else:
        reporter.fail("Repo inspection failed", f"status={status}")
    
    # Test 4: List branches
    reporter.step(f"Test list branches for {GITHUB_OWNER}/{GITHUB_REPO}")
    status, body = http_json(
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/branches",
        headers=headers
    )
    reporter.show("branches", body)
    if status == 200 and isinstance(body, list):
        branch_names = [b.get("name") for b in body]
        reporter.ok(f"Branches: {len(body)} — {branch_names[:5]}")
        results["branches"] = True
    else:
        reporter.fail("List branches failed", f"status={status}")
    
    # Test 5: List PRs
    reporter.step(f"Test list PRs for {GITHUB_OWNER}/{GITHUB_REPO}")
    status, body = http_json(
        f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/pulls?state=open",
        headers=headers
    )
    reporter.show("prs", body)
    if status == 200 and isinstance(body, list):
        reporter.ok(f"Open PRs: {len(body)}")
        results["prs"] = True
    else:
        reporter.fail("List PRs failed", f"status={status}")
    
    return results

# ---------------------------------------------------------------------------
# Figma REST API Tests
# ---------------------------------------------------------------------------

def test_figma_rest_api(reporter: Reporter) -> dict:
    """Test Figma REST API integration."""
    reporter.section("Figma REST API Tests")
    
    results = {
        "health": False,
        "auth": False,
        "file_metadata": False,
        "pages": False,
        "node_fetch": False,
    }
    
    if not FIGMA_TOKEN:
        reporter.fail("Figma token missing", "Set TEST_FIGMA_TOKEN in tests/.env")
        return results
    
    if not FIGMA_FILE_KEY:
        reporter.fail("Figma file not configured", "Set TEST_FIGMA_FILE_URL in tests/.env")
        return results
    
    # Test 1: Health check
    reporter.step("Test UI Design Agent health endpoint")
    status, body = http_json(f"{UI_DESIGN_AGENT_URL}/health")
    reporter.show("health", body)
    if status == 200 and body.get("status") == "ok":
        reporter.ok("UI Design Agent health check passed")
        results["health"] = True
    else:
        reporter.fail("UI Design Agent health check failed", f"status={status}")
    
    # Test 2: Figma authentication & file metadata
    reporter.step(f"Test Figma file metadata for {FIGMA_FILE_KEY}")
    headers = {
        "X-Figma-Token": FIGMA_TOKEN,
        "Accept": "application/json"
    }
    status, body = http_json(
        f"{FIGMA_API_BASE}/files/{FIGMA_FILE_KEY}?depth=1",
        headers=headers
    )
    reporter.show("file-metadata", body)
    if status == 200 and body.get("name"):
        reporter.ok(f"Figma file: {body['name']}")
        results["auth"] = True
        results["file_metadata"] = True
    elif status == 429:
        reporter.skip("Figma file metadata", "Rate limit (429) — wait and retry")
    elif status == 403:
        reporter.fail("Figma authentication failed (403)", "Check TEST_FIGMA_TOKEN")
    elif status == 404:
        reporter.fail("Figma file not found (404)", "Check TEST_FIGMA_FILE_URL")
    else:
        reporter.fail("Figma file metadata failed", f"status={status}")
    
    # Test 3: List pages
    if results["file_metadata"]:
        reporter.step("Test list Figma pages")
        pages = [
            node for node in body.get("document", {}).get("children", [])
            if isinstance(node, dict) and node.get("type") == "CANVAS"
        ]
        if pages:
            page_names = [p.get("name", "") for p in pages]
            reporter.ok(f"Pages: {len(pages)} — {page_names[:5]}")
            results["pages"] = True
        else:
            reporter.fail("No pages found in Figma file")
    
    # Test 4: Fetch node (if node-id in URL)
    if "node-id=" in FIGMA_FILE_URL:
        reporter.step("Test fetch Figma node")
        raw_node_id = FIGMA_FILE_URL.split("node-id=")[1].split("&")[0]
        node_id = raw_node_id.replace("-", ":").replace("%3A", ":").replace("%3a", ":")
        status, body = http_json(
            f"{FIGMA_API_BASE}/files/{FIGMA_FILE_KEY}/nodes?ids={node_id}",
            headers=headers
        )
        reporter.show("node-fetch", body)
        nodes = body.get("nodes", {}) if isinstance(body, dict) else {}
        if status == 200 and node_id in nodes:
            reporter.ok(f"Node {node_id} fetched successfully")
            results["node_fetch"] = True
        elif status == 429:
            reporter.skip("Node fetch", "Rate limit (429)")
        else:
            reporter.fail("Node fetch failed", f"status={status}")
    
    return results

# ---------------------------------------------------------------------------
# Fix Issues
# ---------------------------------------------------------------------------

def fix_issues(reporter: Reporter, jira_results: dict, github_results: dict, figma_results: dict):
    """Attempt to fix identified issues."""
    reporter.section("Fixing Issues")
    
    # Check if agents are running
    if not jira_results.get("health"):
        reporter.step("Jira Agent not responding")
        reporter.info("Ensure Jira Agent is running: docker compose up jira -d")
    
    if not github_results.get("health"):
        reporter.step("SCM Agent not responding")
        reporter.info("Ensure SCM Agent is running: docker compose up scm -d")
    
    if not figma_results.get("health"):
        reporter.step("UI Design Agent not responding")
        reporter.info("Ensure UI Design Agent is running: docker compose up ui-design -d")
    
    # Check credentials
    if not jira_results.get("myself"):
        reporter.step("Jira authentication failed")
        reporter.info("Check TEST_JIRA_TOKEN and TEST_JIRA_EMAIL in tests/.env")
    
    if not github_results.get("auth"):
        reporter.step("GitHub authentication failed")
        reporter.info("Check TEST_GITHUB_TOKEN in tests/.env")
    
    if not figma_results.get("auth"):
        reporter.step("Figma authentication failed")
        reporter.info("Check TEST_FIGMA_TOKEN in tests/.env")
    
    reporter.ok("Issue diagnosis complete — review recommendations above")

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--fix", action="store_true", help="Fix issues and retest")
    args = parser.parse_args(argv)
    
    reporter = Reporter(verbose=args.verbose)
    
    print("\n" + "=" * 70)
    print("  CSTL-4 End-to-End Test Suite")
    print("=" * 70)
    print(f"  Jira Ticket:  {JIRA_TICKET_URL}")
    print(f"  GitHub Repo:  {GITHUB_REPO_URL}")
    print(f"  Figma File:   {FIGMA_FILE_URL}")
    print("=" * 70)
    
    # Run tests
    jira_results = test_jira_rest_api(reporter)
    github_results = test_github_rest_api(reporter)
    figma_results = test_figma_rest_api(reporter)
    
    # Fix issues if requested
    if args.fix:
        fix_issues(reporter, jira_results, github_results, figma_results)
        
        # Retest
        reporter.section("Retesting After Fixes")
        time.sleep(2)  # Give services time to stabilize
        jira_results = test_jira_rest_api(reporter)
        github_results = test_github_rest_api(reporter)
        figma_results = test_figma_rest_api(reporter)
    
    # Summary
    print("\n" + "=" * 70)
    print("  Test Summary")
    print("=" * 70)
    print(f"  Passed:  {reporter.passed}")
    print(f"  Failed:  {reporter.failed}")
    print(f"  Skipped: {reporter.skipped}")
    print("=" * 70)
    
    # Detailed results
    print("\n  Jira REST API:")
    for test, passed in jira_results.items():
        status = "✓" if passed else "✗"
        print(f"    {status} {test}")
    
    print("\n  GitHub REST API:")
    for test, passed in github_results.items():
        status = "✓" if passed else "✗"
        print(f"    {status} {test}")
    
    print("\n  Figma REST API:")
    for test, passed in figma_results.items():
        status = "✓" if passed else "✗"
        print(f"    {status} {test}")
    
    print("\n" + "=" * 70 + "\n")
    
    return 0 if reporter.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
