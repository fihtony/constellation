#!/usr/bin/env python3
"""UI Design Agent integration test.

Covers:
  - Figma REST API: file metadata, pages, page-by-name, node fetch
  - Google Stitch MCP: tools list, project metadata, screen design, screen image
  - Agent HTTP endpoints for both Figma and Stitch
  - A2A message interface (POST /message:send)

Test targets:
  Figma  : https://www.figma.com/design/gxd2LNayM2hh3V3qTlcyPF/Website-Wireframes-UI-Kit--Community-
  Stitch : project 13629074018280446337 (Open English Study Hub)
           screen  4cb76ffb69624ddeb01b16075909d929 (Lesson Library)

Environment variables:
  FIGMA_TOKEN     Personal access token for Figma REST API (or in ui-design/.env)
  STITCH_API_KEY  Google Stitch / Gemini API key (or in tests/.env)

Usage:
    # Dry-run (no network):
    python3 tests/test_ui_design_agent.py

    # Full integration (requires running agent + credentials):
    python3 tests/test_ui_design_agent.py --integration [-v]

    # Specific sub-suite:
    python3 tests/test_ui_design_agent.py --integration --figma
    python3 tests/test_ui_design_agent.py --integration --stitch

    # Against a custom agent URL:
    python3 tests/test_ui_design_agent.py --integration --agent-url http://127.0.0.1:8040
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from urllib.parse import urlencode

# ---------------------------------------------------------------------------
# Add project root to path so agent modules import cleanly
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests.agent_test_support import (
    Reporter,
    agent_url_from_args,
    http_request,
    load_env_file,
    summary_exit_code,
)

# ---------------------------------------------------------------------------
# Test targets
# ---------------------------------------------------------------------------

FIGMA_URL = (
    "https://www.figma.com/design/gxd2LNayM2hh3V3qTlcyPF/"
    "Website-Wireframes-UI-Kit--Community-"
    "?node-id=1-470&p=f&t=m1Ws9RDF0GoDcA35-0"
)
FIGMA_FILE_KEY = "gxd2LNayM2hh3V3qTlcyPF"
FIGMA_NODE_ID = "1:470"

STITCH_PROJECT_ID = "13629074018280446337"
STITCH_SCREEN_ID = "4cb76ffb69624ddeb01b16075909d929"
STITCH_SCREEN_NAME = "Lesson Library"
STITCH_MCP_URL = "https://stitch.googleapis.com/mcp"

LOCAL_AGENT_URL = "http://127.0.0.1:8040"
CONTAINER_AGENT_URL = "http://127.0.0.1:8040"


# ---------------------------------------------------------------------------
# Credential loaders
# ---------------------------------------------------------------------------

def _load_figma_token() -> str:
    """Try env var first, then ui-design/.env."""
    token = os.environ.get("FIGMA_TOKEN", "").strip()
    if not token:
        env = load_env_file("ui-design/.env")
        token = env.get("FIGMA_TOKEN", "").strip()
    return token


def _load_stitch_key() -> str:
    """Try env var first, then tests/.env, then ui-design/.env."""
    key = os.environ.get("STITCH_API_KEY", "").strip()
    if not key:
        env = load_env_file("tests/.env")
        key = env.get("STITCH_API_KEY", "").strip()
    if not key:
        env = load_env_file("ui-design/.env")
        key = env.get("STITCH_API_KEY", "").strip()
    return key


# ---------------------------------------------------------------------------
# Direct Stitch MCP helper (bypass agent, call MCP directly)
# ---------------------------------------------------------------------------

def _stitch_post(method: str, params: dict, api_key: str, timeout: int = 30):
    import json as _json
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    data = _json.dumps(payload).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Goog-Api-Key": api_key,
    }
    req = Request(STITCH_MCP_URL, data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, _json.loads(body) if body.strip() else {}
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, _json.loads(body)
        except Exception:
            return exc.code, {"error": body[:300]}
    except URLError as exc:
        return 0, {"error": str(exc)}


# ---------------------------------------------------------------------------
# Static / dry-run checks
# ---------------------------------------------------------------------------

def run_static_checks(reporter: Reporter) -> None:
    reporter.section("Static / dry-run checks")

    reporter.step("Figma URL is well-formed")
    assert FIGMA_FILE_KEY in FIGMA_URL, "file key not in Figma URL"
    reporter.ok(f"Figma URL contains file key {FIGMA_FILE_KEY}")

    reporter.step("Stitch IDs are well-formed")
    assert len(STITCH_SCREEN_ID) == 32, f"Expected 32-char screen ID, got {len(STITCH_SCREEN_ID)}"
    assert len(STITCH_PROJECT_ID) >= 15, "Stitch project ID too short"
    reporter.ok(f"Stitch project ID: {STITCH_PROJECT_ID}")
    reporter.ok(f"Stitch screen ID ({STITCH_SCREEN_NAME}): {STITCH_SCREEN_ID}")

    reporter.step("ui-design modules importable")
    try:
        _ui_design_dir = os.path.join(PROJECT_ROOT, "ui-design")
        if _ui_design_dir not in sys.path:
            sys.path.insert(0, _ui_design_dir)
        import figma_client as _fc  # noqa: F401
        import stitch_client as _sc  # noqa: F401
        reporter.ok("ui-design/figma_client and ui-design/stitch_client imported successfully")
    except ImportError as exc:
        reporter.fail("Module import failed", str(exc))


# ---------------------------------------------------------------------------
# Figma REST API direct tests (no agent)
# ---------------------------------------------------------------------------

def run_figma_direct_tests(reporter: Reporter) -> None:
    reporter.section("Figma REST API — direct (no agent)")

    token = _load_figma_token()
    if not token:
        reporter.fail("FIGMA_TOKEN not available in env or ui-design/.env")
        return

    reporter.step(f"Validate token — fetch file {FIGMA_FILE_KEY}")
    status, body, _ = http_request(
        f"https://api.figma.com/v1/files/{FIGMA_FILE_KEY}?depth=1",
        headers={"X-Figma-Token": token, "Accept": "application/json"},
    )
    reporter.show("Direct file fetch", body)
    if status == 200 and body.get("name"):
        file_name = body["name"]
        reporter.ok(f"Figma token is valid — file: '{file_name}'")
    elif status == 403:
        reporter.fail("Figma token rejected (403) — check FIGMA_TOKEN")
        return
    elif status == 404:
        reporter.fail("Figma file not found (404) — check FIGMA_FILE_KEY")
        return
    else:
        reporter.fail(f"Unexpected status {status}", str(body)[:200])
        return

    reporter.step(f"Fetch node {FIGMA_NODE_ID}")
    node_id_url = FIGMA_NODE_ID.replace(":", "-")
    status, body, _ = http_request(
        f"https://api.figma.com/v1/files/{FIGMA_FILE_KEY}/nodes?ids={FIGMA_NODE_ID}",
        headers={"X-Figma-Token": token, "Accept": "application/json"},
    )
    reporter.show("Direct node fetch", body)
    nodes = body.get("nodes", {}) if isinstance(body, dict) else {}
    if status == 200 and FIGMA_NODE_ID in nodes:
        reporter.ok(f"Node {FIGMA_NODE_ID} fetched successfully")
    else:
        reporter.fail(f"Node {FIGMA_NODE_ID} fetch failed (status {status})", str(body)[:200])

    reporter.step("List pages via figma_client module")
    os.environ.setdefault("FIGMA_TOKEN", token)
    _ui_design_dir = os.path.join(PROJECT_ROOT, "ui-design")
    if _ui_design_dir not in sys.path:
        sys.path.insert(0, _ui_design_dir)
    import figma_client
    pages, page_status = figma_client.fetch_pages(FIGMA_FILE_KEY)
    reporter.show("Pages", pages)
    if page_status == "ok" and isinstance(pages, list) and pages:
        page_names = [p.get("name", "") for p in pages]
        reporter.ok(f"figma_client.fetch_pages: {len(pages)} pages — {page_names[:5]}")
    else:
        reporter.fail(f"figma_client.fetch_pages failed: {page_status}", str(pages)[:200])


# ---------------------------------------------------------------------------
# Stitch MCP direct tests (no agent)
# ---------------------------------------------------------------------------

def run_stitch_direct_tests(reporter: Reporter) -> None:
    reporter.section("Google Stitch MCP — direct (no agent)")

    api_key = _load_stitch_key()
    if not api_key:
        reporter.fail("STITCH_API_KEY not available in env, tests/.env, or ui-design/.env")
        return

    reporter.step("List Stitch MCP tools")
    status, body = _stitch_post("tools/list", {}, api_key)
    reporter.show("tools/list", body)
    if status == 200 and "result" in body:
        tools = (body["result"] or {}).get("tools", [])
        tool_names = [t.get("name", "") for t in tools]
        reporter.ok(f"Stitch MCP reachable — {len(tools)} tools: {tool_names[:6]}")
    elif status in (401, 403):
        reporter.fail(f"Stitch auth rejected ({status})", str(body)[:200])
        return
    else:
        reporter.fail(f"Stitch tools/list returned HTTP {status}", str(body)[:200])
        return

    reporter.step(f"Fetch project {STITCH_PROJECT_ID}")
    status, body = _stitch_post(
        "tools/call",
        {"name": "get_project", "arguments": {"name": f"projects/{STITCH_PROJECT_ID}"}},
        api_key,
    )
    reporter.show("get_project", body)
    if status == 200 and "result" in body:
        result = body["result"]
        if isinstance(result, dict) and result.get("isError"):
            reporter.fail("get_project returned isError=true", str(result)[:200])
        else:
            reporter.ok(f"Stitch get_project succeeded for project {STITCH_PROJECT_ID}")
    else:
        reporter.fail(f"get_project returned HTTP {status}", str(body)[:200])

    reporter.step(f"Fetch screen '{STITCH_SCREEN_NAME}' ({STITCH_SCREEN_ID})")
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
    reporter.show("get_screen", body)
    if status == 200 and "result" in body:
        result = body["result"]
        if isinstance(result, dict) and result.get("isError"):
            reporter.fail("get_screen returned isError=true", str(result)[:200])
        else:
            content = result.get("content", []) if isinstance(result, dict) else []
            reporter.ok(
                f"Stitch get_screen succeeded: '{STITCH_SCREEN_NAME}' "
                f"({len(content)} content items)"
            )
    else:
        reporter.fail(f"get_screen returned HTTP {status}", str(body)[:200])

    reporter.step(f"Fetch screen image for '{STITCH_SCREEN_NAME}'")
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
    reporter.show("get_screen_image", body)
    if status == 200 and "result" in body:
        result = body["result"]
        if isinstance(result, dict) and result.get("isError"):
            reporter.fail("get_screen_image returned isError=true", str(result)[:200])
            return
        content = result.get("content", []) if isinstance(result, dict) else []
        # Look for image URLs and try downloading one
        image_urls = [
            item.get("url", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "image" and item.get("url")
        ]
        if image_urls:
            img_status, _, _ = http_request(image_urls[0], timeout=20)
            if img_status == 200:
                reporter.ok(f"Screen image downloadable: {image_urls[0][:80]}")
            else:
                reporter.fail(
                    f"Screen image download failed (HTTP {img_status})",
                    image_urls[0][:80],
                )
        else:
            reporter.ok("get_screen_image succeeded (image embedded in response, no separate URL)")
    elif status == 200 and "error" in body:
        err = body["error"]
        msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
        if "not found" in msg.lower() or "unknown" in msg.lower():
            reporter.info("get_screen_image: tool not available in this Stitch MCP version")
        else:
            reporter.fail(f"get_screen_image error: {msg[:150]}")
    else:
        reporter.fail(f"get_screen_image returned HTTP {status}", str(body)[:200])

    reporter.step("stitch_client module: list_tools()")
    _ui_design_dir = os.path.join(PROJECT_ROOT, "ui-design")
    if _ui_design_dir not in sys.path:
        sys.path.insert(0, _ui_design_dir)
    import stitch_client
    os.environ.setdefault("STITCH_API_KEY", api_key)
    tools_list, tools_status = stitch_client.list_tools()
    if tools_status == "ok" and isinstance(tools_list, list):
        reporter.ok(f"stitch_client.list_tools: {len(tools_list)} tools")
    else:
        reporter.fail(f"stitch_client.list_tools failed: {tools_status}")

    reporter.step("stitch_client module: get_project()")
    proj_result, proj_status = stitch_client.get_project(STITCH_PROJECT_ID)
    if proj_status == "ok":
        reporter.ok(f"stitch_client.get_project: OK")
    else:
        reporter.fail(f"stitch_client.get_project failed: {proj_status}", str(proj_result)[:200])

    reporter.step("stitch_client module: get_screen()")
    scr_result, scr_status = stitch_client.get_screen(STITCH_PROJECT_ID, STITCH_SCREEN_ID)
    if scr_status == "ok":
        reporter.ok(f"stitch_client.get_screen: OK (imageUrls: {scr_result.get('imageUrls', [])})")
    else:
        reporter.fail(f"stitch_client.get_screen failed: {scr_status}", str(scr_result)[:200])


# ---------------------------------------------------------------------------
# Agent endpoint tests (requires running agent)
# ---------------------------------------------------------------------------

def run_agent_tests(reporter: Reporter, agent_url: str) -> None:
    reporter.section(f"UI Design Agent endpoints — {agent_url}")

    reporter.step("Health check")
    status, body, _ = http_request(f"{agent_url}/health")
    reporter.show("Health", body)
    if status == 200 and body.get("status") == "ok":
        reporter.ok(f"Agent healthy: {body.get('service')}")
    else:
        reporter.fail("Agent health check failed", f"status={status}, body={body}")
        reporter.info("Start the agent with: cd constellation && PYTHONPATH=. FIGMA_TOKEN=... STITCH_API_KEY=... python3 ui-design/app.py")
        return

    reporter.step("Agent card")
    status, body, _ = http_request(f"{agent_url}/.well-known/agent-card.json")
    reporter.show("Agent card", body)
    if status == 200 and body.get("name") == "UI Design Agent":
        skill_ids = [s.get("id") for s in body.get("skills", [])]
        reporter.ok(f"Agent card valid — skills: {skill_ids}")
    else:
        reporter.fail("Agent card invalid", f"status={status}, body={body}")

    # --- Figma endpoints ---

    token = _load_figma_token()

    reporter.step(f"GET /figma/meta — file {FIGMA_FILE_KEY}")
    query = urlencode({"url": FIGMA_URL})
    status, body, _ = http_request(f"{agent_url}/figma/meta?{query}")
    reporter.show("Figma meta", body)
    meta = body.get("meta", {}) if isinstance(body, dict) else {}
    body_status = body.get("status", "") if isinstance(body, dict) else ""
    if status == 200 and body_status == "ok" and meta.get("name"):
        reporter.ok(f"Agent /figma/meta: '{meta.get('name')}'")
    elif body_status == "error_429":
        reporter.info("/figma/meta: Figma API rate-limited (429) — transient, not a code bug")
    elif not token:
        reporter.info("/figma/meta skipped — FIGMA_TOKEN not set")
    else:
        reporter.fail(f"Agent /figma/meta failed (status {status})", str(body)[:200])

    reporter.step(f"GET /figma/pages — file {FIGMA_FILE_KEY}")
    status, body, _ = http_request(f"{agent_url}/figma/pages?{query}")
    reporter.show("Figma pages", body)
    pages = body.get("pages", []) if isinstance(body, dict) else []
    body_status = body.get("status", "") if isinstance(body, dict) else ""
    if status == 200 and body_status == "ok" and pages:
        page_names = [p.get("name") for p in pages]
        reporter.ok(f"Agent /figma/pages: {len(pages)} pages — {page_names[:4]}")
    elif body_status == "error_429":
        reporter.info("/figma/pages: Figma API rate-limited (429) — transient, not a code bug")
    elif not token:
        reporter.info("/figma/pages skipped — FIGMA_TOKEN not set")
    else:
        reporter.fail(f"Agent /figma/pages failed (status {status})", str(body)[:200])

    # Try to fetch first page by name if pages were listed
    if pages:
        first_page_name = pages[0].get("name", "")
        reporter.step(f"GET /figma/page — page '{first_page_name}'")
        query_page = urlencode({"url": FIGMA_URL, "name": first_page_name})
        status, body, _ = http_request(f"{agent_url}/figma/page?{query_page}")
        reporter.show("Figma page", body)
        page = body.get("page", {}) if isinstance(body, dict) else {}
        body_status = body.get("status", "") if isinstance(body, dict) else ""
        if status == 200 and body_status == "ok":
            reporter.ok(f"Agent /figma/page: '{page.get('name')}'")
        elif body_status == "error_429":
            reporter.info("/figma/page: Figma API rate-limited (429) — transient, not a code bug")
        else:
            reporter.fail(f"Agent /figma/page failed (status {status})", str(body)[:200])

    # --- Stitch endpoints ---

    api_key = _load_stitch_key()

    reporter.step("GET /stitch/tools")
    status, body, _ = http_request(f"{agent_url}/stitch/tools")
    reporter.show("Stitch tools", body)
    tools = body.get("tools", []) if isinstance(body, dict) else []
    if status == 200 and body.get("status") == "ok" and isinstance(tools, list):
        tool_names = [t.get("name") for t in tools]
        reporter.ok(f"Agent /stitch/tools: {len(tools)} tools — {tool_names[:5]}")
    elif not api_key:
        reporter.info("/stitch/tools skipped — STITCH_API_KEY not set")
    else:
        reporter.fail(f"Agent /stitch/tools failed (status {status})", str(body)[:200])

    reporter.step(f"GET /stitch/project — id={STITCH_PROJECT_ID}")
    query_proj = urlencode({"id": STITCH_PROJECT_ID})
    status, body, _ = http_request(f"{agent_url}/stitch/project?{query_proj}")
    reporter.show("Stitch project", body)
    if status == 200 and body.get("status") == "ok":
        reporter.ok(f"Agent /stitch/project: OK")
    elif not api_key:
        reporter.info("/stitch/project skipped — STITCH_API_KEY not set")
    else:
        reporter.fail(f"Agent /stitch/project failed (status {status})", str(body)[:200])

    reporter.step(f"GET /stitch/screen — screen '{STITCH_SCREEN_NAME}'")
    query_scr = urlencode({"project_id": STITCH_PROJECT_ID, "screen_id": STITCH_SCREEN_ID})
    status, body, _ = http_request(f"{agent_url}/stitch/screen?{query_scr}")
    reporter.show("Stitch screen", body)
    if status == 200 and body.get("status") == "ok":
        image_urls = body.get("imageUrls", [])
        reporter.ok(
            f"Agent /stitch/screen: OK — imageUrls: {image_urls[:2]}"
        )
    elif not api_key:
        reporter.info("/stitch/screen skipped — STITCH_API_KEY not set")
    else:
        reporter.fail(f"Agent /stitch/screen failed (status {status})", str(body)[:200])

    reporter.step(f"GET /stitch/screen/image — screen '{STITCH_SCREEN_NAME}'")
    status, body, _ = http_request(f"{agent_url}/stitch/screen/image?{query_scr}")
    reporter.show("Stitch screen/image", body)
    if status == 200:
        image_urls = body.get("imageUrls", [])
        reporter.ok(
            f"Agent /stitch/screen/image: status={body.get('status')}, "
            f"imageUrls={image_urls[:2]}"
        )
    elif not api_key:
        reporter.info("/stitch/screen/image skipped — STITCH_API_KEY not set")
    else:
        reporter.fail(f"Agent /stitch/screen/image failed (status {status})", str(body)[:200])

    # --- A2A message interface ---

    reporter.step("POST /message:send — Figma request")
    status, body, _ = http_request(
        f"{agent_url}/message:send",
        method="POST",
        payload={
            "message": {
                "messageId": "ui-design-test-figma",
                "role": "ROLE_USER",
                "parts": [{"text": f"Fetch Figma file metadata for {FIGMA_URL}"}],
                "metadata": {"requestedCapability": "figma.file.meta"},
            }
        },
    )
    reporter.show("Message send (Figma)", body)
    task = body.get("task", {}) if isinstance(body, dict) else {}
    if status == 200 and task.get("status", {}).get("state") in (
        "TASK_STATE_WORKING", "TASK_STATE_COMPLETED"
    ):
        task_id = task.get("id", "")
        reporter.ok(f"Figma A2A task submitted: {task_id}")
        # Wait for completion
        time.sleep(2)
        if task_id:
            status2, body2, _ = http_request(f"{agent_url}/tasks/{task_id}")
            task2 = body2.get("task", {}) if isinstance(body2, dict) else {}
            state2 = task2.get("status", {}).get("state", "")
            if state2 == "TASK_STATE_COMPLETED":
                reporter.ok(f"Figma A2A task completed: {task_id}")
            else:
                reporter.info(f"Figma A2A task state: {state2}")
    else:
        reporter.fail(f"Figma A2A /message:send failed (status {status})", str(body)[:200])

    reporter.step("POST /message:send — Stitch request")
    status, body, _ = http_request(
        f"{agent_url}/message:send",
        method="POST",
        payload={
            "message": {
                "messageId": "ui-design-test-stitch",
                "role": "ROLE_USER",
                "parts": [{
                    "text": (
                        f"Fetch Stitch screen design for project {STITCH_PROJECT_ID} "
                        f"screen {STITCH_SCREEN_ID}"
                    )
                }],
                "metadata": {"requestedCapability": "stitch.screen.fetch"},
            }
        },
    )
    reporter.show("Message send (Stitch)", body)
    task = body.get("task", {}) if isinstance(body, dict) else {}
    if status == 200 and task.get("status", {}).get("state") in (
        "TASK_STATE_WORKING", "TASK_STATE_COMPLETED"
    ):
        task_id = task.get("id", "")
        reporter.ok(f"Stitch A2A task submitted: {task_id}")
        time.sleep(3)
        if task_id:
            status2, body2, _ = http_request(f"{agent_url}/tasks/{task_id}")
            task2 = body2.get("task", {}) if isinstance(body2, dict) else {}
            state2 = task2.get("status", {}).get("state", "")
            if state2 == "TASK_STATE_COMPLETED":
                reporter.ok(f"Stitch A2A task completed: {task_id}")
            else:
                reporter.info(f"Stitch A2A task state: {state2}")
    else:
        reporter.fail(f"Stitch A2A /message:send failed (status {status})", str(body)[:200])


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--integration", action="store_true",
                        help="Run live integration tests (requires credentials)")
    parser.add_argument("--figma", action="store_true",
                        help="Run Figma sub-suite only")
    parser.add_argument("--stitch", action="store_true",
                        help="Run Stitch sub-suite only")
    parser.add_argument("--agent", action="store_true",
                        help="Run agent endpoint tests (requires running agent)")
    parser.add_argument("--agent-url", default="",
                        help="Agent base URL (default: http://127.0.0.1:8040)")
    parser.add_argument("--container", action="store_true",
                        help="Use container agent URL")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    args = parse_args(argv)
    reporter = Reporter(verbose=args.verbose)

    print(f"\n{'=' * 60}")
    print("  UI Design Agent Integration Tests")
    print(f"{'=' * 60}")
    print(f"  Figma file : {FIGMA_FILE_KEY}")
    print(f"  Stitch     : project {STITCH_PROJECT_ID} / screen {STITCH_SCREEN_ID}")

    run_static_checks(reporter)

    if not args.integration:
        print(
            "\n\033[93mIntegration tests skipped — "
            "pass --integration to run live checks.\033[0m"
        )
        print(f"\nPassed: {reporter.passed}  Failed: {reporter.failed}")
        return 0 if reporter.failed == 0 else 1

    # Load credentials into env for module use
    figma_token = _load_figma_token()
    stitch_key = _load_stitch_key()
    if figma_token:
        os.environ["FIGMA_TOKEN"] = figma_token
    if stitch_key:
        os.environ["STITCH_API_KEY"] = stitch_key

    any_selected = args.figma or args.stitch or args.agent
    run_figma = not any_selected or args.figma
    run_stitch = not any_selected or args.stitch
    run_agent_flag = args.agent or not any_selected

    if run_figma:
        run_figma_direct_tests(reporter)

    if run_stitch:
        run_stitch_direct_tests(reporter)

    if run_agent_flag:
        agent_url = agent_url_from_args(
            args,
            local_default=LOCAL_AGENT_URL,
            container_default=CONTAINER_AGENT_URL,
        )
        run_agent_tests(reporter, agent_url)

    print(f"\n{'=' * 60}")
    print(f"  Passed: {reporter.passed}   Failed: {reporter.failed}")
    print(f"{'=' * 60}")
    return 0 if reporter.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
