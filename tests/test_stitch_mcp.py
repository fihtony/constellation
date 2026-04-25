#!/usr/bin/env python3
"""Google Stitch MCP integration tests.

Tests the Stitch MCP server at https://stitch.googleapis.com/mcp using the
JSON-RPC 2.0 protocol (HTTP POST with X-Goog-Api-Key authentication).

Required keys in tests/.env:
  TEST_STITCH_PROJECT_URL   Full Stitch project URL
                            (e.g. https://stitch.withgoogle.com/projects/12345678)
  TEST_STITCH_SCREEN_ID     32-character screen ID
  TEST_STITCH_API_KEY       Google / Gemini API key

Usage:
    python3 tests/test_stitch_mcp.py              # dry-run (no network)
    python3 tests/test_stitch_mcp.py --integration [-v]
"""

from __future__ import annotations

import argparse
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


def _parse_stitch_project_url(url: str) -> str:
    url = url.strip()
    if "/projects/" in url:
        after = url.split("/projects/")[1]
        return after.split("/")[0].split("?")[0]
    return ""


_stitch_project_url = _env("TEST_STITCH_PROJECT_URL")
STITCH_PROJECT_ID = (
    _parse_stitch_project_url(_stitch_project_url) if _stitch_project_url
    else "your-project-id"
)
STITCH_PROJECT_URL = _stitch_project_url or f"https://stitch.withgoogle.com/projects/{STITCH_PROJECT_ID}"
STITCH_SCREEN_ID = _env("TEST_STITCH_SCREEN_ID", "your-screen-id")
STITCH_MCP_URL = "https://stitch.googleapis.com/mcp"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _stitch_api_key() -> str:
    return _env("TEST_STITCH_API_KEY")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _stitch_post(method: str, params: dict, api_key: str, timeout: int = 30):
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Goog-Api-Key": api_key,
    }
    req = Request(STITCH_MCP_URL, data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body.strip() else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except Exception:
            return exc.code, {"error": body[:300]}
    except URLError as exc:
        return 0, {"error": str(exc)}


# ---------------------------------------------------------------------------
# Test functions
# ---------------------------------------------------------------------------

def test_stitch_ids_parseable(reporter: Reporter) -> None:
    assert STITCH_PROJECT_ID not in ("", "your-project-id"), \
        "STITCH_PROJECT_ID not configured — set TEST_STITCH_PROJECT_URL in tests/.env"
    assert len(STITCH_PROJECT_ID) >= 10, f"Stitch project ID too short: {STITCH_PROJECT_ID!r}"
    reporter.ok(f"Stitch project URL is well-formed: project {STITCH_PROJECT_ID}")

    if STITCH_SCREEN_ID not in ("", "your-screen-id"):
        assert len(STITCH_SCREEN_ID) == 32, \
            f"Expected 32-char screen ID, got {len(STITCH_SCREEN_ID)}: {STITCH_SCREEN_ID!r}"
        reporter.ok(f"Stitch screen ID is well-formed: {STITCH_SCREEN_ID}")
    else:
        reporter.info("TEST_STITCH_SCREEN_ID not set — screen tests will be skipped")


def test_stitch_mcp_tools_list(reporter: Reporter) -> None:
    api_key = _stitch_api_key()
    if not api_key:
        reporter.skip("Stitch MCP tools/list", "TEST_STITCH_API_KEY not set in tests/.env")
        return

    status, body = _stitch_post("tools/list", {}, api_key)
    if status == 200 and "result" in body:
        tools = (body["result"] or {}).get("tools", [])
        tool_names = [t.get("name", "") for t in tools[:10]]
        reporter.ok(f"Stitch MCP reachable — {len(tools)} tools: {tool_names}")
    elif status in (401, 403):
        reporter.fail(f"Stitch MCP auth rejected ({status}) — check TEST_STITCH_API_KEY", str(body)[:150])
    elif status == 0:
        reporter.fail("Stitch MCP unreachable", str(body)[:150])
    else:
        reporter.fail(f"Stitch MCP tools/list returned HTTP {status}", str(body)[:150])


def test_stitch_mcp_get_project(reporter: Reporter) -> None:
    api_key = _stitch_api_key()
    if not api_key:
        reporter.skip("Stitch MCP get_project", "TEST_STITCH_API_KEY not set in tests/.env")
        return

    status, body = _stitch_post(
        "tools/call",
        {"name": "get_project", "arguments": {"name": f"projects/{STITCH_PROJECT_ID}"}},
        api_key,
    )
    if status == 200 and "result" in body:
        result = body["result"]
        if isinstance(result, dict) and result.get("isError"):
            reporter.fail("Stitch get_project returned isError=true", str(result)[:200])
        else:
            reporter.ok(f"Stitch get_project succeeded for project {STITCH_PROJECT_ID}")
    elif status == 200 and "error" in body:
        err = body["error"]
        reporter.fail(f"Stitch get_project error: {err.get('message', str(err))[:150]}")
    elif status in (401, 403):
        # Stitch MCP requires OAuth2 for tool calls; API key is sufficient only for tools/list
        reporter.skip(
            "Stitch get_project",
            f"HTTP {status} — tool calls require OAuth2; API key is only valid for tools/list",
        )
    elif status == 0:
        reporter.fail("Stitch MCP unreachable", str(body)[:150])
    else:
        reporter.fail(f"Stitch get_project returned HTTP {status}", str(body)[:150])


def test_stitch_mcp_get_screen(reporter: Reporter) -> None:
    api_key = _stitch_api_key()
    if not api_key:
        reporter.skip("Stitch MCP get_screen", "TEST_STITCH_API_KEY not set in tests/.env")
        return
    if STITCH_SCREEN_ID in ("", "your-screen-id"):
        reporter.skip("Stitch MCP get_screen", "TEST_STITCH_SCREEN_ID not set in tests/.env")
        return

    status, body = _stitch_post(
        "tools/call",
        {
            "name": "get_screen",
            "arguments": {
                "project_id": STITCH_PROJECT_ID,
                "screen_id": STITCH_SCREEN_ID,
            },
        },
        api_key,
    )
    if status == 200 and "result" in body:
        result = body["result"]
        if isinstance(result, dict) and result.get("isError"):
            reporter.fail("Stitch get_screen returned isError=true", str(result)[:200])
        else:
            content = result.get("content", []) if isinstance(result, dict) else []
            reporter.ok(
                f"Stitch get_screen succeeded for screen {STITCH_SCREEN_ID} "
                f"({len(content)} content items)"
            )
    elif status == 200 and "error" in body:
        err = body["error"]
        reporter.fail(f"Stitch get_screen error: {err.get('message', str(err))[:150]}")
    elif status in (401, 403):
        reporter.skip(
            "Stitch get_screen",
            f"HTTP {status} — tool calls require OAuth2; API key is only valid for tools/list",
        )
    elif status == 0:
        reporter.fail("Stitch MCP unreachable", str(body)[:150])
    else:
        reporter.fail(f"Stitch get_screen returned HTTP {status}", str(body)[:150])


def test_stitch_mcp_get_screen_image(reporter: Reporter) -> None:
    api_key = _stitch_api_key()
    if not api_key:
        reporter.skip("Stitch MCP get_screen_image", "TEST_STITCH_API_KEY not set in tests/.env")
        return
    if STITCH_SCREEN_ID in ("", "your-screen-id"):
        reporter.skip("Stitch MCP get_screen_image", "TEST_STITCH_SCREEN_ID not set in tests/.env")
        return

    status, body = _stitch_post(
        "tools/call",
        {
            "name": "get_screen_image",
            "arguments": {
                "project_id": STITCH_PROJECT_ID,
                "screen_id": STITCH_SCREEN_ID,
            },
        },
        api_key,
    )
    if status == 200 and "result" in body:
        result = body["result"]
        if isinstance(result, dict) and result.get("isError"):
            reporter.fail("Stitch get_screen_image returned isError=true", str(result)[:200])
            return
        content = result.get("content", []) if isinstance(result, dict) else []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "image":
                img_url = item.get("url", "")
                if img_url:
                    try:
                        req = Request(img_url, method="GET")
                        with urlopen(req, timeout=20) as resp:
                            img_status = resp.status
                        if img_status == 200:
                            reporter.ok(f"Stitch screen image downloadable: {img_url[:80]}")
                        else:
                            reporter.fail(f"Stitch screen image download failed (HTTP {img_status})")
                    except Exception as exc:
                        reporter.fail(f"Stitch screen image fetch error", str(exc)[:150])
                    return
        reporter.ok("Stitch get_screen_image call succeeded (no separate image URL in response)")
    elif status == 200 and "error" in body:
        err = body["error"]
        if "not found" in str(err).lower() or "unknown" in str(err).lower():
            reporter.skip("Stitch get_screen_image", "tool not available in this MCP version")
        else:
            reporter.fail(f"Stitch get_screen_image error: {err.get('message', str(err))[:150]}")
    elif status in (401, 403):
        reporter.skip(
            "Stitch get_screen_image",
            f"HTTP {status} — tool calls require OAuth2; API key is only valid for tools/list",
        )
    elif status == 0:
        reporter.fail("Stitch MCP unreachable", str(body)[:150])
    else:
        reporter.fail(f"Stitch get_screen_image returned HTTP {status}", str(body)[:150])


# ---------------------------------------------------------------------------
# Suite runner
# ---------------------------------------------------------------------------

def run_stitch_tests(reporter: Reporter) -> None:
    reporter.section(f"Google Stitch MCP — project {STITCH_PROJECT_ID}")
    test_stitch_mcp_tools_list(reporter)
    test_stitch_mcp_get_project(reporter)
    test_stitch_mcp_get_screen(reporter)
    test_stitch_mcp_get_screen_image(reporter)


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
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    reporter = Reporter(verbose=args.verbose)

    print("\n" + "=" * 60)
    print("  Google Stitch MCP Integration Tests")
    print("=" * 60)
    print(f"  Project : {STITCH_PROJECT_URL}")
    print(f"  Screen  : {STITCH_SCREEN_ID}")
    print(f"  MCP URL : {STITCH_MCP_URL}")

    reporter.section("Static / dry-run checks")
    if STITCH_PROJECT_ID not in ("", "your-project-id"):
        test_stitch_ids_parseable(reporter)
    else:
        reporter.skip("Stitch ID checks", "TEST_STITCH_PROJECT_URL not set in tests/.env")

    if not args.integration:
        print("\n\033[93mIntegration tests skipped — pass --integration to run live checks.\033[0m")
    else:
        run_stitch_tests(reporter)

    print(f"\nPassed: {reporter.passed}  Failed: {reporter.failed}  Skipped: {reporter.skipped}")
    return 0 if reporter.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
