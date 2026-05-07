#!/usr/bin/env python3
"""Tests for the Jira boundary tools (common/tools/jira_tools.py).

Validates that all required Jira tools are registered, use the standard A2A
envelope (not JSON-RPC), and have correct schema definitions.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Import to trigger registration at module level (before any tests run)
import common.tools.jira_tools  # noqa: F401, E402

from common.tools.registry import (
    get_tool,
    is_registered,
    list_tools,
)


class JiraBoundaryToolRegistrationTests(unittest.TestCase):
    """Verify all required Jira boundary tools are registered."""

    EXPECTED_TOOLS = [
        "jira_get_ticket",
        "jira_add_comment",
        "jira_search",
        "jira_transition",
        "jira_assign",
        "jira_get_transitions",
        "jira_get_myself",
        "jira_create_issue",
        "jira_update_fields",
        "jira_validate_permissions",
    ]

    def test_all_jira_boundary_tools_are_registered(self):
        for name in self.EXPECTED_TOOLS:
            self.assertTrue(
                is_registered(name),
                f"Jira boundary tool '{name}' is not registered",
            )

    def test_jira_tool_count(self):
        """Should have at least 10 Jira boundary tools from jira_tools.py."""
        for name in self.EXPECTED_TOOLS:
            self.assertTrue(is_registered(name), f"Missing: {name}")

    def test_jira_tools_have_required_schema_fields(self):
        for name in self.EXPECTED_TOOLS:
            tool = get_tool(name)
            self.assertIsNotNone(tool, f"Tool {name} returned None")
            schema = tool.schema
            self.assertTrue(hasattr(schema, "name"))
            self.assertTrue(hasattr(schema, "description"))
            self.assertTrue(hasattr(schema, "input_schema"))
            self.assertIsInstance(schema.description, str)
            self.assertGreater(len(schema.description), 10)


class JiraA2AEnvelopeTests(unittest.TestCase):
    """Verify Jira boundary tools use the standard A2A envelope."""

    def test_a2a_send_uses_standard_envelope(self):
        """_a2a_send must use message + configuration, not JSON-RPC."""
        from common.tools import jira_tools as jt

        captured_payload = {}

        class _FakeResp:
            def read(self):
                return json.dumps({
                    "task": {
                        "id": "t-1",
                        "status": {"state": "TASK_STATE_COMPLETED"},
                        "artifacts": [{"parts": [{"text": "ok"}]}],
                    }
                }).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        def _capture_urlopen(req, **kw):
            body = json.loads(req.data.decode("utf-8"))
            captured_payload.update(body)
            return _FakeResp()

        with patch.object(jt, "urlopen", side_effect=_capture_urlopen):
            jt._a2a_send("http://jira:8080", "jira.ticket.fetch", {"ticketKey": "X-1"})

        # Must have standard A2A envelope
        self.assertIn("message", captured_payload, "Must use 'message' key, not JSON-RPC")
        self.assertIn("configuration", captured_payload, "Must include 'configuration'")
        self.assertTrue(
            captured_payload["configuration"].get("returnImmediately"),
            "Must set returnImmediately=True",
        )
        # Must NOT have JSON-RPC keys
        self.assertNotIn("jsonrpc", captured_payload, "Must not use JSON-RPC envelope")
        self.assertNotIn("method", captured_payload, "Must not use JSON-RPC 'method' key")

    def test_a2a_send_includes_requested_capability_in_metadata(self):
        """The A2A envelope must include requestedCapability in metadata."""
        from common.tools import jira_tools as jt

        captured_payload = {}

        class _FakeResp:
            def read(self):
                return json.dumps({
                    "task": {
                        "id": "t-1",
                        "status": {"state": "TASK_STATE_COMPLETED"},
                        "artifacts": [{"parts": [{"text": "ok"}]}],
                    }
                }).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        def _capture_urlopen(req, **kw):
            body = json.loads(req.data.decode("utf-8"))
            captured_payload.update(body)
            return _FakeResp()

        with patch.object(jt, "urlopen", side_effect=_capture_urlopen):
            jt._a2a_send("http://jira:8080", "jira.ticket.fetch", {"ticketKey": "X-1"})

        metadata = captured_payload.get("message", {}).get("metadata", {})
        self.assertEqual(
            metadata.get("requestedCapability"),
            "jira.ticket.fetch",
        )


class JiraToolInputValidationTests(unittest.TestCase):
    """Verify tools reject missing required arguments."""

    def test_get_ticket_rejects_empty_key(self):
        tool = get_tool("jira_get_ticket")
        result = tool.execute({"key": ""})
        self.assertTrue(result.get("isError", False))

    def test_add_comment_rejects_missing_comment(self):
        tool = get_tool("jira_add_comment")
        result = tool.execute({"key": "X-1", "comment": ""})
        self.assertTrue(result.get("isError", False))

    def test_search_rejects_empty_jql(self):
        tool = get_tool("jira_search")
        result = tool.execute({"jql": ""})
        self.assertTrue(result.get("isError", False))

    def test_transition_rejects_missing_fields(self):
        tool = get_tool("jira_transition")
        result = tool.execute({"key": "X-1", "transition_name": ""})
        self.assertTrue(result.get("isError", False))

    def test_assign_rejects_missing_account_id(self):
        tool = get_tool("jira_assign")
        result = tool.execute({"key": "X-1", "account_id": ""})
        self.assertTrue(result.get("isError", False))

    def test_create_issue_rejects_missing_summary(self):
        tool = get_tool("jira_create_issue")
        result = tool.execute({"project": "PROJ", "summary": ""})
        self.assertTrue(result.get("isError", False))

    def test_update_fields_rejects_missing_fields(self):
        tool = get_tool("jira_update_fields")
        result = tool.execute({"key": "X-1", "fields": None})
        self.assertTrue(result.get("isError", False))

    def test_validate_permissions_rejects_empty_action(self):
        tool = get_tool("jira_validate_permissions")
        result = tool.execute({"action": "", "target": "X-1"})
        self.assertTrue(result.get("isError", False))


if __name__ == "__main__":
    unittest.main()
