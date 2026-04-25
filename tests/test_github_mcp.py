#!/usr/bin/env python3
"""GitHub MCP integration tests — official Docker MCP server + GitHub REST API.

Tests use the official GitHub MCP server image (ghcr.io/github/github-mcp-server)
via STDIO JSON-RPC 2.0, and also validate the GitHub REST API directly.

Required keys in tests/.env:
  TEST_GITHUB_REPO_URL   Full repo URL (e.g. https://github.com/owner/repo)
  TEST_GITHUB_TOKEN      GitHub personal access token (scope: repo)

Usage:
    python3 tests/test_github_mcp.py              # dry-run (no network)
    python3 tests/test_github_mcp.py --integration [-v]
    python3 tests/test_github_mcp.py --integration --rest   # REST API only
    python3 tests/test_github_mcp.py --integration --mcp    # MCP Docker only
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
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


def _parse_github_repo_url(url: str) -> tuple[str, str]:
    url = url.strip().rstrip("/")
    if url.endswith(".git"):
        url = url[:-4]
    if "?" in url:
        url = url.split("?")[0]
    parts = [p for p in url.split("/") if p and ":" not in p]
    if len(parts) >= 3:
        return parts[-2], parts[-1]
    return "", ""


_github_url = _env("TEST_GITHUB_REPO_URL")
if _github_url:
    GITHUB_OWNER, GITHUB_REPO = _parse_github_repo_url(_github_url)
else:
    GITHUB_OWNER = "your-username"
    GITHUB_REPO = "test-repo"

GITHUB_REPO_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}"
GITHUB_API_BASE = "https://api.github.com"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _github_auth_header() -> str | None:
    token = _env("TEST_GITHUB_TOKEN")
    if not token:
        return None
    return f"Bearer {token}"


def _github_token() -> str:
    return _env("TEST_GITHUB_TOKEN")


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
# GitHub MCP client — official Docker image (STDIO JSON-RPC 2.0)
# ---------------------------------------------------------------------------

class GitHubMCPClient:
    """Client for the official GitHub MCP server (ghcr.io/github/github-mcp-server).

    Communicates over STDIO JSON-RPC 2.0 by launching the server as a Docker
    subprocess. Implements the MCP initialize handshake automatically.

    Usage:
        with GitHubMCPClient(token) as client:
            tools = client.tools_list()
            resp  = client.call_tool("get_file_contents", {...})
    """

    IMAGE = "ghcr.io/github/github-mcp-server"
    MCP_PROTOCOL_VERSION = "2024-11-05"

    def __init__(self, token: str, timeout: int = 30):
        self.token = token
        self.timeout = timeout
        self._proc: subprocess.Popen | None = None
        self._req_id = 0
        self._reader_thread: threading.Thread | None = None
        self._response_queue: queue.Queue = queue.Queue()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *_):
        self.stop()

    def start(self) -> None:
        """Start the Docker container and perform the MCP initialize handshake."""
        env = {**os.environ, "GITHUB_PERSONAL_ACCESS_TOKEN": self.token}
        self._proc = subprocess.Popen(
            [
                "docker", "run", "-i", "--rm",
                "-e", "GITHUB_PERSONAL_ACCESS_TOKEN",
                self.IMAGE,
            ],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self._reader_thread = threading.Thread(target=self._drain_stdout, daemon=True)
        self._reader_thread.start()

        resp = self._rpc("initialize", {
            "protocolVersion": self.MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "constellation-test", "version": "1.0"},
        })
        if "error" in resp:
            raise RuntimeError(f"GitHub MCP init failed: {resp['error']}")
        self._notify("notifications/initialized", {})

    def _drain_stdout(self) -> None:
        assert self._proc is not None
        try:
            for raw in self._proc.stdout:
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self._response_queue.put(line)
        except Exception:
            pass
        finally:
            self._response_queue.put(None)

    def _rpc(self, method: str, params: dict, timeout: int | None = None) -> dict:
        self._req_id += 1
        req_id = self._req_id
        msg = json.dumps({
            "jsonrpc": "2.0", "id": req_id,
            "method": method, "params": params,
        }) + "\n"
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(msg.encode())
        self._proc.stdin.flush()

        deadline = time.time() + (timeout or self.timeout)
        while time.time() < deadline:
            try:
                line = self._response_queue.get(timeout=max(0.1, deadline - time.time()))
            except queue.Empty:
                break
            if line is None:
                break
            try:
                data = json.loads(line)
                if "id" in data and data["id"] == req_id:
                    return data
                self._response_queue.put(line)
            except json.JSONDecodeError:
                pass
        return {"error": {"code": -1, "message": f"timeout waiting for MCP response to {method!r}"}}

    def _notify(self, method: str, params: dict) -> None:
        assert self._proc and self._proc.stdin
        msg = json.dumps({"jsonrpc": "2.0", "method": method, "params": params}) + "\n"
        self._proc.stdin.write(msg.encode())
        self._proc.stdin.flush()

    def tools_list(self) -> list:
        resp = self._rpc("tools/list", {})
        return (resp.get("result") or {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict, timeout: int = 30) -> dict:
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
        if self._proc:
            try:
                self._proc.stdin.close()
                self._proc.wait(timeout=5)
            except Exception:
                self._proc.kill()
            self._proc = None


# ---------------------------------------------------------------------------
# GitHub REST API test functions (direct, no Docker)
# ---------------------------------------------------------------------------

def test_github_url_parseable(reporter: Reporter) -> None:
    assert GITHUB_OWNER in GITHUB_REPO_URL
    assert GITHUB_REPO in GITHUB_REPO_URL
    reporter.ok(f"GitHub repo URL is well-formed: {GITHUB_REPO_URL}")


def test_github_repo_metadata(reporter: Reporter) -> None:
    auth = _github_auth_header()
    headers = {"X-GitHub-Api-Version": "2022-11-28"}
    if auth:
        headers["Authorization"] = auth
    status, body = _http_get(f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}", headers=headers)
    if status == 200:
        reporter.ok(f"GitHub REST repo accessible: {body.get('full_name')} "
                    f"(default branch: {body.get('default_branch')})")
    elif status == 401:
        reporter.fail("GitHub auth rejected (401)", str(body)[:150])
    elif status == 403:
        reporter.fail("GitHub forbidden (403) — check token scopes", str(body)[:150])
    elif status == 404:
        reporter.fail(f"GitHub repo {GITHUB_OWNER}/{GITHUB_REPO} not found (404)")
    else:
        reporter.fail(f"GitHub repo metadata returned HTTP {status}", str(body)[:150])


def test_github_repo_branches(reporter: Reporter) -> None:
    auth = _github_auth_header()
    headers = {"X-GitHub-Api-Version": "2022-11-28"}
    if auth:
        headers["Authorization"] = auth
    status, body = _http_get(
        f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/branches", headers=headers
    )
    if status == 200 and isinstance(body, list):
        branch_names = [b.get("name", "") for b in body[:5]]
        reporter.ok(f"GitHub REST branches: {len(body)} total — {branch_names}")
    else:
        reporter.fail(f"GitHub REST branches returned HTTP {status}", str(body)[:150])


def test_github_repo_contents(reporter: Reporter) -> None:
    auth = _github_auth_header()
    headers = {"X-GitHub-Api-Version": "2022-11-28"}
    if auth:
        headers["Authorization"] = auth
    status, body = _http_get(
        f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/contents/", headers=headers
    )
    if status == 200 and isinstance(body, list):
        names = [item.get("name", "") for item in body[:8]]
        reporter.ok(f"GitHub REST root contents: {len(body)} items — {names}")
    else:
        reporter.fail(f"GitHub REST contents returned HTTP {status}", str(body)[:150])


def test_github_pull_requests(reporter: Reporter) -> None:
    auth = _github_auth_header()
    headers = {"X-GitHub-Api-Version": "2022-11-28"}
    if auth:
        headers["Authorization"] = auth
    status, body = _http_get(
        f"{GITHUB_API_BASE}/repos/{GITHUB_OWNER}/{GITHUB_REPO}/pulls?state=open&per_page=5",
        headers=headers,
    )
    if status == 200 and isinstance(body, list):
        reporter.ok(f"GitHub REST PRs: {len(body)} open")
    else:
        reporter.fail(f"GitHub REST PRs returned HTTP {status}", str(body)[:150])


# ---------------------------------------------------------------------------
# GitHub MCP (Docker) test functions
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
# Suite runners
# ---------------------------------------------------------------------------

def run_rest_tests(reporter: Reporter) -> None:
    reporter.section(f"GitHub REST API — {GITHUB_REPO_URL}")
    test_github_repo_metadata(reporter)
    test_github_repo_branches(reporter)
    test_github_repo_contents(reporter)
    test_github_pull_requests(reporter)


def run_mcp_tests(reporter: Reporter) -> None:
    reporter.section(f"GitHub MCP (Docker) — {GITHUB_REPO_URL}")

    token = _github_token()
    if not token:
        reporter.skip("GitHub MCP server tests", "TEST_GITHUB_TOKEN not set in tests/.env")
        return

    try:
        result = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        if result.returncode != 0:
            reporter.skip("GitHub MCP server tests", "Docker daemon not running")
            return
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        reporter.skip("GitHub MCP server tests", f"Docker unavailable: {exc}")
        return

    reporter.step("Starting GitHub MCP server container …")
    try:
        with GitHubMCPClient(token, timeout=30) as client:
            reporter.ok("GitHub MCP server container started and initialized")
            test_github_mcp_tools_list(reporter, client)
            test_github_mcp_get_repository(reporter, client)
            test_github_mcp_list_branches(reporter, client)
            test_github_mcp_get_file_contents(reporter, client)
            test_github_mcp_list_pull_requests(reporter, client)
            test_github_mcp_search_code(reporter, client)
    except RuntimeError as exc:
        reporter.fail("GitHub MCP server failed to start", str(exc))


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
    parser.add_argument("--rest", action="store_true", help="Run GitHub REST API tests only")
    parser.add_argument("--mcp", action="store_true", help="Run GitHub MCP Docker tests only")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    reporter = Reporter(verbose=args.verbose)

    print("\n" + "=" * 60)
    print("  GitHub Integration Tests  (REST API + MCP Docker)")
    print("=" * 60)
    print(f"  Repo : {GITHUB_REPO_URL}")
    print(f"  MCP  : ghcr.io/github/github-mcp-server (STDIO)")

    reporter.section("Static / dry-run checks")
    test_github_url_parseable(reporter)

    if not args.integration:
        print("\n\033[93mIntegration tests skipped — pass --integration to run live checks.\033[0m")
    else:
        any_selected = args.rest or args.mcp
        if not any_selected or args.rest:
            run_rest_tests(reporter)
        if not any_selected or args.mcp:
            run_mcp_tests(reporter)

    print(f"\nPassed: {reporter.passed}  Failed: {reporter.failed}  Skipped: {reporter.skipped}")
    return 0 if reporter.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
