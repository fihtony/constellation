#!/usr/bin/env python3
"""Tests for the agentic runtime interface (run_agentic, AgenticResult, AgenticCheckpoint)."""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest.mock import Mock, patch

# Ensure project root is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from common.runtime.adapter import AgenticCheckpoint, AgenticResult, AgentRuntimeAdapter, get_runtime
from common.runtime.mock import MockAdapter


class AgenticResultTests(unittest.TestCase):
    def test_default_fields(self):
        result = AgenticResult(success=True, summary="done")
        self.assertTrue(result.success)
        self.assertEqual(result.summary, "done")
        self.assertEqual(result.artifacts, [])
        self.assertEqual(result.tool_calls, [])
        self.assertIsNone(result.continuation)
        self.assertEqual(result.turns_used, 0)
        self.assertEqual(result.backend_used, "")
        self.assertEqual(result.evidence, [])
        self.assertEqual(result.approvals_used, [])
        self.assertEqual(result.policy_profile, "")
        self.assertIsNone(result.checkpoint_id)
        self.assertIsNone(result.verifier_summary)

    def test_full_fields(self):
        result = AgenticResult(
            success=False,
            summary="failed",
            artifacts=[{"type": "file", "path": "src/x.py"}],
            tool_calls=[{"name": "bash", "args": {"cmd": "ls"}}],
            continuation="sess-abc",
            raw_output="raw text",
            turns_used=12,
            backend_used="claude-code",
            evidence=[{"tool": "bash"}],
            approvals_used=[{"type": "manual"}],
            policy_profile="workspace-write",
            checkpoint_id="cp-1",
            verifier_summary="verified",
        )
        self.assertFalse(result.success)
        self.assertEqual(result.continuation, "sess-abc")
        self.assertEqual(result.turns_used, 12)
        self.assertEqual(result.backend_used, "claude-code")
        self.assertEqual(result.policy_profile, "workspace-write")
        self.assertEqual(result.checkpoint_id, "cp-1")
        self.assertEqual(result.verifier_summary, "verified")


class AgenticCheckpointTests(unittest.TestCase):
    def test_default_fields(self):
        cp = AgenticCheckpoint(task_id="t1", provider="claude-code", continuation=None, summary="partial")
        self.assertEqual(cp.task_id, "t1")
        self.assertEqual(cp.provider, "claude-code")
        self.assertIsNone(cp.continuation)
        self.assertEqual(cp.policy_hash, "")
        self.assertEqual(cp.toolset_hash, "")
        self.assertEqual(cp.open_questions, [])
        self.assertEqual(cp.pending_approvals, [])

    def test_provider_tag_prevents_cross_provider_reuse(self):
        cp_claude = AgenticCheckpoint(task_id="t1", provider="claude-code", continuation="sess-x", summary="")
        cp_connect = AgenticCheckpoint(task_id="t1", provider="connect-agent", continuation="thread-y", summary="")
        self.assertNotEqual(cp_claude.provider, cp_connect.provider)
        self.assertNotEqual(cp_claude.continuation, cp_connect.continuation)


class MockAdapterAgenticTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {}, clear=True)
        self.env_patcher.start()
        from common.runtime import adapter as m
        m._INSTANCES.clear()

    def tearDown(self):
        self.env_patcher.stop()

    def test_mock_run_agentic_returns_success(self):
        os.environ["AGENT_RUNTIME"] = "mock"
        runtime = get_runtime()
        result = runtime.run_agentic("do something")
        self.assertIsInstance(result, AgenticResult)
        self.assertTrue(result.success)
        self.assertEqual(result.backend_used, "mock")
        self.assertEqual(result.turns_used, 1)

    def test_mock_run_agentic_uses_env_response(self):
        os.environ["AGENT_RUNTIME"] = "mock"
        os.environ["MOCK_AGENTIC_RESPONSE"] = "custom agentic result"
        runtime = get_runtime()
        result = runtime.run_agentic("task")
        self.assertEqual(result.summary, "custom agentic result")

    def test_mock_run_agentic_calls_on_progress(self):
        os.environ["AGENT_RUNTIME"] = "mock"
        os.environ["MOCK_AGENTIC_RESPONSE"] = "progress update"
        runtime = get_runtime()
        progress_calls = []
        result = runtime.run_agentic("task", on_progress=progress_calls.append)
        self.assertTrue(result.success)
        self.assertGreater(len(progress_calls), 0)

    def test_mock_supports_mcp_default_false(self):
        os.environ["AGENT_RUNTIME"] = "mock"
        runtime = get_runtime()
        self.assertFalse(runtime.supports_mcp())

    def test_mock_supports_mcp_via_env(self):
        os.environ["AGENT_RUNTIME"] = "mock"
        os.environ["MOCK_SUPPORTS_MCP"] = "1"
        runtime = get_runtime()
        self.assertTrue(runtime.supports_mcp())


