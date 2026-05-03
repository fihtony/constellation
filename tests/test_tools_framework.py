#!/usr/bin/env python3
"""Tests for the unified tool framework (base, registry, mcp_adapter, native_adapter)."""

from __future__ import annotations

import io
import json
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import clear_registry, get_tool, is_registered, list_tools, register_tool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_tool(name: str, description: str = "test tool") -> ConstellationTool:
    class _T(ConstellationTool):
        @property
        def schema(self) -> ToolSchema:
            return ToolSchema(
                name=name,
                description=description,
                input_schema={"type": "object", "properties": {}, "required": []},
            )

        def execute(self, args: dict) -> dict:
            return self.ok(f"executed {name} with {args}")

    return _T()


# ---------------------------------------------------------------------------
# Registry tests
# ---------------------------------------------------------------------------

class ToolRegistryTests(unittest.TestCase):
    def setUp(self):
        clear_registry()

    def tearDown(self):
        clear_registry()

    def test_register_and_retrieve(self):
        tool = _make_tool("test_tool_a")
        register_tool(tool)
        retrieved = get_tool("test_tool_a")
        self.assertIs(retrieved, tool)

    def test_list_tools_returns_registered_order(self):
        a = _make_tool("aaa")
        b = _make_tool("bbb")
        register_tool(a)
        register_tool(b)
        tools = list_tools()
        self.assertEqual([t.schema.name for t in tools], ["aaa", "bbb"])

    def test_duplicate_registration_raises(self):
        register_tool(_make_tool("dup"))
        with self.assertRaises(ValueError):
            register_tool(_make_tool("dup"))

    def test_get_unknown_tool_raises(self):
        with self.assertRaises(KeyError) as ctx:
            get_tool("nonexistent_xyz")
        self.assertIn("nonexistent_xyz", str(ctx.exception))

    def test_is_registered(self):
        self.assertFalse(is_registered("mytest"))
        register_tool(_make_tool("mytest"))
        self.assertTrue(is_registered("mytest"))

    def test_clear_registry(self):
        register_tool(_make_tool("to_clear"))
        clear_registry()
        self.assertEqual(list_tools(), [])


# ---------------------------------------------------------------------------
# Tool base class
# ---------------------------------------------------------------------------

class ToolBaseTests(unittest.TestCase):
    def test_ok_helper(self):
        tool = _make_tool("t")
        result = tool.ok("hello")
        self.assertEqual(result["isError"], False)
        self.assertEqual(result["content"][0]["text"], "hello")

    def test_error_helper(self):
        tool = _make_tool("t")
        result = tool.error("oops")
        self.assertTrue(result["isError"])
        self.assertEqual(result["content"][0]["text"], "oops")

    def test_execute_returns_ok(self):
        tool = _make_tool("exec_test")
        result = tool.execute({"x": 1})
        self.assertFalse(result["isError"])
        self.assertIn("exec_test", result["content"][0]["text"])


# ---------------------------------------------------------------------------
# MCP adapter
# ---------------------------------------------------------------------------

