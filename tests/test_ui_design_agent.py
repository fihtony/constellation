#!/usr/bin/env python3
"""UI Design Agent HTTP endpoint integration tests.

Verifies the agent's HTTP API endpoints for Figma and Stitch:
  - GET /health
  - GET /.well-known/agent-card.json
  - GET /figma/meta, /figma/pages, /figma/page
  - GET /stitch/tools, /stitch/project, /stitch/screen, /stitch/screen/image
  - POST /message:send  (A2A interface for Figma and Stitch tasks)
  - GET /tasks/{id}     (task state polling)

All configuration is loaded from tests/.env.  For direct (non-agent) API
tests see test_figma_rest_api.py and test_stitch_mcp.py.

Required keys in tests/.env:
  TEST_FIGMA_FILE_URL       Full Figma design URL
  TEST_FIGMA_TOKEN          Figma Personal Access Token
  TEST_STITCH_PROJECT_URL   Full Stitch project URL
  TEST_STITCH_SCREEN_ID     32-character Stitch screen ID
  TEST_STITCH_API_KEY       Google / Gemini API key

Usage:
    # Dry-run (no network, no agent):
    python3 tests/test_ui_design_agent.py

    # Full integration (requires running ui-design agent + credentials):
    python3 tests/test_ui_design_agent.py --integration [-v]

    # Against a running container:
    python3 tests/test_ui_design_agent.py --integration --container

    # Against a custom agent URL:
    python3 tests/test_ui_design_agent.py --integration --agent-url http://127.0.0.1:8040
"""

from __future__ import annotations

import argparse
import json
import os
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
# Configuration — loaded from tests/.env
# ---------------------------------------------------------------------------

_ENV = load_env_file("tests/.env")


def _env(key: str, fallback: str = "") -> str:
    return os.environ.get(key) or _ENV.get(key, fallback)


def _parse_figma_file_url(url: str) -> str:
    url = url.strip()
    for prefix in ("/design/", "/file/"):
        if prefix in url:
            return url.split(prefix)[1].split("/")[0].split("?")[0]
    return ""


def _parse_stitch_project_url(url: str) -> str:
    url = url.strip()
    if "/projects/" in url:
        return url.split("/projects/")[1].split("/")[0].split("?")[0]
    return ""


_figma_file_url = _env("TEST_FIGMA_FILE_URL")
FIGMA_URL = _figma_file_url or "https://www.figma.com/design/your-file-key/Test-File"
FIGMA_FILE_KEY = _parse_figma_file_url(_figma_file_url) if _figma_file_url else "your-file-key"

_stitch_project_url = _env("TEST_STITCH_PROJECT_URL")
STITCH_PROJECT_ID = (
    _parse_stitch_project_url(_stitch_project_url) if _stitch_project_url
    else "your-project-id"
)
STITCH_SCREEN_ID = _env("TEST_STITCH_SCREEN_ID", "your-screen-id")

LOCAL_AGENT_URL = "http://127.0.0.1:8040"
CONTAINER_AGENT_URL = "http://127.0.0.1:8040"


# ---------------------------------------------------------------------------
# Credential loaders
# ---------------------------------------------------------------------------

def _load_figma_token() -> str:
    # Priority: TEST_FIGMA_TOKEN in tests/.env → FIGMA_TOKEN env var → ui-design/.env
    token = _env("TEST_FIGMA_TOKEN") or os.environ.get("FIGMA_TOKEN", "")
    if not token:
        token = load_env_file("ui-design/.env").get("FIGMA_TOKEN", "").strip()
    return token


def _load_stitch_key() -> str:
    # Priority: TEST_STITCH_API_KEY in tests/.env → STITCH_API_KEY env var → ui-design/.env
    key = _env("TEST_STITCH_API_KEY") or os.environ.get("STITCH_API_KEY", "")
    if not key:
        key = load_env_file("ui-design/.env").get("STITCH_API_KEY", "").strip()
    return key


