#!/usr/bin/env python3
"""Figma REST API integration tests.

Tests the Figma REST API directly (no agent) using Personal Access Token auth.
Validates file metadata, page listing, page-by-name lookup, and node retrieval.

Required keys in tests/.env:
  TEST_FIGMA_FILE_URL   Full Figma design URL
                        (e.g. https://www.figma.com/design/abc123/My-Design)
  TEST_FIGMA_TOKEN      Figma Personal Access Token

Optional:
  FIGMA_TOKEN           Alternate env var name for Figma token (also checked)

Usage:
    python3 tests/test_figma_rest_api.py              # dry-run (no network)
    python3 tests/test_figma_rest_api.py --integration [-v]
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

PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))


def _env(key: str, fallback: str = "") -> str:
    return _ENV.get(key, fallback)


def _parse_figma_file_url(url: str) -> str:
    url = url.strip()
    for prefix in ("/design/", "/file/"):
        if prefix in url:
            after = url.split(prefix)[1]
            return after.split("/")[0].split("?")[0]
    return ""


_figma_file_url = _env("TEST_FIGMA_FILE_URL")
FIGMA_FILE_URL = _figma_file_url
FIGMA_FILE_KEY = _parse_figma_file_url(_figma_file_url) if _figma_file_url else ""
FIGMA_API_BASE = "https://api.figma.com/v1"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _figma_token() -> str:
    # ONLY from tests/.env — raise if missing
    token = _env("TEST_FIGMA_TOKEN")
    if not token:
        raise SystemExit("ERROR: TEST_FIGMA_TOKEN not set in tests/.env — cannot run tests")
    return token


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _figma_get(path: str, token: str, timeout: int = 20):
    url = f"{FIGMA_API_BASE}{path}"
    headers = {
        "X-Figma-Token": token,
        "Accept": "application/json",
    }
    req = Request(url, headers=headers, method="GET")
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

def test_figma_url_parseable(reporter: Reporter) -> None:
    assert FIGMA_FILE_KEY, \
        "FIGMA_FILE_KEY not configured — set TEST_FIGMA_FILE_URL in tests/.env"
    assert FIGMA_FILE_KEY in FIGMA_FILE_URL, "file key not found in Figma URL"
    reporter.ok(f"Figma file URL is well-formed — file key: {FIGMA_FILE_KEY}")


def test_figma_token_valid(reporter: Reporter, token: str) -> str | None:
    """Fetch file metadata to validate the Figma token. Returns file name on success."""
    reporter.step(f"Validate Figma token — fetch file {FIGMA_FILE_KEY}")
    status, body = _figma_get(f"/files/{FIGMA_FILE_KEY}?depth=1", token)
    reporter.show("File metadata", body)
    if status == 200 and body.get("name"):
        file_name = body["name"]
        reporter.ok(f"Figma token is valid — file: '{file_name}'")
        return file_name
    elif status == 429:
        reporter.skip("Figma token validation", "Figma API rate limit (429) — wait and retry")
    elif status == 403:
        reporter.fail("Figma token rejected (403) — check TEST_FIGMA_TOKEN in tests/.env")
    elif status == 404:
        reporter.fail("Figma file not found (404) — check TEST_FIGMA_FILE_URL in tests/.env")
    else:
        reporter.fail(f"Unexpected status {status}", str(body)[:200])
    return None


def test_figma_fetch_node(reporter: Reporter, token: str, node_id: str) -> None:
    reporter.step(f"Fetch node {node_id}")
    status, body = _figma_get(f"/files/{FIGMA_FILE_KEY}/nodes?ids={node_id}", token)
    reporter.show("Node fetch", body)
    nodes = body.get("nodes", {}) if isinstance(body, dict) else {}
    if status == 200 and node_id in nodes:
        reporter.ok(f"Node {node_id} fetched successfully")
    elif status == 429:
        reporter.skip(f"Node {node_id} fetch", "Figma API rate limit (429)")
    else:
        reporter.fail(f"Node {node_id} fetch failed (status {status})", str(body)[:200])


def test_figma_list_pages(reporter: Reporter, token: str) -> list:
    """List pages via direct REST API. Returns list of page dicts on success."""
    reporter.step("List pages via REST API")
    status, body = _figma_get(f"/files/{FIGMA_FILE_KEY}?depth=1", token)
    reporter.show("Pages (from file)", body)
    if status == 429:
        reporter.skip("List pages", "Figma API rate limit (429)")
        return []
    if status != 200:
        reporter.fail(f"List pages failed (status {status})", str(body)[:200])
        return []
    pages = [
        node
        for node in (body.get("document", {}).get("children", []))
        if isinstance(node, dict) and node.get("type") == "CANVAS"
    ]
    if pages:
        page_names = [p.get("name", "") for p in pages]
        reporter.ok(f"Pages listed: {len(pages)} — {page_names[:6]}")
    else:
        reporter.fail("No pages found in file document", str(body)[:200])
    return pages


def test_figma_client_module(reporter: Reporter, token: str) -> list:
    """List pages via the figma_client module (also validates the module is importable)."""
    reporter.step("List pages via figma_client module")
    _ui_design_dir = os.path.join(PROJECT_ROOT, "ui-design")
    if _ui_design_dir not in sys.path:
        sys.path.insert(0, _ui_design_dir)
    try:
        import figma_client
    except ImportError as exc:
        reporter.fail("figma_client module not importable", str(exc))
        return []

    os.environ.setdefault("FIGMA_TOKEN", token)
    pages, page_status = figma_client.fetch_pages(FIGMA_FILE_KEY)
    reporter.show("figma_client pages", pages)
    if page_status == "ok" and isinstance(pages, list) and pages:
        page_names = [p.get("name", "") for p in pages]
        reporter.ok(f"figma_client.fetch_pages: {len(pages)} pages — {page_names[:5]}")
        return pages
    elif page_status == "error_429":
        reporter.skip("figma_client.fetch_pages", "Figma API rate limit (429)")
    else:
        reporter.fail(f"figma_client.fetch_pages failed: {page_status}", str(pages)[:200])
    return []


# ---------------------------------------------------------------------------
# Suite runner
# ---------------------------------------------------------------------------

def run_figma_tests(reporter: Reporter) -> None:
    reporter.section(f"Figma REST API — file {FIGMA_FILE_KEY}")

    token = _figma_token()
    if not token:
        reporter.fail("Figma token not available — set TEST_FIGMA_TOKEN in tests/.env "
                      "or FIGMA_TOKEN in ui-design/.env")
        return

    file_name = test_figma_token_valid(reporter, token)
    if not file_name:
        return

    pages = test_figma_client_module(reporter, token)

    # Extract a node ID from the URL for node-fetch test
    node_id = None
    if _figma_file_url and "node-id=" in _figma_file_url:
        raw = _figma_file_url.split("node-id=")[1].split("&")[0]
        # URL-encoded colon becomes %3A or hyphen in URL
        node_id = raw.replace("-", ":").replace("%3A", ":").replace("%3a", ":")
    if node_id and node_id != ":":
        test_figma_fetch_node(reporter, token, node_id)

    reporter.step("List pages via REST API (direct)")
    test_figma_list_pages(reporter, token)


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
    print("  Figma REST API Integration Tests")
    print("=" * 60)
    print(f"  File URL : {FIGMA_FILE_URL}")
    print(f"  File key : {FIGMA_FILE_KEY}")

    reporter.section("Static / dry-run checks")
    if FIGMA_FILE_KEY:
        test_figma_url_parseable(reporter)
    else:
        reporter.skip("Figma URL check", "TEST_FIGMA_FILE_URL not set in tests/.env")

    reporter.step("figma_client module importable")
    _ui_design_dir = os.path.join(PROJECT_ROOT, "ui-design")
    if _ui_design_dir not in sys.path:
        sys.path.insert(0, _ui_design_dir)
    try:
        import figma_client as _fc  # noqa: F401
        reporter.ok("ui-design/figma_client imported successfully")
    except ImportError as exc:
        reporter.fail("figma_client module import failed", str(exc))

    if not args.integration:
        print("\n\033[93mIntegration tests skipped — pass --integration to run live checks.\033[0m")
    else:
        run_figma_tests(reporter)

    print(f"\nPassed: {reporter.passed}  Failed: {reporter.failed}  Skipped: {reporter.skipped}")
    return 0 if reporter.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
