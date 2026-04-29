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

    def test_copilot_cli_ignores_generic_github_tokens(self):
        os.environ["AGENT_RUNTIME"] = "copilot-cli"
        os.environ["GH_TOKEN"] = "gho_personal"
        os.environ["GITHUB_TOKEN"] = "github_pat_personal"

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
        self.assertIn("generic GitHub credentials are ignored", result["warnings"][0])

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

    def test_copilot_cli_executes_in_isolated_home(self):
        os.environ["AGENT_RUNTIME"] = "copilot-cli"
        os.environ["COPILOT_GITHUB_TOKEN"] = "github_pat_test"
        os.environ["GH_TOKEN"] = "gho_personal"
        os.environ["GITHUB_TOKEN"] = "github_pat_personal"
        os.environ["HOME"] = "/Users/personal"

        completed = Mock(return_value=Mock(returncode=0, stdout='{"summary":"cli ok"}', stderr=""))
        with patch("common.runtime.copilot_cli.shutil.which", return_value="/usr/bin/copilot"), \
             patch("common.runtime.copilot_cli.subprocess.run", completed):
            get_runtime().run("hello from cli")

        env = completed.call_args.kwargs["env"]
        self.assertEqual(env["COPILOT_GITHUB_TOKEN"], "github_pat_test")
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotEqual(env["HOME"], "/Users/personal")
        self.assertTrue(env["COPILOT_HOME"].startswith(env["HOME"]))
        self.assertTrue(env["GH_CONFIG_DIR"].startswith(env["XDG_CONFIG_HOME"]))

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

    def test_runtime_configuration_summary_reports_resolved_connect_url(self):
        os.environ["AGENT_RUNTIME"] = "copilot-connect"

        with patch("common.runtime.adapter.resolve_openai_base_url", return_value="http://host.rancher-desktop.internal:1288/v1"):
            summary = summarize_runtime_configuration()

        self.assertEqual(summary["effectiveBackend"], "copilot-connect")
        self.assertEqual(summary["resolvedBaseUrl"], "http://host.rancher-desktop.internal:1288/v1")
        self.assertFalse(summary["baseUrlConfigured"])

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

    def test_load_dotenv_ignores_ambient_github_credentials_by_default(self):
        os.environ["GH_TOKEN"] = "gho_host"
        os.environ["GITHUB_TOKEN"] = "github_pat_host"
        os.environ["COPILOT_GITHUB_TOKEN"] = "github_pat_host_copilot"

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

            load_dotenv(agent_env)

        self.assertEqual(os.environ["COPILOT_GITHUB_TOKEN"], "github_pat_from_common")
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
                "GH_TOKEN": "gho_host",
                "GITHUB_TOKEN": "github_pat_host",
                "SCM_TOKEN": "github_pat_scm",
                "PATH": "/usr/bin",
            },
            keep={"COPILOT_GITHUB_TOKEN": "github_pat_runtime"},
        )

        self.assertEqual(env["COPILOT_GITHUB_TOKEN"], "github_pat_runtime")
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("SCM_TOKEN", env)

    def test_build_isolated_git_env_uses_runtime_home(self):
        env = build_isolated_git_env(
            {
                "HOME": "/Users/personal",
                "GH_TOKEN": "gho_host",
                "GITHUB_TOKEN": "ghp_host",
                "SCM_TOKEN": "github_pat_scm",
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
            "github_pat_runtime",
            {
                "HOME": "/Users/personal",
                "GH_TOKEN": "gho_personal",
                "GITHUB_TOKEN": "github_pat_personal",
                "SCM_TOKEN": "github_pat_scm",
            },
        )

        self.assertEqual(env["COPILOT_GITHUB_TOKEN"], "github_pat_runtime")
        self.assertNotEqual(env["HOME"], "/Users/personal")
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("SCM_TOKEN", env)
        self.assertTrue(env["COPILOT_HOME"].startswith(env["HOME"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)