# ---------------------------------------------------------------------------
# Static / dry-run checks (no network)
# ---------------------------------------------------------------------------

def run_static_checks(reporter: Reporter) -> None:
    reporter.section("Static / dry-run checks")

    reporter.step("Figma URL configured")
    if FIGMA_FILE_KEY not in ("", "your-file-key"):
        reporter.ok(f"Figma file key: {FIGMA_FILE_KEY}")
    else:
        reporter.skip("Figma file key", "TEST_FIGMA_FILE_URL not set in tests/.env")

    reporter.step("Stitch IDs configured")
    if STITCH_PROJECT_ID not in ("", "your-project-id"):
        reporter.ok(f"Stitch project ID: {STITCH_PROJECT_ID}")
        if STITCH_SCREEN_ID not in ("", "your-screen-id"):
            reporter.ok(f"Stitch screen ID: {STITCH_SCREEN_ID}")
        else:
            reporter.skip("Stitch screen ID", "TEST_STITCH_SCREEN_ID not set in tests/.env")
    else:
        reporter.skip("Stitch IDs", "TEST_STITCH_PROJECT_URL not set in tests/.env")

    reporter.step("ui-design modules importable")
    _ui_design_dir = os.path.join(PROJECT_ROOT, "ui-design")
    if _ui_design_dir not in sys.path:
        sys.path.insert(0, _ui_design_dir)
    try:
        import figma_client as _fc  # noqa: F401
        import stitch_client as _sc  # noqa: F401
        reporter.ok("ui-design/figma_client and ui-design/stitch_client imported successfully")
    except ImportError as exc:
        reporter.fail("Module import failed", str(exc))


# ---------------------------------------------------------------------------
# Agent endpoint tests (requires running ui-design agent)
# ---------------------------------------------------------------------------

