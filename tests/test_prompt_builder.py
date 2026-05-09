"""Tests for the modular prompt builder (Phase 5)."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.prompt_builder import (
    build_system_prompt_from_manifest,
    build_task_prompt,
    _fetch_skill_from_registry,
    _read_manifest_order,
    _read_manifest_include_skills,
)
from common.agent_system_prompt import get_agent_manifest_prompt

_TEAM_LEAD_DIR = os.path.join(_REPO_ROOT, "team-lead")
_SKILLS_ROOT = os.path.join(_REPO_ROOT, ".github", "skills")


class ManifestParsingTests(unittest.TestCase):
    """Tests for manifest.yaml parsing helpers."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmpdir, "prompts", "system"), exist_ok=True)

    def _write_manifest(self, content: str) -> str:
        path = os.path.join(self.tmpdir, "prompts", "system", "manifest.yaml")
        with open(path, "w") as f:
            f.write(content)
        return path

    def test_read_manifest_order_basic(self):
        path = self._write_manifest(
            "systemOrder:\n  - 00-role.md\n  - 10-boundaries.md\nincludeSkills: true\n"
        )
        order = _read_manifest_order(path)
        self.assertEqual(order, ["00-role.md", "10-boundaries.md"])

    def test_read_manifest_include_skills_true(self):
        path = self._write_manifest("systemOrder:\n  - 00-role.md\nincludeSkills: true\n")
        self.assertTrue(_read_manifest_include_skills(path))

    def test_read_manifest_include_skills_false(self):
        path = self._write_manifest("systemOrder:\n  - 00-role.md\nincludeSkills: false\n")
        self.assertFalse(_read_manifest_include_skills(path))

    def test_read_manifest_include_skills_default_false(self):
        path = self._write_manifest("systemOrder:\n  - 00-role.md\n")
        self.assertFalse(_read_manifest_include_skills(path))

    def test_read_manifest_order_empty(self):
        path = self._write_manifest("systemOrder:\n")
        order = _read_manifest_order(path)
        self.assertEqual(order, [])

    def test_read_manifest_nonexistent_file(self):
        order = _read_manifest_order("/nonexistent/manifest.yaml")
        self.assertEqual(order, [])


class PromptBuilderTests(unittest.TestCase):
    """Tests for build_system_prompt_from_manifest."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        sys_dir = os.path.join(self.tmpdir, "prompts", "system")
        os.makedirs(sys_dir, exist_ok=True)

        # Write manifest
        with open(os.path.join(sys_dir, "manifest.yaml"), "w") as f:
            f.write("systemOrder:\n  - 00-role.md\n  - 10-boundaries.md\nincludeSkills: false\n")

        # Write section files
        with open(os.path.join(sys_dir, "00-role.md"), "w") as f:
            f.write("# Role\nYou are an agent.")

        with open(os.path.join(sys_dir, "10-boundaries.md"), "w") as f:
            f.write("# Boundaries\nDo not do bad things.")

    def test_builds_prompt_with_separator(self):
        result = build_system_prompt_from_manifest(self.tmpdir)
        self.assertIn("# Role", result)
        self.assertIn("# Boundaries", result)
        self.assertIn("---", result)

    def test_prompt_order_preserved(self):
        result = build_system_prompt_from_manifest(self.tmpdir)
        role_pos = result.find("# Role")
        bounds_pos = result.find("# Boundaries")
        self.assertLess(role_pos, bounds_pos)

    def test_missing_file_skipped_gracefully(self):
        # Remove 10-boundaries.md to test graceful skip
        os.remove(os.path.join(self.tmpdir, "prompts", "system", "10-boundaries.md"))
        result = build_system_prompt_from_manifest(self.tmpdir)
        self.assertIn("# Role", result)
        self.assertNotIn("# Boundaries", result)

    def test_missing_manifest_returns_empty_string(self):
        import tempfile
        empty_dir = tempfile.mkdtemp()
        result = build_system_prompt_from_manifest(empty_dir)
        self.assertEqual(result, "")

    def test_include_skills_appends_skill_content(self):
        # Enable includeSkills and point to a temp skills root
        sys_dir = os.path.join(self.tmpdir, "prompts", "system")
        with open(os.path.join(sys_dir, "manifest.yaml"), "w") as f:
            f.write("systemOrder:\n  - 00-role.md\nincludeSkills: true\n")

        skills_root = tempfile.mkdtemp()
        skill_dir = os.path.join(skills_root, "my-skill")
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, "SKILL.md"), "w") as f:
            f.write("# My Skill\nDo the thing.")

        result = build_system_prompt_from_manifest(
            self.tmpdir,
            skill_names=["my-skill"],
            skills_root=skills_root,
        )
        self.assertIn("## Skill: my-skill", result)
        self.assertIn("Do the thing.", result)

    def test_include_skills_strips_frontmatter(self):
        sys_dir = os.path.join(self.tmpdir, "prompts", "system")
        with open(os.path.join(sys_dir, "manifest.yaml"), "w") as f:
            f.write("systemOrder:\n  - 00-role.md\nincludeSkills: true\n")

        skills_root = tempfile.mkdtemp()
        skill_dir = os.path.join(skills_root, "fenced-skill")
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, "SKILL.md"), "w") as f:
            f.write("---\nname: fenced-skill\n---\n# Fenced Content\nHello.")

        result = build_system_prompt_from_manifest(
            self.tmpdir,
            skill_names=["fenced-skill"],
            skills_root=skills_root,
        )
        self.assertNotIn("name: fenced-skill", result)
        self.assertIn("# Fenced Content", result)

    def test_include_skills_prefers_registry_content_when_available(self):
        sys_dir = os.path.join(self.tmpdir, "prompts", "system")
        with open(os.path.join(sys_dir, "manifest.yaml"), "w") as f:
            f.write("systemOrder:\n  - 00-role.md\nincludeSkills: true\n")

        skills_root = tempfile.mkdtemp()
        skill_dir = os.path.join(skills_root, "my-skill")
        os.makedirs(skill_dir)
        with open(os.path.join(skill_dir, "SKILL.md"), "w") as f:
            f.write("# Local Skill\nUse the local copy.")

        with mock.patch(
            "common.prompt_builder._fetch_skill_from_registry",
            return_value="# Registry Skill\nUse the registry copy.",
        ):
            result = build_system_prompt_from_manifest(
                self.tmpdir,
                skill_names=["my-skill"],
                skills_root=skills_root,
                registry_url="http://registry:9000",
            )

        self.assertIn("Registry Skill", result)
        self.assertIn("Use the registry copy.", result)
        self.assertNotIn("Use the local copy.", result)

    def test_fetch_skill_from_registry_returns_empty_when_unconfigured(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_fetch_skill_from_registry("my-skill"), "")


class AgentSystemPromptTests(unittest.TestCase):
    def test_get_agent_manifest_prompt_rebuilds_each_call(self):
        fake_agent_file = os.path.join(self.tmpdir if hasattr(self, "tmpdir") else tempfile.mkdtemp(), "agent", "app.py")
        with mock.patch(
            "common.agent_system_prompt.build_system_prompt_from_manifest",
            side_effect=["first", "second"],
        ):
            self.assertEqual(get_agent_manifest_prompt(fake_agent_file, agent_name="agent"), "first")
            self.assertEqual(get_agent_manifest_prompt(fake_agent_file, agent_name="agent"), "second")


class BuildTaskPromptTests(unittest.TestCase):
    """Tests for build_task_prompt."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.tmpdir, "prompts", "tasks"), exist_ok=True)
        with open(os.path.join(self.tmpdir, "prompts", "tasks", "intake.md"), "w") as f:
            f.write("# Intake\nGather context first.")

    def test_loads_task_prompt(self):
        result = build_task_prompt(self.tmpdir, "intake")
        self.assertIn("Gather context first.", result)

    def test_missing_task_prompt_returns_empty(self):
        result = build_task_prompt(self.tmpdir, "nonexistent")
        self.assertEqual(result, "")


