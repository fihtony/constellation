#!/usr/bin/env python3
"""Focused tests for the unified runtime adapter contract."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from urllib.error import URLError
from unittest.mock import Mock, patch

from common.env_utils import load_dotenv
from common.runtime.adapter import get_runtime, summarize_runtime_configuration


class RuntimeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {}, clear=True)
        self.env_patcher.start()
        from common.runtime import adapter as runtime_adapter

        runtime_adapter._INSTANCES.clear()

    def tearDown(self):
        self.env_patcher.stop()

    def test_get_runtime_supports_all_documented_backends(self):
        for backend in ("copilot-cli", "claude-code", "copilot-connect", "mock"):
            runtime = get_runtime(backend)
            self.assertIsNotNone(runtime)

    def test_get_runtime_unknown_backend_falls_back_to_connect(self):
        runtime = get_runtime("does-not-exist")
        self.assertEqual(runtime.__class__.__name__, "CopilotConnectAdapter")

    def test_copilot_connect_uses_model_override_and_contract(self):
        os.environ["AGENT_RUNTIME"] = "copilot-connect"
        os.environ["OPENAI_BASE_URL"] = "http://example.test/v1"

        payload = {
            "choices": [
                {
                    "message": {
                        "content": '{"summary":"ok","artifacts":[],"warnings":[],"next_actions":[]}'
                    }
                }
            ]
        }

        class _Response:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return json.dumps(payload).encode("utf-8")

        with patch("common.runtime.copilot_connect.urlopen", return_value=_Response()) as mocked_open:
            result = get_runtime().run("hello", model="gpt-test", max_tokens=256)

        self.assertEqual(result["summary"], "ok")
        self.assertEqual(result["backend_used"], "copilot-connect")
        request = mocked_open.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "gpt-test")
        self.assertEqual(body["max_tokens"], 256)

    def test_copilot_cli_falls_back_to_connect_when_token_missing(self):
        os.environ["AGENT_RUNTIME"] = "copilot-cli"
        fallback_result = {
            "summary": "fallback",
            "structured_output": {},
            "artifacts": [],
            "warnings": [],
            "next_actions": [],
            "raw_response": "fallback",
            "backend_used": "copilot-connect",
        }
        with patch("common.runtime.copilot_connect.CopilotConnectAdapter.run", return_value=dict(fallback_result)):
            result = get_runtime().run("hello")
        self.assertEqual(result["summary"], "fallback")
        self.assertIn("COPILOT_GITHUB_TOKEN is not configured", result["warnings"][0])

    def test_copilot_cli_uses_cli_when_binary_and_token_are_configured(self):
        os.environ["AGENT_RUNTIME"] = "copilot-cli"
        os.environ["COPILOT_GITHUB_TOKEN"] = "github_pat_test"
        os.environ["COPILOT_MODEL"] = "gpt-5-mini"

        completed = Mock(return_value=Mock(returncode=0, stdout='{"summary":"cli ok"}', stderr=""))
        with patch("common.runtime.copilot_cli.shutil.which", return_value="/usr/bin/copilot"), \
             patch("common.runtime.copilot_cli.subprocess.run", completed):
            result = get_runtime().run("hello from cli")

        self.assertEqual(result["backend_used"], "copilot-cli")
        self.assertEqual(result["summary"], "cli ok")
        command = completed.call_args.args[0]
        self.assertEqual(command[0], "copilot")
        self.assertIn("--model", command)

    def test_copilot_connect_falls_back_to_mock_when_endpoint_is_unreachable(self):
        os.environ["AGENT_RUNTIME"] = "copilot-connect"
        os.environ["ALLOW_MOCK_FALLBACK"] = "1"

        with patch("common.runtime.copilot_connect.urlopen", side_effect=URLError("offline")):
            result = get_runtime().run("hello offline")

        self.assertEqual(result["backend_used"], "copilot-connect")
        self.assertIn("Fell back to mock response.", result["warnings"])
        self.assertIn("MOCK_LLM_RESPONSE", result["raw_response"])

    def test_mock_runtime_returns_configured_response(self):
        os.environ["AGENT_RUNTIME"] = "mock"
        os.environ["MOCK_RUNTIME_RESPONSE"] = '{"summary":"mock ok","artifacts":[],"warnings":[],"next_actions":[]}'

        result = get_runtime().run("hello mock")

        self.assertEqual(result["backend_used"], "mock")
        self.assertEqual(result["summary"], "mock ok")

    def test_claude_code_falls_back_to_connect_when_binary_missing(self):
        os.environ["AGENT_RUNTIME"] = "claude-code"
        fallback_result = {
            "summary": "fallback",
            "structured_output": {},
            "artifacts": [],
            "warnings": [],
            "next_actions": [],
            "raw_response": "fallback",
            "backend_used": "copilot-connect",
        }
        with patch("common.runtime.claude_code.shutil.which", return_value=None), \
             patch("common.runtime.copilot_connect.CopilotConnectAdapter.run", return_value=dict(fallback_result)):
            result = get_runtime().run("hello")
        self.assertEqual(result["summary"], "fallback")
        self.assertIn("Claude Code CLI binary", result["warnings"][0])

    def test_runtime_configuration_summary_is_redacted(self):
        os.environ["AGENT_RUNTIME"] = "copilot-cli"
        os.environ["COPILOT_GITHUB_TOKEN"] = "github_pat_secret"

        with patch("common.runtime.adapter.shutil.which", return_value="/usr/bin/copilot"):
            summary = summarize_runtime_configuration()

        self.assertEqual(summary["effectiveBackend"], "copilot-cli")
        self.assertTrue(summary["tokenConfigured"])
        self.assertTrue(summary["tokenSources"]["COPILOT_GITHUB_TOKEN"])
        self.assertNotIn("github_pat_secret", json.dumps(summary))

    def test_runtime_configuration_summary_falls_back_when_copilot_cli_is_not_ready(self):
        os.environ["AGENT_RUNTIME"] = "copilot-cli"

        with patch("common.runtime.adapter.shutil.which", return_value="/usr/bin/copilot"):
            summary = summarize_runtime_configuration()

        self.assertEqual(summary["requestedBackend"], "copilot-cli")
        self.assertEqual(summary["effectiveBackend"], "copilot-connect")
        self.assertFalse(summary["tokenConfigured"])
        self.assertEqual(summary["fallbackReason"], "Copilot CLI token is not configured.")

    def test_load_dotenv_applies_shared_defaults_and_local_overrides(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            common_dir = os.path.join(temp_dir, "common")
            agent_dir = os.path.join(temp_dir, "agent")
            os.makedirs(common_dir, exist_ok=True)
            os.makedirs(agent_dir, exist_ok=True)

            common_env = os.path.join(common_dir, ".env")
            agent_env = os.path.join(agent_dir, ".env")

            with open(common_env, "w", encoding="utf-8") as handle:
                handle.write("AGENT_RUNTIME=copilot-cli\nCOPILOT_MODEL=gpt-5-mini\n")
            with open(agent_env, "w", encoding="utf-8") as handle:
                handle.write("AGENT_RUNTIME=claude-code\n")

            merged = load_dotenv(agent_env)

            self.assertEqual(merged["AGENT_RUNTIME"], "claude-code")
            self.assertEqual(merged["COPILOT_MODEL"], "gpt-5-mini")
            self.assertEqual(os.environ["AGENT_RUNTIME"], "claude-code")

    def test_load_dotenv_treats_blank_process_env_as_missing(self):
        os.environ["COPILOT_GITHUB_TOKEN"] = ""

        with tempfile.TemporaryDirectory() as temp_dir:
            common_dir = os.path.join(temp_dir, "common")
            agent_dir = os.path.join(temp_dir, "agent")
            os.makedirs(common_dir, exist_ok=True)
            os.makedirs(agent_dir, exist_ok=True)

            common_env = os.path.join(common_dir, ".env")
            agent_env = os.path.join(agent_dir, ".env")

            with open(common_env, "w", encoding="utf-8") as handle:
                handle.write("COPILOT_GITHUB_TOKEN=github_pat_from_common\n")
            with open(agent_env, "w", encoding="utf-8") as handle:
                handle.write("OPENAI_MODEL=gpt-5-mini\n")

            merged = load_dotenv(agent_env)

            self.assertEqual(merged["COPILOT_GITHUB_TOKEN"], "github_pat_from_common")
            self.assertEqual(os.environ["COPILOT_GITHUB_TOKEN"], "github_pat_from_common")


if __name__ == "__main__":
    unittest.main(verbosity=2)