#!/usr/bin/env python3
"""Focused tests for credential isolation in launchers and test helpers."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_ROOT = os.path.join(PROJECT_ROOT, "tests")
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
if TESTS_ROOT not in sys.path:
    sys.path.insert(0, TESTS_ROOT)

from agent_test_support import build_test_subprocess_env
from common.launcher import Launcher
from common.launcher_rancher import RancherLauncher


class TestSubprocessEnvIsolation(unittest.TestCase):
    def test_build_test_subprocess_env_strips_host_github_credentials(self):
        with patch.dict(
            os.environ,
            {
                "PATH": "/usr/bin",
                "GH_TOKEN": "gho_host",
                "GITHUB_TOKEN": "github_pat_host",
                "COPILOT_GITHUB_TOKEN": "github_pat_host_copilot",
            },
            clear=True,
        ):
            env = build_test_subprocess_env({"PYTHONPATH": PROJECT_ROOT})

        self.assertEqual(env["PYTHONPATH"], PROJECT_ROOT)
        self.assertEqual(env["PATH"], "/usr/bin")
        self.assertNotIn("GH_TOKEN", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("COPILOT_GITHUB_TOKEN", env)
        self.assertNotIn("CONSTELLATION_TRUSTED_ENV", env)

    def test_build_test_subprocess_env_marks_explicit_test_credentials_trusted(self):
        with patch.dict(os.environ, {"PATH": "/usr/bin", "GH_TOKEN": "gho_host"}, clear=True):
            env = build_test_subprocess_env({"SCM_TOKEN": "github_pat_from_tests"}, trusted=True)

        self.assertEqual(env["SCM_TOKEN"], "github_pat_from_tests")
        self.assertEqual(env["CONSTELLATION_TRUSTED_ENV"], "1")
        self.assertNotIn("GH_TOKEN", env)


class TestDockerLauncherEnvIsolation(unittest.TestCase):
    def test_launcher_marks_child_env_trusted_and_uses_file_backed_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            common_dir = os.path.join(temp_dir, "common")
            agent_dir = os.path.join(temp_dir, "agent")
            os.makedirs(common_dir, exist_ok=True)
            os.makedirs(agent_dir, exist_ok=True)

            with open(os.path.join(common_dir, ".env"), "w", encoding="utf-8") as handle:
                handle.write("COPILOT_GITHUB_TOKEN=github_pat_from_common\n")
            with open(os.path.join(agent_dir, ".env"), "w", encoding="utf-8") as handle:
                handle.write("OPENAI_MODEL=gpt-5-mini\n")

            agent_definition = {
                "agent_id": "web-agent",
                "display_name": "Web Agent",
                "execution_mode": "per-task",
                "launch_spec": {
                    "image": "constellation-web-agent:latest",
                    "port": 8050,
                    "envFile": os.path.join(agent_dir, ".env"),
                    "passThroughEnv": ["COPILOT_GITHUB_TOKEN"],
                    "mountDockerSocket": False,
                    "startupDelaySeconds": 0,
                },
            }

            requests: list[tuple[str, str, dict | None]] = []

            def fake_request(method, path, payload=None):
                requests.append((method, path, payload))
                return {}

            with patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "github_pat_host"}, clear=True):
                launcher = Launcher()
                with patch.object(launcher, "_request", side_effect=fake_request), \
                     patch("common.launcher.time.sleep", return_value=None):
                    launcher.launch_instance(agent_definition, "task-123")

            payload = requests[0][2]
            env_list = payload["Env"]
            self.assertIn("CONSTELLATION_TRUSTED_ENV=1", env_list)
            self.assertIn("COPILOT_GITHUB_TOKEN=github_pat_from_common", env_list)
            self.assertNotIn("COPILOT_GITHUB_TOKEN=github_pat_host", env_list)

    def test_launcher_appends_extra_binds(self):
        agent_definition = {
            "agent_id": "office-agent",
            "display_name": "Office Agent",
            "execution_mode": "per-task",
            "launch_spec": {
                "image": "constellation-office-agent:latest",
                "port": 8060,
                "mountDockerSocket": False,
                "startupDelaySeconds": 0,
                "extraBinds": [
                    "/Users/test/Documents:/app/userdata:ro",
                    "/tmp/workspace:/app/workspace:rw",
                ],
            },
        }
        requests: list[tuple[str, str, dict | None]] = []

        def fake_request(method, path, payload=None):
            requests.append((method, path, payload))
            return {}

        with patch.dict(os.environ, {}, clear=True):
            launcher = Launcher()
            with patch.object(launcher, "_request", side_effect=fake_request), \
                 patch("common.launcher.time.sleep", return_value=None):
                launcher.launch_instance(agent_definition, "task-123")

        binds = requests[0][2]["HostConfig"]["Binds"]
        self.assertIn("/Users/test/Documents:/app/userdata:ro", binds)
        self.assertIn("/tmp/workspace:/app/workspace:rw", binds)

    def test_launcher_uses_host_socket_source_for_nested_agents(self):
        agent_definition = {
            "agent_id": "team-lead-agent",
            "display_name": "Team Lead Agent",
            "execution_mode": "per-task",
            "launch_spec": {
                "image": "constellation-team-lead-agent:latest",
                "port": 8030,
                "mountDockerSocket": True,
                "startupDelaySeconds": 0,
            },
        }
        requests: list[tuple[str, str, dict | None]] = []

        def fake_request(method, path, payload=None):
            requests.append((method, path, payload))
            return {}

        def fake_discover(path):
            if path == "/app/artifacts":
                return "/host/artifacts"
            if path == "/var/run/docker.sock":
                return "/host/docker.sock"
            return path

        with patch.dict(os.environ, {}, clear=True):
            launcher = Launcher()
            with patch.object(launcher, "_discover_host_source", side_effect=fake_discover), \
                 patch.object(launcher, "_request", side_effect=fake_request), \
                 patch("common.launcher.os.path.exists", side_effect=lambda value: value == "/var/run/docker.sock"), \
                 patch("common.launcher.os.stat", return_value=SimpleNamespace(st_gid=777)), \
                 patch("common.launcher.time.sleep", return_value=None):
                launcher.launch_instance(agent_definition, "task-123")

        payload = requests[0][2]
        env_list = payload["Env"]
        binds = payload["HostConfig"]["Binds"]
        self.assertIn("DOCKER_SOCKET=/var/run/docker.sock", env_list)
        self.assertIn("/host/artifacts:/app/artifacts", binds)
        self.assertIn("/host/docker.sock:/var/run/docker.sock", binds)
        self.assertEqual(payload["HostConfig"]["GroupAdd"], ["777"])


class TestRancherLauncherEnvIsolation(unittest.TestCase):
    def test_rancher_launcher_uses_container_socket_path_inside_containers(self):
        with patch.dict(os.environ, {}, clear=True), \
             patch("common.launcher_rancher.os.path.exists", side_effect=lambda value: value == "/.dockerenv"):
            launcher = RancherLauncher()

        self.assertEqual(launcher.socket_path, "/var/run/docker.sock")

    def test_launcher_marks_child_env_trusted_and_uses_file_backed_token(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            common_dir = os.path.join(temp_dir, "common")
            agent_dir = os.path.join(temp_dir, "agent")
            os.makedirs(common_dir, exist_ok=True)
            os.makedirs(agent_dir, exist_ok=True)

            with open(os.path.join(common_dir, ".env"), "w", encoding="utf-8") as handle:
                handle.write("COPILOT_GITHUB_TOKEN=github_pat_from_common\n")
            with open(os.path.join(agent_dir, ".env"), "w", encoding="utf-8") as handle:
                handle.write("OPENAI_MODEL=gpt-5-mini\n")

            agent_definition = {
                "agent_id": "web-agent",
                "display_name": "Web Agent",
                "execution_mode": "per-task",
                "launch_spec": {
                    "image": "constellation-web-agent:latest",
                    "port": 8050,
                    "envFile": os.path.join(agent_dir, ".env"),
                    "passThroughEnv": ["COPILOT_GITHUB_TOKEN"],
                    "mountDockerSocket": False,
                    "startupDelaySeconds": 0,
                },
            }

            requests: list[tuple[str, str, dict | None]] = []

            def fake_request(method, path, payload=None):
                requests.append((method, path, payload))
                return {}

            with patch.dict(os.environ, {"COPILOT_GITHUB_TOKEN": "github_pat_host"}, clear=True):
                launcher = RancherLauncher()
                with patch.object(launcher, "_request", side_effect=fake_request), \
                     patch("common.launcher_rancher.time.sleep", return_value=None):
                    launcher.launch_instance(agent_definition, "task-123")

            payload = requests[0][2]
            env_list = payload["Env"]
            self.assertIn("CONSTELLATION_TRUSTED_ENV=1", env_list)
            self.assertIn("COPILOT_GITHUB_TOKEN=github_pat_from_common", env_list)
            self.assertNotIn("COPILOT_GITHUB_TOKEN=github_pat_host", env_list)

    def test_launcher_appends_extra_binds(self):
        agent_definition = {
            "agent_id": "office-agent",
            "display_name": "Office Agent",
            "execution_mode": "per-task",
            "launch_spec": {
                "image": "constellation-office-agent:latest",
                "port": 8060,
                "mountDockerSocket": False,
                "startupDelaySeconds": 0,
                "extraBinds": [
                    "/Users/test/Documents:/app/userdata:ro",
                    "/tmp/workspace:/app/workspace:rw",
                ],
            },
        }
        requests: list[tuple[str, str, dict | None]] = []

        def fake_request(method, path, payload=None):
            requests.append((method, path, payload))
            return {}

        with patch.dict(os.environ, {}, clear=True):
            launcher = RancherLauncher()
            with patch.object(launcher, "_request", side_effect=fake_request), \
                 patch("common.launcher_rancher.time.sleep", return_value=None):
                launcher.launch_instance(agent_definition, "task-123")

        binds = requests[0][2]["HostConfig"]["Binds"]
        self.assertIn("/Users/test/Documents:/app/userdata:ro", binds)
        self.assertIn("/tmp/workspace:/app/workspace:rw", binds)

    def test_rancher_launcher_uses_host_socket_source_for_nested_agents(self):
        agent_definition = {
            "agent_id": "team-lead-agent",
            "display_name": "Team Lead Agent",
            "execution_mode": "per-task",
            "launch_spec": {
                "image": "constellation-team-lead-agent:latest",
                "port": 8030,
                "mountDockerSocket": True,
                "startupDelaySeconds": 0,
            },
        }
        requests: list[tuple[str, str, dict | None]] = []

        def fake_request(method, path, payload=None):
            requests.append((method, path, payload))
            return {}

        def fake_discover(path):
            if path == "/app/artifacts":
                return "/host/artifacts"
            if path == "/var/run/docker.sock":
                return "/host/rancher.sock"
            return path

        with patch.dict(os.environ, {"DOCKER_SOCKET": "/var/run/docker.sock"}, clear=True):
            launcher = RancherLauncher()
            with patch.object(launcher, "_discover_host_source", side_effect=fake_discover), \
                 patch.object(launcher, "_request", side_effect=fake_request), \
                 patch("common.launcher_rancher.os.path.exists", side_effect=lambda value: value == "/var/run/docker.sock"), \
                 patch("common.launcher_rancher.os.stat", return_value=SimpleNamespace(st_gid=888)), \
                 patch("common.launcher_rancher.time.sleep", return_value=None):
                launcher.launch_instance(agent_definition, "task-123")

        payload = requests[0][2]
        env_list = payload["Env"]
        binds = payload["HostConfig"]["Binds"]
        self.assertIn("DOCKER_SOCKET=/var/run/docker.sock", env_list)
        self.assertIn("/host/artifacts:/app/artifacts", binds)
        self.assertIn("/host/rancher.sock:/var/run/docker.sock", binds)
        self.assertEqual(payload["HostConfig"]["GroupAdd"], ["888"])


if __name__ == "__main__":
    unittest.main(verbosity=2)