class McpAdapterTests(unittest.TestCase):
    def setUp(self):
        clear_registry()

    def tearDown(self):
        clear_registry()

    def _run_server(self, lines: list[str]) -> list[dict]:
        from common.tools.mcp_adapter import start_mcp_server
        stdin = io.StringIO("\n".join(lines) + "\n")
        stdout = io.StringIO()
        start_mcp_server(stdin=stdin, stdout=stdout)
        stdout.seek(0)
        return [json.loads(line) for line in stdout if line.strip()]

    def test_initialize(self):
        req = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        responses = self._run_server([req])
        self.assertEqual(len(responses), 1)
        result = responses[0]["result"]
        self.assertIn("protocolVersion", result)
        self.assertEqual(result["serverInfo"]["name"], "constellation")

    def test_tools_list_empty(self):
        req = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        responses = self._run_server([req])
        self.assertEqual(responses[0]["result"]["tools"], [])

    def test_tools_list_with_registered_tool(self):
        register_tool(_make_tool("list_me", "A listed tool"))
        req = json.dumps({"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}})
        responses = self._run_server([req])
        tools = responses[0]["result"]["tools"]
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]["name"], "list_me")
        self.assertEqual(tools[0]["description"], "A listed tool")

    def test_tools_call_executes_tool(self):
        register_tool(_make_tool("callable_tool"))
        req = json.dumps({
            "jsonrpc": "2.0", "id": 4, "method": "tools/call",
            "params": {"name": "callable_tool", "arguments": {"foo": "bar"}},
        })
        responses = self._run_server([req])
        result = responses[0]["result"]
        self.assertFalse(result.get("isError", True))
        self.assertIn("callable_tool", result["content"][0]["text"])

    def test_tools_call_unknown_tool_returns_error(self):
        req = json.dumps({
            "jsonrpc": "2.0", "id": 5, "method": "tools/call",
            "params": {"name": "no_such_tool", "arguments": {}},
        })
        responses = self._run_server([req])
        self.assertIn("error", responses[0])

    def test_unknown_method_returns_error(self):
        req = json.dumps({"jsonrpc": "2.0", "id": 6, "method": "tools/unknown", "params": {}})
        responses = self._run_server([req])
        self.assertIn("error", responses[0])

    def test_invalid_json_returns_error(self):
        responses = self._run_server(["not valid json"])
        self.assertIn("error", responses[0])

    def test_multiple_requests(self):
        register_tool(_make_tool("multi_test"))
        init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        lst = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        responses = self._run_server([init, lst])
        self.assertEqual(len(responses), 2)


# ---------------------------------------------------------------------------
# Native adapter
# ---------------------------------------------------------------------------

class NativeAdapterTests(unittest.TestCase):
    def setUp(self):
        clear_registry()

    def tearDown(self):
        clear_registry()

    def test_get_function_definitions_empty(self):
        from common.tools.native_adapter import get_function_definitions
        defs = get_function_definitions()
        self.assertEqual(defs, [])

    def test_get_function_definitions_with_tools(self):
        from common.tools.native_adapter import get_function_definitions
        register_tool(_make_tool("fn_tool", "A function tool"))
        defs = get_function_definitions()
        self.assertEqual(len(defs), 1)
        self.assertEqual(defs[0]["type"], "function")
        self.assertEqual(defs[0]["function"]["name"], "fn_tool")

    def test_dispatch_function_call(self):
        from common.tools.native_adapter import dispatch_function_call
        register_tool(_make_tool("dispatch_me"))
        result = dispatch_function_call("dispatch_me", {"key": "val"})
        self.assertIn("dispatch_me", result)

    def test_dispatch_unknown_raises(self):
        from common.tools.native_adapter import dispatch_function_call
        with self.assertRaises(KeyError):
            dispatch_function_call("not_a_real_tool", {})


# ---------------------------------------------------------------------------
# Barrel import tests
# ---------------------------------------------------------------------------

class BarrelImportTests(unittest.TestCase):
    def setUp(self):
        clear_registry()

    def tearDown(self):
        clear_registry()

    def test_dev_agent_barrel_registers_expected_tools(self):
        import common.tools.dev_agent_tools  # noqa: F401
        expected = {
            "jira_get_ticket",
            "jira_add_comment",
            "scm_create_branch",
            "scm_push_files",
            "scm_create_pr",
            "design_fetch_figma_screen",
            "design_fetch_stitch_screen",
            "report_progress",
        }
        registered = {t.schema.name for t in list_tools()}
        self.assertTrue(expected.issubset(registered), f"Missing: {expected - registered}")

    def test_team_lead_barrel_registers_extra_tools(self):
        import common.tools.team_lead_tools  # noqa: F401
        registered = {t.schema.name for t in list_tools()}
        self.assertIn("registry_query", registered)
        self.assertIn("registry_list_agents", registered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