def run_agent_tests(reporter: Reporter, agent_url: str) -> None:
    reporter.section(f"UI Design Agent endpoints — {agent_url}")

    # Health
    reporter.step("Health check")
    status, body, _ = http_request(f"{agent_url}/health")
    reporter.show("Health", body)
    if status == 200 and body.get("status") == "ok":
        reporter.ok(f"Agent healthy: {body.get('service')}")
    elif status == 0:
        reporter.skip("Agent health check",
                      "Agent not running — start with: PYTHONPATH=. python3 ui-design/app.py")
        return
    else:
        reporter.fail("Agent health check failed", f"status={status}, body={body}")
        return

    # Agent card
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
        reporter.info("/figma/meta skipped — TEST_FIGMA_TOKEN not set in tests/.env")
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
        reporter.info("/figma/pages skipped — TEST_FIGMA_TOKEN not set in tests/.env")
    else:
        reporter.fail(f"Agent /figma/pages failed (status {status})", str(body)[:200])

    if pages:
        first_page_name = pages[0].get("name", "")
        reporter.step(f"GET /figma/page — page '{first_page_name}'")
        query_page = urlencode({"url": FIGMA_URL, "name": first_page_name})
        status, body, _ = http_request(f"{agent_url}/figma/page?{query_page}")
        reporter.show("Figma page", body)
        body_status = body.get("status", "") if isinstance(body, dict) else ""
        if status == 200 and body_status == "ok":
            page = body.get("page", {})
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
        reporter.info("/stitch/tools skipped — TEST_STITCH_API_KEY not set in tests/.env")
    else:
        reporter.fail(f"Agent /stitch/tools failed (status {status})", str(body)[:200])

    if STITCH_PROJECT_ID not in ("", "your-project-id"):
        reporter.step(f"GET /stitch/project — id={STITCH_PROJECT_ID}")
        status, body, _ = http_request(
            f"{agent_url}/stitch/project?{urlencode({'id': STITCH_PROJECT_ID})}"
        )
        reporter.show("Stitch project", body)
        if status == 200 and body.get("status") == "ok":
            reporter.ok("Agent /stitch/project: OK")
        elif not api_key:
            reporter.info("/stitch/project skipped — TEST_STITCH_API_KEY not set")
        else:
            reporter.fail(f"Agent /stitch/project failed (status {status})", str(body)[:200])

    if STITCH_SCREEN_ID not in ("", "your-screen-id"):
        query_scr = urlencode({"project_id": STITCH_PROJECT_ID, "screen_id": STITCH_SCREEN_ID})

        reporter.step(f"GET /stitch/screen — screen {STITCH_SCREEN_ID}")
        status, body, _ = http_request(f"{agent_url}/stitch/screen?{query_scr}")
        reporter.show("Stitch screen", body)
        if status == 200 and body.get("status") == "ok":
            image_urls = body.get("imageUrls", [])
            reporter.ok(f"Agent /stitch/screen: OK — imageUrls: {image_urls[:2]}")
        elif not api_key:
            reporter.info("/stitch/screen skipped — TEST_STITCH_API_KEY not set")
        else:
            reporter.fail(f"Agent /stitch/screen failed (status {status})", str(body)[:200])

        reporter.step("GET /stitch/screen/image")
        status, body, _ = http_request(f"{agent_url}/stitch/screen/image?{query_scr}")
        reporter.show("Stitch screen/image", body)
        if status == 200:
            image_urls = body.get("imageUrls", [])
            reporter.ok(
                f"Agent /stitch/screen/image: status={body.get('status')}, "
                f"imageUrls={image_urls[:2]}"
            )
        elif not api_key:
            reporter.info("/stitch/screen/image skipped — TEST_STITCH_API_KEY not set")
        else:
            reporter.fail(f"Agent /stitch/screen/image failed (status {status})", str(body)[:200])

    # --- A2A message interface ---
    reporter.step("POST /message:send — Figma task")
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
        time.sleep(2)
        if task_id:
            status2, body2, _ = http_request(f"{agent_url}/tasks/{task_id}")
            state2 = (body2.get("task", {}) or {}).get("status", {}).get("state", "")
            if state2 == "TASK_STATE_COMPLETED":
                reporter.ok(f"Figma A2A task completed: {task_id}")
            else:
                reporter.info(f"Figma A2A task state: {state2}")
    else:
        reporter.fail(f"Figma A2A /message:send failed (status {status})", str(body)[:200])

    if STITCH_SCREEN_ID not in ("", "your-screen-id"):
        reporter.step("POST /message:send — Stitch task")
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
                state2 = (body2.get("task", {}) or {}).get("status", {}).get("state", "")
                if state2 == "TASK_STATE_COMPLETED":
                    reporter.ok(f"Stitch A2A task completed: {task_id}")
                else:
                    reporter.info(f"Stitch A2A task state: {state2}")
        else:
            reporter.fail(f"Stitch A2A /message:send failed (status {status})", str(body)[:200])


# ---------------------------------------------------------------------------
# Argument parsing & main
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--integration", action="store_true",
                        help="Run live integration tests (requires running agent + credentials)")
    parser.add_argument("--agent-url", default="",
                        help="Agent base URL (default: http://127.0.0.1:8040)")
    parser.add_argument("--container", action="store_true",
                        help="Use container agent URL (http://127.0.0.1:8040)")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


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
        print(f"\nPassed: {reporter.passed}  Failed: {reporter.failed}  Skipped: {reporter.skipped}")
        return 0 if reporter.failed == 0 else 1

    figma_token = _load_figma_token()
    stitch_key = _load_stitch_key()
    if figma_token:
        os.environ["FIGMA_TOKEN"] = figma_token
    if stitch_key:
        os.environ["STITCH_API_KEY"] = stitch_key

    agent_url = agent_url_from_args(
        args,
        local_default=LOCAL_AGENT_URL,
        container_default=CONTAINER_AGENT_URL,
    )
    run_agent_tests(reporter, agent_url)

    print(f"\n{'=' * 60}")
    print(f"  Passed: {reporter.passed}   Failed: {reporter.failed}   Skipped: {reporter.skipped}")
    print(f"{'=' * 60}")
    return 0 if reporter.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
