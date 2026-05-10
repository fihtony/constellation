#!/usr/bin/env python3
"""Regression tests for boundary-agent discovery helpers."""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from common.tools import design_tools as dt
from common.tools import jira_tools as jt
from common.tools import scm_tools as st


class _FakeResp:
    def __init__(self, payload: dict | list):
        self._payload = payload

    def read(self):
        return json.dumps(self._payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_discover_jira_url_accepts_list_response_and_service_url():
    payload = [
        {
            "agent_id": "jira-agent",
            "instances": [
                {
                    "service_url": "http://jira:8010",
                }
            ],
        }
    ]
    with patch("common.tools.agent_discovery.urlopen", return_value=_FakeResp(payload)):
        assert jt._discover_jira_url("jira.ticket.fetch") == "http://jira:8010"


def test_discover_scm_url_falls_back_to_card_url():
    payload = {
        "agents": [
            {
                "agent_id": "scm-agent",
                "instances": [],
                "card_url": "http://scm:8020/.well-known/agent-card.json",
            }
        ]
    }
    with patch("common.tools.agent_discovery.urlopen", return_value=_FakeResp(payload)):
        assert st._discover_scm_url("scm.repo.inspect") == "http://scm:8020"


def test_discover_design_url_accepts_list_response_and_service_url():
    payload = [
        {
            "agent_id": "ui-design-agent",
            "instances": [
                {
                    "service_url": "http://ui-design:8040",
                }
            ],
        }
    ]
    with patch("common.tools.agent_discovery.urlopen", return_value=_FakeResp(payload)):
        assert dt._discover_design_url("figma.page.fetch") == "http://ui-design:8040"


def test_design_tool_uses_registered_capabilities():
    figma_tool = dt.FigmaFetchScreenTool()
    stitch_tool = dt.StitchFetchScreenTool()

    with patch.object(dt, "_discover_design_url", return_value="http://ui-design:8040") as discover_mock, \
         patch.object(dt, "_a2a_send", return_value={"task": {"status": {"state": "TASK_STATE_COMPLETED"}}}) as send_mock:
        figma_tool.execute({"figma_url": "https://example.test/file"})
        figma_tool.execute({"figma_url": "https://example.test/file", "node_id": "1:2"})
        stitch_tool.execute({"screen_id": "screen-1"})

    assert discover_mock.call_args_list[0].args[0] == "figma.page.fetch"
    assert discover_mock.call_args_list[1].args[0] == "figma.node.get"
    assert discover_mock.call_args_list[2].args[0] == "stitch.screen.fetch"
    assert send_mock.call_args_list[0].args[1] == "figma.page.fetch"
    assert send_mock.call_args_list[1].args[1] == "figma.node.get"
    assert send_mock.call_args_list[2].args[1] == "stitch.screen.fetch"


def test_design_a2a_send_uses_standard_envelope_and_polls_terminal_task():
    captured_requests: list[tuple[str, dict]] = []

    def _fake_urlopen(req, timeout=0):
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        captured_requests.append((req.full_url, body))
        if req.full_url.endswith("/message:send"):
            return _FakeResp(
                {
                    "task": {
                        "id": "design-task-1",
                        "status": {"state": "TASK_STATE_WORKING"},
                    }
                }
            )
        return _FakeResp(
            {
                "task": {
                    "id": "design-task-1",
                    "status": {"state": "TASK_STATE_COMPLETED"},
                    "artifacts": [{"parts": [{"text": "ok"}]}],
                }
            }
        )

    with patch.object(dt, "urlopen", side_effect=_fake_urlopen):
        task = dt._a2a_send("http://ui-design:8040", "ui-design.figma.fetch", {"figma_url": "https://example.test/file"})

    send_url, send_body = captured_requests[0]
    assert send_url == "http://ui-design:8040/message:send"
    assert "message" in send_body
    assert "configuration" in send_body
    assert send_body["configuration"]["returnImmediately"] is True
    assert "jsonrpc" not in send_body
    assert send_body["message"]["metadata"]["requestedCapability"] == "ui-design.figma.fetch"
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert captured_requests[1][0] == "http://ui-design:8040/tasks/design-task-1"