class TeamLeadPromptFilesTests(unittest.TestCase):
    """Integration tests for the team-lead prompts/ directory."""

    def test_manifest_exists(self):
        manifest = os.path.join(_TEAM_LEAD_DIR, "prompts", "system", "manifest.yaml")
        self.assertTrue(os.path.isfile(manifest), f"Missing manifest.yaml at {manifest}")

    def test_all_manifest_files_exist(self):
        manifest = os.path.join(_TEAM_LEAD_DIR, "prompts", "system", "manifest.yaml")
        if not os.path.isfile(manifest):
            self.skipTest("manifest.yaml not found")
        order = _read_manifest_order(manifest)
        self.assertGreater(len(order), 0, "manifest.yaml systemOrder is empty")
        sys_dir = os.path.join(_TEAM_LEAD_DIR, "prompts", "system")
        for filename in order:
            fpath = os.path.join(sys_dir, filename)
            self.assertTrue(os.path.isfile(fpath), f"Listed in manifest but missing: {fpath}")

    def test_build_team_lead_system_prompt(self):
        result = build_system_prompt_from_manifest(_TEAM_LEAD_DIR)
        self.assertGreater(len(result), 100, "System prompt appears too short")
        # Key phrases from the modular files
        self.assertIn("Team Lead Agent", result)
        self.assertIn("Boundaries", result)

    def test_build_team_lead_system_prompt_with_skills(self):
        result = build_system_prompt_from_manifest(
            _TEAM_LEAD_DIR,
            skill_names=["constellation-architecture-delivery"],
            skills_root=_SKILLS_ROOT,
        )
        self.assertIn("## Skill: constellation-architecture-delivery", result)

    def test_task_prompts_exist(self):
        for task in ("intake", "review", "revision"):
            path = os.path.join(_TEAM_LEAD_DIR, "prompts", "tasks", f"{task}.md")
            self.assertTrue(os.path.isFile(path) if False else os.path.isfile(path),
                            f"Missing task prompt: {path}")

    def test_build_intake_task_prompt(self):
        result = build_task_prompt(_TEAM_LEAD_DIR, "intake")
        self.assertGreater(len(result), 50)
        self.assertIn("platform", result)

    def test_build_review_task_prompt(self):
        result = build_task_prompt(_TEAM_LEAD_DIR, "review")
        self.assertGreater(len(result), 50)
        self.assertIn("prUrl", result)

    def test_build_revision_task_prompt(self):
        result = build_task_prompt(_TEAM_LEAD_DIR, "revision")
        self.assertGreater(len(result), 50)
        self.assertIn("revision", result.lower())


if __name__ == "__main__":
    unittest.main()