class BaseAdapterAgenticNotImplementedTests(unittest.TestCase):
    """The default run_agentic() raises NotImplementedError."""

    def test_base_run_agentic_raises(self):
        class _Minimal(AgentRuntimeAdapter):
            def run(self, prompt, **kwargs):
                return {}

        adapter = _Minimal()
        with self.assertRaises(NotImplementedError):
            adapter.run_agentic("task")

    def test_base_supports_mcp_false(self):
        class _Minimal(AgentRuntimeAdapter):
            def run(self, prompt, **kwargs):
                return {}

        adapter = _Minimal()
        self.assertFalse(adapter.supports_mcp())


class ClaudeCodeAgenticTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {}, clear=True)
        self.env_patcher.start()
        from common.runtime import adapter as m
        m._INSTANCES.clear()

    def tearDown(self):
        self.env_patcher.stop()

    def test_returns_failure_when_binary_missing(self):
        os.environ["AGENT_RUNTIME"] = "claude-code"
        with patch("common.runtime.claude_code.shutil.which", return_value=None):
            runtime = get_runtime()
            result = runtime.run_agentic("do something")
        self.assertIsInstance(result, AgenticResult)
        self.assertFalse(result.success)
        self.assertEqual(result.backend_used, "claude-code")
        self.assertIn("not found", result.summary)

    def test_run_agentic_succeeds_when_binary_available(self):
        os.environ["AGENT_RUNTIME"] = "claude-code"
        completed = Mock(return_value=Mock(returncode=0, stdout="task done\nARTIFACT:{}", stderr=""))
        with patch("common.runtime.claude_code.shutil.which", return_value="/usr/bin/claude"), \
             patch("common.runtime.claude_code.subprocess.run", completed):
            runtime = get_runtime()
            result = runtime.run_agentic("do something", tools=["bash", "read"])

        self.assertIsInstance(result, AgenticResult)
        self.assertTrue(result.success)
        self.assertEqual(result.backend_used, "claude-code")
        cmd = completed.call_args.args[0]
        self.assertIn("--print", cmd)
        self.assertIn("--allowedTools", cmd)
        self.assertIn("bash,read", cmd)

    def test_run_agentic_timeout_returns_failure(self):
        import subprocess
        os.environ["AGENT_RUNTIME"] = "claude-code"
        with patch("common.runtime.claude_code.shutil.which", return_value="/usr/bin/claude"), \
             patch("common.runtime.claude_code.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 30)):
            runtime = get_runtime()
            result = runtime.run_agentic("do something")

        self.assertFalse(result.success)
        self.assertIn("timed out", result.summary)

    def test_run_agentic_passes_mcp_config(self):
        import tempfile, json
        os.environ["AGENT_RUNTIME"] = "claude-code"
        completed = Mock(return_value=Mock(returncode=0, stdout="done", stderr=""))
        mcp_servers = {"constellation": {"command": "python3", "args": ["-m", "common.tools.mcp_adapter"]}}

        with patch("common.runtime.claude_code.shutil.which", return_value="/usr/bin/claude"), \
             patch("common.runtime.claude_code.subprocess.run", completed):
            runtime = get_runtime()
            result = runtime.run_agentic("do something", mcp_servers=mcp_servers)

        self.assertTrue(result.success)
        cmd = completed.call_args.args[0]
        self.assertIn("--mcp-config", cmd)

    def test_sdk_disallowed_tools_are_passed(self):
        os.environ["AGENT_RUNTIME"] = "claude-code"
        completed = Mock(return_value=Mock(returncode=0, stdout="done", stderr=""))
        with patch("common.runtime.claude_code.shutil.which", return_value="/usr/bin/claude"), \
             patch("common.runtime.claude_code.subprocess.run", completed):
            runtime = get_runtime()
            runtime.run_agentic("task")

        cmd = completed.call_args.args[0]
        self.assertIn("--disallowedTools", cmd)
        disallowed_idx = cmd.index("--disallowedTools")
        disallowed_str = cmd[disallowed_idx + 1]
        self.assertIn("AskUserQuestion", disallowed_str)

    def test_supports_mcp_true(self):
        os.environ["AGENT_RUNTIME"] = "claude-code"
        with patch("common.runtime.claude_code.shutil.which", return_value=None):
            runtime = get_runtime()
        self.assertTrue(runtime.supports_mcp())


class ConnectAgentAgenticTests(unittest.TestCase):
    class _FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {"AGENT_RUNTIME": "connect-agent", "OPENAI_MODEL": "gpt-5-mini"}, clear=True)
        self.env_patcher.start()
        from common.runtime import adapter as m
        m._INSTANCES.clear()
        from common.tools.registry import clear_registry
        clear_registry()

    def tearDown(self):
        self.env_patcher.stop()

    def test_run_agentic_bootstraps_tools_from_constellation_mcp_server(self):
        captured_payloads = []

        def _fake_urlopen(req, timeout=0):
            captured_payloads.append(json.loads(req.data.decode("utf-8")))
            return self._FakeResponse({
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "done"},
                }],
            })

        mcp_servers = {
            "constellation": {
                "command": "python3",
                "args": ["-m", "common.mcp.constellation_server", "--tools", "progress_tools"],
            }
        }

        with patch("common.runtime.connect_agent.transport.urlopen", side_effect=_fake_urlopen):
            runtime = get_runtime()
            result = runtime.run_agentic("do something", mcp_servers=mcp_servers, max_turns=1)

        self.assertTrue(result.success)
        self.assertEqual(result.backend_used, "connect-agent")
        self.assertIn("tools", captured_payloads[0])
        tool_names = [item["function"]["name"] for item in captured_payloads[0]["tools"]]
        self.assertIn("report_progress", tool_names)

    def test_run_agentic_keeps_default_rules_when_custom_system_prompt_is_supplied(self):
        captured_payloads = []

        def _fake_urlopen(req, timeout=0):
            captured_payloads.append(json.loads(req.data.decode("utf-8")))
            return self._FakeResponse({
                "choices": [{
                    "finish_reason": "stop",
                    "message": {"content": "done"},
                }],
            })

        with patch("common.runtime.connect_agent.transport.urlopen", side_effect=_fake_urlopen):
            runtime = get_runtime()
            result = runtime.run_agentic(
                "do something",
                system_prompt="CUSTOM TASK RULE: write the screenshot into the project folder.",
                max_turns=1,
            )

        self.assertTrue(result.success)
        system_prompt = captured_payloads[0]["messages"][0]["content"]
        self.assertIn("Treat explicit task-specific requirements as hard requirements", system_prompt)
        self.assertIn("CUSTOM TASK RULE: write the screenshot into the project folder.", system_prompt)


