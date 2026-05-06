#!/usr/bin/env python3
"""Static guardrails ensuring agent files use the unified runtime abstraction."""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMMON_ENV_PATH = os.path.join(PROJECT_ROOT, "common", ".env")
AGENT_FILES = [
    os.path.join(PROJECT_ROOT, "team-lead", "app.py"),
    os.path.join(PROJECT_ROOT, "web", "app.py"),
    os.path.join(PROJECT_ROOT, "jira", "app.py"),
    os.path.join(PROJECT_ROOT, "scm", "app.py"),
    os.path.join(PROJECT_ROOT, "ui-design", "app.py"),
]
PER_TASK_REGISTRY_CONFIGS = [
    os.path.join(PROJECT_ROOT, "team-lead", "registry-config.json"),
    os.path.join(PROJECT_ROOT, "web", "registry-config.json"),
    os.path.join(PROJECT_ROOT, "android", "registry-config.json"),
]
AGENT_DOCKERFILES = [
    os.path.join(PROJECT_ROOT, "compass", "Dockerfile"),
    os.path.join(PROJECT_ROOT, "team-lead", "Dockerfile"),
    os.path.join(PROJECT_ROOT, "web", "Dockerfile"),
    os.path.join(PROJECT_ROOT, "jira", "Dockerfile"),
    os.path.join(PROJECT_ROOT, "scm", "Dockerfile"),
    os.path.join(PROJECT_ROOT, "ui-design", "Dockerfile"),
    os.path.join(PROJECT_ROOT, "office", "Dockerfile"),
    os.path.join(PROJECT_ROOT, "android", "Dockerfile"),
]
SKILL_DOCKERFILES = [
    os.path.join(PROJECT_ROOT, "team-lead", "Dockerfile"),
    os.path.join(PROJECT_ROOT, "web", "Dockerfile"),
]


class AgentRuntimeAdoptionTests(unittest.TestCase):
    def _read_env_file(self, path):
        values = {}
        for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
        return values

    def test_agents_no_longer_import_generate_text_directly(self):
        for path in AGENT_FILES:
            with self.subTest(path=path):
                content = Path(path).read_text(encoding="utf-8")
                self.assertNotIn("from common.llm_client import generate_text", content)

    def test_agents_import_runtime_adapter(self):
        for path in AGENT_FILES:
            with self.subTest(path=path):
                content = Path(path).read_text(encoding="utf-8")
                self.assertIn("from common.runtime.adapter import get_runtime", content)

    def test_common_env_uses_connect_agent_with_gpt5_mini_model(self):
        env = self._read_env_file(COMMON_ENV_PATH)
        self.assertEqual(env.get("AGENT_RUNTIME"), "connect-agent")
        self.assertEqual(env.get("COPILOT_MODEL"), "gpt-5-mini")

    def test_per_task_agents_inherit_shared_runtime_defaults(self):
        for path in PER_TASK_REGISTRY_CONFIGS:
            with self.subTest(path=path):
                payload = json.loads(Path(path).read_text(encoding="utf-8"))
                env = payload.get("launchSpec", {}).get("env", {})
                self.assertNotIn("AGENT_RUNTIME", env)
                self.assertNotIn("AGENT_MODEL", env)

    def test_team_lead_and_web_images_copy_workspace_skills(self):
        for path in SKILL_DOCKERFILES:
            with self.subTest(path=path):
                content = Path(path).read_text(encoding="utf-8")
                self.assertIn("COPY .github/skills/ /app/.github/skills/", content)

    def test_llm_enabled_agent_images_install_copilot_cli(self):
        for path in AGENT_DOCKERFILES:
            with self.subTest(path=path):
                content = Path(path).read_text(encoding="utf-8")
                self.assertIn("npm install -g @github/copilot", content)

    def test_agents_import_configure_control_tools(self):
        all_agent_files = AGENT_FILES + [
            os.path.join(PROJECT_ROOT, "compass", "app.py"),
            os.path.join(PROJECT_ROOT, "android", "app.py"),
            os.path.join(PROJECT_ROOT, "office", "app.py"),
        ]
        for path in all_agent_files:
            with self.subTest(path=path):
                content = Path(path).read_text(encoding="utf-8")
                self.assertIn("configure_control_tools", content,
                              f"{path} must call configure_control_tools in its task workflow")

    def test_agents_use_manifest_system_prompt(self):
        all_agent_files = AGENT_FILES + [
            os.path.join(PROJECT_ROOT, "compass", "app.py"),
            os.path.join(PROJECT_ROOT, "android", "app.py"),
            os.path.join(PROJECT_ROOT, "office", "app.py"),
        ]
        for path in all_agent_files:
            with self.subTest(path=path):
                content = Path(path).read_text(encoding="utf-8")
                self.assertTrue(
                    "build_system_prompt_from_manifest" in content or "_build_manifest_prompt" in content,
                    f"{path} must use manifest-based system prompt loading"
                )

    def test_execution_agents_use_run_agentic_for_implementation(self):
        for path in [
            os.path.join(PROJECT_ROOT, "web", "app.py"),
            os.path.join(PROJECT_ROOT, "android", "app.py"),
        ]:
            with self.subTest(path=path):
                content = Path(path).read_text(encoding="utf-8")
                self.assertIn(
                    "run_agentic(",
                    content,
                    f"{path} must invoke runtime.run_agentic() for execution-stage autonomy",
                )

    def test_agent_manifests_exist_and_have_agent_id(self):
        agent_dirs = ["team-lead", "web", "jira", "scm", "ui-design", "office", "android", "compass"]
        for agent in agent_dirs:
            manifest_path = os.path.join(PROJECT_ROOT, agent, "prompts", "system", "manifest.yaml")
            with self.subTest(agent=agent):
                self.assertTrue(os.path.isfile(manifest_path),
                                f"{agent} must have a prompts/system/manifest.yaml")
                content = Path(manifest_path).read_text(encoding="utf-8")
                self.assertIn("systemOrder:", content,
                              f"{agent}/prompts/system/manifest.yaml must have systemOrder field")


if __name__ == "__main__":
    unittest.main(verbosity=2)