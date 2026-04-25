#!/usr/bin/env python3
"""Dedicated Figma agent integration test."""

from __future__ import annotations

import argparse
import os
from urllib.parse import urlencode

from agent_test_support import (
    PROJECT_ROOT,
    Reporter,
    agent_url_from_args,
    http_request,
    load_env_file,
    summary_exit_code,
)

FIGMA_URL = (
    "https://www.figma.com/design/vebUzJrO4bOj2nAajcBruU/"
    "Updated-navigation?node-id=569-1429&p=f&m=dev"
)
PAGE_NAME = "Content"
FILE_KEY = "vebUzJrO4bOj2nAajcBruU"
NODE_ID = "569:1429"
LOCAL_AGENT_URL = "http://127.0.0.1:18030"
CONTAINER_AGENT_URL = "http://127.0.0.1:8030"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-url", default="")
    parser.add_argument("--container", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    reporter = Reporter(verbose=args.verbose)
    agent_url = agent_url_from_args(
        args,
        local_default=LOCAL_AGENT_URL,
        container_default=CONTAINER_AGENT_URL,
    )
    env_values = load_env_file("figma/.env")
    token = env_values.get("FIGMA_TOKEN", "")
    ca_bundle = os.path.join(PROJECT_ROOT, "certs", "slf-ca-bundle.crt")

    reporter.section("Figma Agent Integration")
    if not token:
        reporter.fail("FIGMA_TOKEN is missing in figma/.env")
        return summary_exit_code(reporter)

    reporter.step("Validate direct Figma file access with the updated token")
    status, body, _ = http_request(
        f"https://api.figma.com/v1/files/{FILE_KEY}",
        headers={"X-Figma-Token": token, "Accept": "application/json"},
        ca_bundle=ca_bundle,
    )
    reporter.show("Direct file fetch", body)
    if status == 200 and body.get("name") == "Updated navigation":
        reporter.ok("Updated Figma token can read the file")
    else:
        reporter.fail("Updated Figma token still cannot read the file", f"status={status}, body={body}")
        return summary_exit_code(reporter)

    reporter.step("Validate direct Figma node access")
    status, body, _ = http_request(
        f"https://api.figma.com/v1/files/{FILE_KEY}/nodes?ids={NODE_ID}",
        headers={"X-Figma-Token": token, "Accept": "application/json"},
        ca_bundle=ca_bundle,
    )
    reporter.show("Direct node fetch", body)
    nodes = body.get("nodes", {}) if isinstance(body, dict) else {}
    if status == 200 and NODE_ID in nodes:
        reporter.ok("Updated Figma token can read the requested node")
    else:
        reporter.fail("Updated Figma token cannot read the requested node", f"status={status}, body={body}")
        return summary_exit_code(reporter)

    reporter.step("Check Figma agent health")
    status, body, _ = http_request(f"{agent_url}/health")
    if status == 200:
        reporter.ok("Figma agent is healthy")
    else:
        reporter.fail("Figma agent health check failed", f"status={status}, body={body}")
        return summary_exit_code(reporter)

    reporter.step("Fetch file metadata through the Figma agent")
    query = urlencode({"url": FIGMA_URL})
    status, body, _ = http_request(f"{agent_url}/figma/meta?{query}")
    reporter.show("Agent meta", body)
    meta = body.get("meta", {}) if isinstance(body, dict) else {}
    if status == 200 and body.get("status") == "ok" and meta.get("name") == "Updated navigation":
        reporter.ok("Figma agent fetched file metadata")
    else:
        reporter.fail("Figma agent failed to fetch file metadata", f"status={status}, body={body}")

    reporter.step("List pages through the Figma agent")
    status, body, _ = http_request(f"{agent_url}/figma/pages?{query}")
    reporter.show("Agent pages", body)
    pages = body.get("pages", []) if isinstance(body, dict) else []
    page_names = [item.get("name") for item in pages if isinstance(item, dict)]
    if status == 200 and PAGE_NAME in page_names:
        reporter.ok("Figma agent listed the expected page")
    else:
        reporter.fail("Figma agent failed to list the expected page", f"status={status}, body={body}")

    reporter.step("Fetch the requested page by name through the Figma agent")
    query = urlencode({"url": FIGMA_URL, "name": PAGE_NAME})
    status, body, _ = http_request(f"{agent_url}/figma/page?{query}")
    reporter.show("Agent page", body)
    page = body.get("page", {}) if isinstance(body, dict) else {}
    if status == 200 and body.get("status") == "ok" and page.get("name") == PAGE_NAME:
        reporter.ok("Figma agent fetched the requested page")
    else:
        reporter.fail("Figma agent failed to fetch the requested page", f"status={status}, body={body}")

    reporter.step("Exercise the message interface")
    status, body, _ = http_request(
        f"{agent_url}/message:send",
        method="POST",
        payload={
            "message": {
                "messageId": "figma-agent-test",
                "role": "ROLE_USER",
                "parts": [{"text": f"Fetch this Figma URL and page {PAGE_NAME}: {FIGMA_URL}"}],
            }
        },
    )
    reporter.show("Message send", body)
    task = body.get("task", {}) if isinstance(body, dict) else {}
    state = task.get("status", {}).get("state")
    if status == 200 and state == "TASK_STATE_COMPLETED":
        reporter.ok("Figma message flow completed")
    else:
        reporter.fail("Figma message flow failed", f"status={status}, body={body}")

    return summary_exit_code(reporter)


if __name__ == "__main__":
    raise SystemExit(main())