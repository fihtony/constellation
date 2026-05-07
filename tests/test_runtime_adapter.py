#!/usr/bin/env python3
"""Focused tests for the unified runtime adapter contract."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from urllib.error import URLError
from unittest.mock import Mock, patch

from common.env_utils import (
    build_isolated_copilot_env,
    build_isolated_git_env,
    load_dotenv,
    resolve_openai_base_url,
    sanitize_credential_env,
)
from common.runtime.adapter import get_runtime, require_agentic_runtime, summarize_runtime_configuration


class RuntimeAdapterTests(unittest.TestCase):
    def setUp(self):
        self.env_patcher = patch.dict(os.environ, {}, clear=True)
        self.env_patcher.start()
        from common.runtime import adapter as runtime_adapter

        runtime_adapter._INSTANCES.clear()

    def tearDown(self):
        self.env_patcher.stop()

    def test_get_runtime_supports_all_documented_backends(self):
        for backend in ("connect-agent", "copilot-cli", "claude-code"):
            runtime = get_runtime(backend)
            self.assertIsNotNone(runtime)

    def test_get_runtime_unknown_backend_raises(self):
        with self.assertRaises(KeyError):
            get_runtime("does-not-exist")

    def test_copilot_connect_runtime_is_rejected(self):
        os.environ["AGENT_RUNTIME"] = "copilot-connect"
        with self.assertRaises(KeyError):
            get_runtime()

    def test_connect_agent_uses_model_override_and_contract(self):
        os.environ["AGENT_RUNTIME"] = "connect-agent"
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

        with patch("common.runtime.connect_agent.transport.urlopen", return_value=_Response()) as mocked_open:
            result = get_runtime().run("hello", model="gpt-test", max_tokens=256)

        self.assertEqual(result["summary"], "ok")
        self.assertEqual(result["backend_used"], "connect-agent")
        request = mocked_open.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["model"], "gpt-test")
        self.assertEqual(body["max_tokens"], 256)

    def test_copilot_cli_fails_when_token_missing(self):
        os.environ["AGENT_RUNTIME"] = "copilot-cli"
        result = get_runtime().run("hello")
        self.assertIn("COPILOT_GITHUB_TOKEN is not configured", result["summary"])
        self.assertEqual(result["backend_used"], "copilot-cli")

    def test_copilot_cli_fails_when_generic_github_tokens_only(self):
        os.environ["AGENT_RUNTIME"] = "copilot-cli"
        os.environ["GH_TOKEN"] = "ambient_gh_token"
        os.environ["GITHUB_TOKEN"] = "personal_github_token"

        result = get_runtime().run("hello")

        self.assertIn("COPILOT_GITHUB_TOKEN is not configured", result["summary"])
        self.assertEqual(result["backend_used"], "copilot-cli")

    def test_copilot_cli_uses_cli_when_binary_and_token_are_configured(self):
        os.environ["AGENT_RUNTIME"] = "copilot-cli"
        os.environ["COPILOT_GITHUB_TOKEN"] = "copilot_token_test"
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

    def test_copilot_cli_executes_in_isolated_home(self):
        os.environ["AGENT_RUNTIME"] = "copilot-cli"
        os.environ["COPILOT_GITHUB_TOKEN"] = "copilot_token_test"
        os.environ["GH_TOKEN"] = "ambient_gh_token"
        os.environ["GITHUB_TOKEN"] = "personal_github_token"
        os.environ["HOME"] = "/Users/personal"

        completed = Mock(return_value=Mock(returncode=0, stdout='{"summary":"cli ok"}', stderr=""))
        with patch("common.runtime.copilot_cli.shutil.which", return_value="/usr/bin/copilot"), \
             patch("common.runtime.copilot_cli.subprocess.run", completed):
            get_runtime().run("hello from cli")

        env = completed.call_args.kwargs["env"]
        self.assertEqual(env["COPILOT_GITHUB_TOKEN"], "copilot_token_test")
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotEqual(env["HOME"], "/Users/personal")
        self.assertTrue(env["COPILOT_HOME"].startswith(env["HOME"]))
        self.assertTrue(env["GH_CONFIG_DIR"].startswith(env["XDG_CONFIG_HOME"]))

    def test_connect_agent_fails_when_endpoint_is_unreachable(self):
        os.environ["AGENT_RUNTIME"] = "connect-agent"

        with patch("common.runtime.connect_agent.transport.urlopen", side_effect=URLError("offline")):
            result = get_runtime().run("hello offline")

        self.assertEqual(result["backend_used"], "connect-agent")
        self.assertIn("unreachable", result["summary"])

    def test_claude_code_fails_when_binary_missing(self):
        os.environ["AGENT_RUNTIME"] = "claude-code"
        with patch("common.runtime.claude_code.shutil.which", return_value=None):
            result = get_runtime().run("hello")
        self.assertIn("not found", result["summary"])
        self.assertEqual(result["backend_used"], "claude-code")

    def test_runtime_configuration_summary_is_redacted(self):
        os.environ["AGENT_RUNTIME"] = "copilot-cli"
        os.environ["COPILOT_GITHUB_TOKEN"] = "copilot_token_secret"

        with patch("common.runtime.adapter.shutil.which", return_value="/usr/bin/copilot"):
            summary = summarize_runtime_configuration()

        self.assertEqual(summary["effectiveBackend"], "copilot-cli")
        self.assertTrue(summary["supportsAgentic"])
        self.assertTrue(summary["agenticReady"])
        self.assertTrue(summary["tokenConfigured"])
        self.assertTrue(summary["tokenSources"]["COPILOT_GITHUB_TOKEN"])
        self.assertNotIn("copilot_token_secret", json.dumps(summary))

    def test_runtime_configuration_summary_reports_error_when_copilot_cli_is_not_ready(self):
        os.environ["AGENT_RUNTIME"] = "copilot-cli"

        with patch("common.runtime.adapter.shutil.which", return_value="/usr/bin/copilot"):
            summary = summarize_runtime_configuration()

        self.assertEqual(summary["requestedBackend"], "copilot-cli")
        self.assertEqual(summary["effectiveBackend"], "copilot-cli")
        self.assertFalse(summary["tokenConfigured"])
        self.assertTrue(summary["supportsAgentic"])
        self.assertIn("not ready", summary["error"])

    def test_resolve_openai_base_url_uses_rancher_host_inside_container(self):
        os.environ["CONTAINER_RUNTIME"] = "rancher"

        with patch("common.env_utils._is_containerized_process", return_value=True):
            self.assertEqual(
                resolve_openai_base_url(),
                "http://host.rancher-desktop.internal:1288/v1",
            )

    def test_resolve_openai_base_url_uses_localhost_for_host_process(self):
        os.environ["CONTAINER_RUNTIME"] = "rancher"

        with patch("common.env_utils._is_containerized_process", return_value=False):
            self.assertEqual(resolve_openai_base_url(), "http://localhost:1288/v1")

    def test_runtime_configuration_summary_reports_connect_agent_details(self):
        os.environ["AGENT_RUNTIME"] = "connect-agent"

        with patch("common.runtime.adapter.resolve_openai_base_url", return_value="http://host.rancher-desktop.internal:1288/v1"):
            summary = summarize_runtime_configuration()

        self.assertEqual(summary["effectiveBackend"], "connect-agent")
        self.assertTrue(summary["supportsAgentic"])
        self.assertTrue(summary["agenticReady"])
        self.assertEqual(summary["resolvedBaseUrl"], "http://host.rancher-desktop.internal:1288/v1")
        self.assertFalse(summary["baseUrlConfigured"])

    def test_runtime_configuration_summary_reports_claude_agentic_readiness(self):
        os.environ["AGENT_RUNTIME"] = "claude-code"

        with patch("common.runtime.adapter.shutil.which", return_value="/usr/bin/claude"):
            summary = summarize_runtime_configuration()

        self.assertEqual(summary["effectiveBackend"], "claude-code")
        self.assertTrue(summary["supportsAgentic"])
        self.assertTrue(summary["agenticReady"])

    def test_require_agentic_runtime_accepts_ready_copilot_cli(self):
        os.environ["AGENT_RUNTIME"] = "copilot-cli"
        os.environ["COPILOT_GITHUB_TOKEN"] = "copilot_token_test"

        with patch("common.runtime.adapter.shutil.which", return_value="/usr/bin/copilot"):
            summary = require_agentic_runtime("Team Lead")

        self.assertEqual(summary["effectiveBackend"], "copilot-cli")
        self.assertTrue(summary["agenticReady"])

    def test_require_agentic_runtime_rejects_unready_copilot_cli(self):
        os.environ["AGENT_RUNTIME"] = "copilot-cli"

        with self.assertRaisesRegex(RuntimeError, "cannot start agentic execution"):
            require_agentic_runtime("Team Lead")

    def test_copilot_cli_run_agentic_executes_tools_until_final_answer(self):
        os.environ["AGENT_RUNTIME"] = "copilot-cli"
        os.environ["COPILOT_GITHUB_TOKEN"] = "copilot_token_test"

        responses = [
            {
                "summary": "calling tool",
                "raw_response": '<tool_call name="demo_tool">{"value": 7}</tool_call>',
                "warnings": [],
            },
            {
                "summary": "finished",
                "raw_response": "<final_answer>done after tool call</final_answer>",
                "warnings": [],
            },
        ]

        with patch("common.runtime.copilot_cli.shutil.which", return_value="/usr/bin/copilot"), \
             patch("common.runtime.copilot_cli.CopilotCliAdapter.run", side_effect=responses) as mocked_run, \
             patch("common.runtime.copilot_cli._dispatch_tool", return_value="tool ok") as mocked_dispatch:
            result = get_runtime().run_agentic("do something", tools=["demo_tool"], max_turns=3)

        self.assertTrue(result.success)
        self.assertEqual(result.backend_used, "copilot-cli")
        self.assertEqual(result.summary, "done after tool call")
        self.assertEqual(result.turns_used, 2)
        self.assertEqual(result.tool_calls[0]["name"], "demo_tool")
        self.assertEqual(result.tool_calls[0]["arguments"], {"value": 7})
        mocked_dispatch.assert_called_once_with("demo_tool", {"value": 7})
        self.assertEqual(mocked_run.call_count, 2)

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
                handle.write("COPILOT_GITHUB_TOKEN=copilot_token_from_common\n")
            with open(agent_env, "w", encoding="utf-8") as handle:
                handle.write("OPENAI_MODEL=gpt-5-mini\n")

            merged = load_dotenv(agent_env)

            self.assertEqual(merged["COPILOT_GITHUB_TOKEN"], "copilot_token_from_common")
            self.assertEqual(os.environ["COPILOT_GITHUB_TOKEN"], "copilot_token_from_common")

    def test_load_dotenv_ignores_ambient_github_credentials_by_default(self):
        os.environ["GH_TOKEN"] = "ambient_gh_host"
        os.environ["GITHUB_TOKEN"] = "ambient_github_token"
        os.environ["COPILOT_GITHUB_TOKEN"] = "ambient_copilot_token"

        with tempfile.TemporaryDirectory() as temp_dir:
            common_dir = os.path.join(temp_dir, "common")
            agent_dir = os.path.join(temp_dir, "agent")
            os.makedirs(common_dir, exist_ok=True)
            os.makedirs(agent_dir, exist_ok=True)

            common_env = os.path.join(common_dir, ".env")
            agent_env = os.path.join(agent_dir, ".env")

            with open(common_env, "w", encoding="utf-8") as handle:
                handle.write("COPILOT_GITHUB_TOKEN=copilot_token_from_common\n")
            with open(agent_env, "w", encoding="utf-8") as handle:
                handle.write("OPENAI_MODEL=gpt-5-mini\n")

            load_dotenv(agent_env)

        self.assertEqual(os.environ["COPILOT_GITHUB_TOKEN"], "copilot_token_from_common")
        self.assertNotIn("GH_TOKEN", os.environ)
        self.assertNotIn("GITHUB_TOKEN", os.environ)

    def test_load_dotenv_keeps_trusted_credential_overrides(self):
        os.environ["CONSTELLATION_TRUSTED_ENV"] = "1"
        os.environ["SCM_TOKEN"] = "token_from_tests_env"

        with tempfile.TemporaryDirectory() as temp_dir:
            common_dir = os.path.join(temp_dir, "common")
            agent_dir = os.path.join(temp_dir, "agent")
            os.makedirs(common_dir, exist_ok=True)
            os.makedirs(agent_dir, exist_ok=True)

            common_env = os.path.join(common_dir, ".env")
            agent_env = os.path.join(agent_dir, ".env")

            with open(common_env, "w", encoding="utf-8") as handle:
                handle.write("SCM_TOKEN=token_from_common\n")
            with open(agent_env, "w", encoding="utf-8") as handle:
                handle.write("OPENAI_MODEL=gpt-5-mini\n")

            load_dotenv(agent_env)

        self.assertEqual(os.environ["SCM_TOKEN"], "token_from_tests_env")

    def test_sanitize_credential_env_strips_ambient_tokens(self):
        env = sanitize_credential_env(
            {
                "HOME": "/Users/personal",
                "GH_TOKEN": "ambient_gh_host",
                "GITHUB_TOKEN": "ambient_github_token",
                "SCM_TOKEN": "scm_token_example",
                "PATH": "/usr/bin",
            },
            keep={"COPILOT_GITHUB_TOKEN": "copilot_token_runtime"},
        )

        self.assertEqual(env["COPILOT_GITHUB_TOKEN"], "copilot_token_runtime")
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("SCM_TOKEN", env)

    def test_build_isolated_git_env_uses_runtime_home(self):
        env = build_isolated_git_env(
            {
                "HOME": "/Users/personal",
                "GH_TOKEN": "ambient_gh_host",
                "GITHUB_TOKEN": "ambient_github_token",
                "SCM_TOKEN": "scm_token_example",
            },
            scope="scm-test",
        )

        self.assertNotEqual(env["HOME"], "/Users/personal")
        # GIT_CONFIG_GLOBAL points to an isolated gitconfig file (not /dev/null)
        # so that safe.directory=* can be set for Docker bind-mounted workspaces.
        git_cfg = env["GIT_CONFIG_GLOBAL"]
        self.assertTrue(git_cfg.endswith(".gitconfig-isolated"), git_cfg)
        self.assertTrue(os.path.isfile(git_cfg), f"gitconfig not created: {git_cfg}")
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GCM_INTERACTIVE"], "never")
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("SCM_TOKEN", env)

    def test_build_isolated_copilot_env_removes_generic_github_tokens(self):
        env = build_isolated_copilot_env(
            "copilot_token_runtime",
            {
                "HOME": "/Users/personal",
                "GH_TOKEN": "ambient_gh_token",
                "GITHUB_TOKEN": "personal_github_token",
                "SCM_TOKEN": "scm_token_example",
            },
        )

        self.assertEqual(env["COPILOT_GITHUB_TOKEN"], "copilot_token_runtime")
        self.assertNotEqual(env["HOME"], "/Users/personal")
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("SCM_TOKEN", env)
        self.assertTrue(env["COPILOT_HOME"].startswith(env["HOME"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)