#!/usr/bin/env python3
"""GitHub MCP integration tests — GitHubMCPProvider + raw remote MCP server verification.

Tests the GitHubMCPProvider class (scm/providers/github_mcp.py) against every
SCM capability required by the Constellation system.  Also validates the raw
remote GitHub MCP server (https://api.githubcopilot.com/mcp/) independently.

All configuration is read EXCLUSIVELY from tests/.env.  No agent .env files
are consulted.  Fail-fast with a clear error if required keys are missing.

Required keys in tests/.env:
  TEST_GITHUB_REPO_URL   Full repo URL (e.g. https://github.com/owner/repo)
  TEST_GITHUB_TOKEN      GitHub personal access token (scope: repo)

Usage:
    python3 tests/test_github_mcp.py              # dry-run (no network)
    python3 tests/test_github_mcp.py --integration [-v]
    python3 tests/test_github_mcp.py --integration --raw      # raw HTTP MCP only
    python3 tests/test_github_mcp.py --integration --provider # GitHubMCPProvider only
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

_HERE = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from agent_test_support import Reporter, load_env_file, unique_suffix
from agent_test_targets import scm_owner, scm_repo_slug

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_token() -> str:
    tests_env = load_env_file("tests/.env")
    token = tests_env.get("TEST_GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("ERROR: TEST_GITHUB_TOKEN not set in tests/.env — cannot run tests")
    return token


GITHUB_OWNER = scm_owner()
GITHUB_REPO = scm_repo_slug()
GITHUB_REPO_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}"
GITHUB_API_BASE = "https://api.github.com"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _github_auth_header() -> str | None:
    return f"Bearer {_load_token()}"


def _github_token() -> str:
    return _load_token()


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
# GitHub MCP client — remote HTTP server (https://api.githubcopilot.com/mcp/)
# ---------------------------------------------------------------------------

class GitHubMCPClient:
    """Client for the remote GitHub MCP server (Streamable HTTP transport).

    Connects to https://api.githubcopilot.com/mcp/ with PAT authentication.
    Implements the MCP initialize handshake and session management.

    Usage:
        with GitHubMCPClient(token) as client:
            tools = client.tools_list()
            resp  = client.call_tool("get_file_contents", {...})
    """

    MCP_URL = "https://api.githubcopilot.com/mcp/"
    MCP_PROTOCOL_VERSION = "2024-11-05"

    def __init__(self, token: str, timeout: int = 60):
        self.token = token
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._req_id = 0
        self._session_id: str | None = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    def _headers(self) -> dict:
        h = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            h["Mcp-Session-Id"] = self._session_id
        return h

    def _post(self, payload: dict, timeout: int | None = None) -> dict:
        data = json.dumps(payload).encode("utf-8")
        req = Request(self.MCP_URL, data=data, headers=self._headers(), method="POST")
        try:
            with urlopen(req, timeout=timeout or self.timeout) as resp:
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
            try:
                return json.loads(raw)
            except Exception:
                return {"error": {"code": exc.code, "message": raw[:300]}}
        except URLError as exc:
            return {"error": {"code": -1, "message": str(exc)}}

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
                        if isinstance(item, dict) and (expected_id is None or item.get("id") == expected_id):
                            return item
                elif isinstance(data, dict):
                    if expected_id is None or data.get("id") == expected_id:
                        return data
            except json.JSONDecodeError:
                pass
        return {"error": {"code": -1, "message": "no matching response in SSE stream"}}

    def _rpc(self, method: str, params: dict, timeout: int | None = None) -> dict:
        self._req_id += 1
        return self._post({
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
            "params": params,
        }, timeout)

    def start(self) -> None:
        """Perform MCP initialize handshake with the remote server."""
        resp = self._rpc("initialize", {
            "protocolVersion": self.MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "constellation-test", "version": "1.0"},
        })
        if "error" in resp:
            raise RuntimeError(f"GitHub MCP init failed: {resp['error']}")
        # Send notifications/initialized (fire-and-forget)
        try:
            payload = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}
            data = json.dumps(payload).encode("utf-8")
            req = Request(self.MCP_URL, data=data, headers=self._headers(), method="POST")
            with urlopen(req, timeout=10):
                pass
        except Exception:
            pass

    def tools_list(self) -> list:
        resp = self._rpc("tools/list", {})
        return (resp.get("result") or {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict, timeout: int = 60) -> dict:
        return self._rpc("tools/call", {"name": name, "arguments": arguments}, timeout=timeout)

    def extract_text(self, tool_resp: dict) -> str:
        result = tool_resp.get("result") or {}
        if isinstance(result, dict):
            parts = [
                item.get("text", "")
                for item in result.get("content", [])
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            return "\n".join(parts)
        return str(result)

    def is_error(self, tool_resp: dict) -> bool:
        return bool((tool_resp.get("result") or {}).get("isError")) or "error" in tool_resp

    def stop(self) -> None:
        """Delete the remote session (best-effort)."""
        if self._session_id:
            try:
                req = Request(self.MCP_URL, headers=self._headers(), method="DELETE")
                with urlopen(req, timeout=5):
                    pass
            except Exception:
                pass
            self._session_id = None


# ---------------------------------------------------------------------------
# Raw MCP server sanity-check functions
# ---------------------------------------------------------------------------

def test_github_mcp_server_sanity(reporter: Reporter) -> None:
    """Quick static check that target repo URL is well-formed."""
    if GITHUB_OWNER and GITHUB_REPO and GITHUB_OWNER in GITHUB_REPO_URL:
        reporter.ok(f"Target repo URL well-formed: {GITHUB_REPO_URL}")
    else:
        reporter.fail("Target repo URL not set — add TEST_GITHUB_REPO_URL to tests/.env")


# ---------------------------------------------------------------------------
# Raw remote MCP server test functions
# ---------------------------------------------------------------------------

def test_github_mcp_tools_list(reporter: Reporter, client: GitHubMCPClient) -> None:
    tools = client.tools_list()
    if not tools:
        reporter.fail("GitHub MCP tools/list returned empty list")
        return
    tool_names = sorted(t.get("name", "") for t in tools)
    reporter.ok(f"GitHub MCP server ready — {len(tools)} tools (sample: {tool_names[:4]})")


def test_github_mcp_get_repository(reporter: Reporter, client: GitHubMCPClient) -> None:
    resp = client.call_tool("search_repositories", {
        "query": f"repo:{GITHUB_OWNER}/{GITHUB_REPO}",
    })
    if client.is_error(resp):
        reporter.fail("search_repositories failed", client.extract_text(resp)[:200])
        return
    text = client.extract_text(resp)
    if GITHUB_REPO in text or GITHUB_OWNER in text:
        reporter.ok(f"search_repositories: {GITHUB_OWNER}/{GITHUB_REPO} found")
    else:
        reporter.fail("search_repositories: expected repo data not in response", text[:200])


def test_github_mcp_list_branches(reporter: Reporter, client: GitHubMCPClient) -> None:
    resp = client.call_tool("list_branches", {"owner": GITHUB_OWNER, "repo": GITHUB_REPO})
    if client.is_error(resp):
        reporter.fail("list_branches failed", client.extract_text(resp)[:200])
        return
    text = client.extract_text(resp)
    if text.strip():
        reporter.ok("list_branches: branch data returned")
    else:
        reporter.fail("list_branches: empty response")


def test_github_mcp_get_file_contents(reporter: Reporter, client: GitHubMCPClient) -> None:
    resp = client.call_tool("get_file_contents", {
        "owner": GITHUB_OWNER,
        "repo": GITHUB_REPO,
        "path": "README.md",
    })
    if client.is_error(resp):
        reporter.fail("get_file_contents (README.md) failed", client.extract_text(resp)[:200])
        return
    text = client.extract_text(resp)
    if text.strip():
        reporter.ok("get_file_contents: README.md retrieved")
    else:
        reporter.fail("get_file_contents: empty content returned")


def test_github_mcp_list_pull_requests(reporter: Reporter, client: GitHubMCPClient) -> None:
    resp = client.call_tool("list_pull_requests", {
        "owner": GITHUB_OWNER,
        "repo": GITHUB_REPO,
        "state": "open",
    })
    if client.is_error(resp):
        reporter.fail("list_pull_requests failed", client.extract_text(resp)[:200])
        return
    text = client.extract_text(resp)
    reporter.ok("list_pull_requests: call succeeded" + (
        f" — {text[:80]}" if text.strip() else " (no open PRs)"
    ))


def test_github_mcp_search_code(reporter: Reporter, client: GitHubMCPClient) -> None:
    resp = client.call_tool("search_code", {
        "query": f"repo:{GITHUB_OWNER}/{GITHUB_REPO} README",
    })
    if client.is_error(resp):
        err_text = client.extract_text(resp)
        if "not found" in err_text.lower() or "unknown tool" in err_text.lower():
            reporter.skip("search_code", "tool not in enabled toolsets")
        else:
            reporter.fail("search_code failed", err_text[:200])
        return
    reporter.ok("search_code: call succeeded")


# ---------------------------------------------------------------------------
# GitHubMCPProvider end-to-end tests (full Constellation capability coverage)
# ---------------------------------------------------------------------------

def run_provider_tests(reporter: Reporter) -> None:
    """Test GitHubMCPProvider for all Constellation-required SCM capabilities."""
    reporter.section(f"GitHubMCPProvider (remote HTTP) — {GITHUB_REPO_URL}")

    token = _github_token()

    from scm.providers.github_mcp import GitHubMCPProvider
    p = GitHubMCPProvider(token=token)

    suffix = unique_suffix()
    branch = f"agent/mcp-test/{suffix}"
    pr_id = None
    default_branch = "main"

    try:
        # TC-MCP-01: get_repo
        reporter.step("TC-MCP-01  get_repo()")
        repo_info, status = p.get_repo(GITHUB_OWNER, GITHUB_REPO)
        if status == "ok" and repo_info.get("fullName"):
            default_branch = repo_info.get("defaultBranch", "main")
            reporter.ok(f"get_repo(): {repo_info['fullName']} — default: {default_branch}")
        else:
            reporter.fail(f"get_repo() status={status!r}", str(repo_info)[:100])
            return

        # TC-MCP-02: list_branches
        reporter.step("TC-MCP-02  list_branches()")
        branches, status = p.list_branches(GITHUB_OWNER, GITHUB_REPO)
        if status == "ok" and branches:
            reporter.ok(f"list_branches(): {len(branches)} branches — {[b['name'] for b in branches[:4]]}")
        else:
            reporter.fail(f"list_branches() status={status!r}", str(branches)[:100])

        # TC-MCP-03: create_branch
        reporter.step(f"TC-MCP-03  create_branch() → {branch}")
        result, status = p.create_branch(GITHUB_OWNER, GITHUB_REPO, branch, default_branch)
        if "created" in status:
            reporter.ok(f"create_branch(): {result.get('htmlUrl', branch)}")
        else:
            reporter.fail(f"create_branch() status={status!r}", str(result)[:150])
            return

        # TC-MCP-04: push_files
        reporter.step("TC-MCP-04  push_files()")
        files = [{"path": f"agent-tests/{suffix}/mcp-provider.txt",
                  "content": f"MCP provider test — {suffix}\nTimestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ')}"}]
        result, status = p.push_files(GITHUB_OWNER, GITHUB_REPO, branch, default_branch,
                                      files, f"chore: MCP provider test {suffix}")
        if status == "pushed":
            reporter.ok(f"push_files(): {result.get('htmlUrl', '')}")
        else:
            reporter.fail(f"push_files() status={status!r}", str(result)[:150])
            return

        # TC-MCP-05: create_pr
        reporter.step("TC-MCP-05  create_pr()")
        pr, status = p.create_pr(GITHUB_OWNER, GITHUB_REPO, branch, default_branch,
                                  f"[MCP Test] {suffix}",
                                  "Automated MCP provider test PR — safe to close.")
        if "created" in status and pr.get("id"):
            pr_id = pr["id"]
            reporter.ok(f"create_pr(): PR #{pr_id} — {pr.get('htmlUrl', '')}")
        else:
            reporter.fail(f"create_pr() status={status!r}", str(pr)[:150])
            return

        # TC-MCP-06: get_pr
        reporter.step(f"TC-MCP-06  get_pr({pr_id})")
        pr_fetched, status = p.get_pr(GITHUB_OWNER, GITHUB_REPO, pr_id)
        if status == "ok" and pr_fetched.get("id") == pr_id:
            reporter.ok(f"get_pr(): state={pr_fetched.get('state')}, from={pr_fetched.get('fromBranch')}")
        else:
            reporter.fail(f"get_pr() status={status!r}", str(pr_fetched)[:100])

        # TC-MCP-07: list_prs
        reporter.step("TC-MCP-07  list_prs()")
        prs, status = p.list_prs(GITHUB_OWNER, GITHUB_REPO, "open")
        ids = [x["id"] for x in prs]
        if status == "ok" and pr_id in ids:
            reporter.ok(f"list_prs(): {len(prs)} open — #{pr_id} found")
        else:
            reporter.fail(f"list_prs() did not include #{pr_id}", f"ids={ids}")

        # TC-MCP-08: add_pr_comment
        reporter.step("TC-MCP-08  add_pr_comment()")
        comment_body = f"[MCP Test] Automated comment from test_github_mcp.py — {suffix}"
        comment, status = p.add_pr_comment(GITHUB_OWNER, GITHUB_REPO, pr_id, comment_body)
        if "created" in status and comment.get("id"):
            reporter.ok(f"add_pr_comment(): #{comment['id']} — {comment.get('htmlUrl','')}")
        else:
            reporter.fail(f"add_pr_comment() status={status!r}", str(comment)[:100])

        # TC-MCP-09: list_pr_comments
        reporter.step("TC-MCP-09  list_pr_comments()")
        comments, status = p.list_pr_comments(GITHUB_OWNER, GITHUB_REPO, pr_id)
        bodies = [c.get("body", "") for c in comments]
        if status == "ok" and any(suffix in b for b in bodies):
            reporter.ok(f"list_pr_comments(): {len(comments)} comment(s) — test comment found")
        else:
            reporter.fail("list_pr_comments() did not find test comment",
                          f"status={status!r} bodies={bodies[:2]}")

        # TC-MCP-10: search_repos
        reporter.step("TC-MCP-10  search_repos()")
        results, status = p.search_repos(f"repo:{GITHUB_OWNER}/{GITHUB_REPO}", limit=5)
        if status == "ok" and any(r.get("repo") == GITHUB_REPO for r in results):
            reporter.ok(f"search_repos(): found {GITHUB_OWNER}/{GITHUB_REPO}")
        else:
            reporter.fail(f"search_repos() did not find repo",
                          f"status={status!r} results={[r.get('repo') for r in results]}")

    finally:
        p.close()


# ---------------------------------------------------------------------------
# Raw MCP server verification (tools/list + key tool calls)
# ---------------------------------------------------------------------------

def run_raw_mcp_tests(reporter: Reporter) -> None:
    reporter.section(f"Raw GitHub MCP Server (remote HTTP) — {GITHUB_REPO_URL}")

    token = _github_token()

    reporter.step("Connecting to remote GitHub MCP server …")
    try:
        with GitHubMCPClient(token, timeout=60) as client:
            reporter.ok("Remote GitHub MCP server connected and initialized")
            test_github_mcp_tools_list(reporter, client)
            test_github_mcp_get_repository(reporter, client)
            test_github_mcp_list_branches(reporter, client)
            test_github_mcp_get_file_contents(reporter, client)
            test_github_mcp_list_pull_requests(reporter, client)
            test_github_mcp_search_code(reporter, client)
    except RuntimeError as exc:
        reporter.fail("Remote GitHub MCP server connection failed", str(exc))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--integration", action="store_true",
                        help="Run live integration tests (requires credentials)")
    parser.add_argument("--raw", action="store_true",
                        help="Run raw remote MCP server verification only")
    parser.add_argument("--provider", action="store_true",
                        help="Run GitHubMCPProvider tests only")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    reporter = Reporter(verbose=args.verbose)

    print("\n" + "=" * 60)
    print("  GitHub MCP Tests  (GitHubMCPProvider + remote HTTP server)")
    print("=" * 60)
    print(f"  Repo    : {GITHUB_REPO_URL}")
    print(f"  MCP URL : https://api.githubcopilot.com/mcp/")

    reporter.section("Static / dry-run checks")
    test_github_mcp_server_sanity(reporter)

    if not args.integration:
        print("\n\033[93mIntegration tests skipped — pass --integration to run live checks.\033[0m")
    else:
        any_selected = args.raw or args.provider
        if not any_selected or args.provider:
            run_provider_tests(reporter)
        if not any_selected or args.raw:
            run_raw_mcp_tests(reporter)

    print(f"\nPassed: {reporter.passed}  Failed: {reporter.failed}  Skipped: {reporter.skipped}")
    return 0 if reporter.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
