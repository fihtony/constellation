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
                self.assertRegex(
                    content,
                    r"from common\.runtime\.adapter import [^\n]*\bget_runtime\b",
                )

    def test_common_env_uses_connect_agent_with_gpt5_mini_model(self):
        env = self._read_env_file(COMMON_ENV_PATH)
        self.assertEqual(env.get("AGENT_RUNTIME"), "connect-agent")
        self.assertEqual(env.get("AGENT_MODEL"), "gpt-5-mini")

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
        # Agents may call configure_control_tools directly or via a named wrapper
        # (e.g. configure_team_lead_control_tools, configure_web_agent_control_tools,
        #  or run_compass_workflow from common modules)
        valid_patterns = ["configure_control_tools", "configure_team_lead_control_tools",
                          "configure_web_agent_control_tools",
                          "configure_office_control_tools",
                          "team_lead_agentic_workflow", "web_agentic_workflow",
                          "office_agentic_workflow",
                          "run_compass_workflow", "compass_agentic_workflow"]
        for path in all_agent_files:
            with self.subTest(path=path):
                content = Path(path).read_text(encoding="utf-8")
                self.assertTrue(
                    any(pattern in content for pattern in valid_patterns),
                    f"{path} must call configure_control_tools (or a named wrapper) in its task workflow"
                )

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
            os.path.join(PROJECT_ROOT, "office", "app.py"),
        ]:
            with self.subTest(path=path):
                content = Path(path).read_text(encoding="utf-8")
                self.assertIn(
                    "run_agentic(",
                    content,
                    f"{path} must invoke runtime.run_agentic() for execution-stage autonomy",
                )

    def test_web_uses_task_prompt_file_instead_of_inline_workflow_markdown(self):
        web_app = Path(PROJECT_ROOT, "web", "app.py").read_text(encoding="utf-8")
        # Accept build_task_prompt (direct call) or build_web_task_prompt (via common helper)
        self.assertTrue(
            "build_task_prompt" in web_app or "build_web_task_prompt" in web_app,
            "web/app.py must delegate task prompt construction to a helper (build_task_prompt or build_web_task_prompt)",
        )
        self.assertNotIn("## Your Workflow (follow this order)", web_app)

    def test_office_uses_task_prompt_file_instead_of_inline_workflow_markdown(self):
        office_app = Path(PROJECT_ROOT, "office", "app.py").read_text(encoding="utf-8")
        self.assertTrue(
            "build_office_task_prompt" in office_app,
            "office/app.py must delegate task prompt construction to build_office_task_prompt",
        )
        # Old Python-branched capability prompt builder must be gone
        self.assertNotIn("_build_office_task_prompt", office_app)

    def test_office_task_prompt_uses_canonical_workspace_tools(self):
        prompt = Path(PROJECT_ROOT, "office", "prompts", "tasks", "process.md").read_text(encoding="utf-8")
        for token in (
            "read_local_file",
            "write_local_file",
            "list_local_dir",
            "search_local_files",
            "run_local_command",
        ):
            self.assertIn(token, prompt, f"office process.md must reference {token}")
        # Legacy aliases must not appear as primary instruction
        self.assertNotIn("`read_file`", prompt)
        self.assertNotIn("`write_file`", prompt)
        self.assertNotIn("`glob`", prompt)
        self.assertNotIn("`list_dir`", prompt)

    def test_web_task_prompt_uses_canonical_workspace_and_scm_tools(self):
        prompt = Path(PROJECT_ROOT, "web", "prompts", "tasks", "implement.md").read_text(encoding="utf-8")
        for token in (
            "scm_clone_repo",
            "get_task_context",
            "read_local_file",
            "write_local_file",
            "edit_local_file",
            "run_local_command",
        ):
            self.assertIn(token, prompt)
        self.assertNotIn("use bash to clone", prompt.lower())

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