class ProviderRegistryTests(unittest.TestCase):
    def test_all_backends_auto_registered(self):
        # Importing each backend module triggers self-registration.
        from common.runtime.provider_registry import is_registered, list_runtimes
        import common.runtime.mock        # noqa: F401
        import common.runtime.claude_code # noqa: F401
        import common.runtime.connect_agent # noqa: F401
        import common.runtime.copilot_cli # noqa: F401

        for name in ("mock", "claude-code", "connect-agent", "copilot-cli"):
            self.assertTrue(is_registered(name), f"{name!r} should be registered")

    def test_duplicate_registration_raises(self):
        from common.runtime.provider_registry import _registry, register_runtime
        from common.runtime.mock import MockAdapter
        # Ensure mock is not registered by clearing temporarily
        original = _registry.pop("mock", None)
        try:
            register_runtime("mock", MockAdapter)
            with self.assertRaises(ValueError):
                register_runtime("mock", MockAdapter)
        finally:
            if original is not None:
                _registry["mock"] = original

    def test_get_runtime_class_unknown_raises(self):
        from common.runtime.provider_registry import get_runtime_class
        with self.assertRaises(KeyError):
            get_runtime_class("nonexistent-runtime-xyz")

    def test_runtime_launch_contribution_round_trip(self):
        from common.runtime.provider_registry import (
            RuntimeLaunchContribution,
            VolumeMount,
            _launch_registry,
            get_launch_contribution,
            register_runtime_launch,
        )

        original = _launch_registry.pop("demo-runtime", None)
        try:
            register_runtime_launch(
                "demo-runtime",
                lambda context: RuntimeLaunchContribution(
                    mounts=[VolumeMount(source="/host/demo", target="/mnt/demo", read_only=True)],
                    env={"DEMO_FLAG": context.get("flag", "0")},
                    launcher_profile="docker-sandbox",
                ),
            )
            contribution = get_launch_contribution("demo-runtime", {"flag": "1"})
            self.assertEqual(contribution.env["DEMO_FLAG"], "1")
            self.assertEqual(contribution.launcher_profile, "docker-sandbox")
            self.assertEqual(contribution.mounts[0].target, "/mnt/demo")
        finally:
            if original is None:
                _launch_registry.pop("demo-runtime", None)
            else:
                _launch_registry["demo-runtime"] = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
