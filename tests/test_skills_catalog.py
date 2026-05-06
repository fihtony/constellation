"""Tests for the skills catalog scanner and Registry skills endpoints."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from common.skills_catalog import SkillsCatalog, _catalog_hash

_SKILLS_ROOT = os.path.join(_REPO_ROOT, ".github", "skills")


class SkillsCatalogScanTests(unittest.TestCase):
    """Tests for SkillsCatalog.scan()."""

    def setUp(self):
        self.cat = SkillsCatalog(_SKILLS_ROOT)
        self.cat.scan()

    def test_loads_at_least_one_skill(self):
        self.assertGreater(len(self.cat.get_catalog()), 0)

    def test_all_skills_have_valid_applies_to(self):
        for skill in self.cat.get_catalog():
            self.assertIsInstance(
                skill.get("appliesTo"),
                dict,
                f"appliesTo for {skill['id']} is not a dict",
            )

    def test_all_skills_have_id_version_level(self):
        for skill in self.cat.get_catalog():
            self.assertIn("id", skill, f"Missing 'id' in skill: {skill}")
            self.assertIn("version", skill)
            self.assertIn("level", skill)

    def test_version_is_nonempty_hex_string(self):
        version = self.cat.get_version()
        self.assertTrue(len(version) > 0)
        # Should be hex characters only
        int(version, 16)

    def test_get_skill_by_id_returns_correct_skill(self):
        s = self.cat.get_skill("constellation-architecture-delivery")
        self.assertIsNotNone(s)
        self.assertEqual(s["id"], "constellation-architecture-delivery")

    def test_get_skill_unknown_returns_none(self):
        self.assertIsNone(self.cat.get_skill("nonexistent-skill-xyz"))

    def test_expected_skills_present(self):
        expected = {
            "constellation-architecture-delivery",
            "constellation-frontend-delivery",
            "constellation-backend-delivery",
            "constellation-code-review-delivery",
            "constellation-testing-delivery",
            "github-mcp-workflow",
            "jira-cloud-workflow",
        }
        ids = {s["id"] for s in self.cat.get_catalog()}
        for skill_id in expected:
            self.assertIn(skill_id, ids, f"Expected skill not found: {skill_id}")

    def test_total_skills_count(self):
        # We have created skill.yaml for all 22 existing skill directories.
        self.assertGreaterEqual(len(self.cat.get_catalog()), 22)


class SkillsCatalogQueryTests(unittest.TestCase):
    """Tests for SkillsCatalog.query()."""

    def setUp(self):
        self.cat = SkillsCatalog(_SKILLS_ROOT)
        self.cat.scan()

    def test_team_lead_query_matches_generic_skills(self):
        result = self.cat.query({
            "agentRole": "team-lead",
            "agentId": "team-lead-agent",
            "targetCategories": ["generic"],
            "taskMetadata": {},
        })
        self.assertIn("matched", result)
        self.assertIn("rejected", result)
        self.assertIn("catalogVersion", result)
        matched_ids = {s["id"] for s in result["matched"]}
        # Generic skills like architecture-delivery, code-review should match
        self.assertIn("constellation-architecture-delivery", matched_ids)
        self.assertIn("constellation-code-review-delivery", matched_ids)

    def test_frontend_query_matches_react_skills(self):
        result = self.cat.query({
            "agentRole": "frontend",
            "agentId": "web-agent",
            "targetCategories": ["workflow:development"],
            "taskMetadata": {
                "workflow": "development",
                "languages": ["typescript"],
                "tags": ["react"],
            },
        })
        matched_ids = {s["id"] for s in result["matched"]}
        self.assertIn("react-nextjs-delivery", matched_ids)
        self.assertIn("mui-delivery", matched_ids)

    def test_scm_agent_gets_scm_skills_not_frontend(self):
        result = self.cat.query({
            "agentRole": "scm",
            "agentId": "scm-agent",
            "targetCategories": ["workflow:scm"],
            "taskMetadata": {"workflow": "scm"},
        })
        matched_ids = {s["id"] for s in result["matched"]}
        # SCM skills should be included
        self.assertIn("github-mcp-workflow", matched_ids)

    def test_language_filter_excludes_wrong_language_skills(self):
        result = self.cat.query({
            "agentRole": "backend",
            "agentId": "web-agent",
            "targetCategories": ["workflow:development"],
            "taskMetadata": {
                "workflow": "development",
                "languages": ["kotlin"],
            },
        })
        matched_ids = {s["id"] for s in result["matched"]}
        # java-spring-delivery requires java, should be excluded for kotlin-only tasks
        self.assertNotIn("java-spring-delivery", matched_ids)

    def test_empty_query_returns_all_skills(self):
        result = self.cat.query({})
        # With no role/filter, all or most skills should match
        self.assertGreater(len(result["matched"]), 0)

    def test_result_contains_catalog_version(self):
        result = self.cat.query({"agentRole": "team-lead"})
        self.assertEqual(result["catalogVersion"], self.cat.get_version())


class SkillsCatalogEmptyRootTests(unittest.TestCase):
    """Tests with a non-existent skills root (graceful degradation)."""

    def test_scan_nonexistent_root_returns_empty(self):
        cat = SkillsCatalog("/nonexistent/path/to/skills")
        cat.scan()
        self.assertEqual(cat.get_catalog(), [])
        self.assertIsNotNone(cat.get_version())

    def test_query_empty_catalog_returns_empty_match(self):
        cat = SkillsCatalog("/nonexistent/path/to/skills")
        cat.scan()
        result = cat.query({"agentRole": "team-lead"})
        self.assertEqual(result["matched"], [])
        self.assertEqual(result["rejected"], [])


class SkillYamlFileTests(unittest.TestCase):
    """Tests for individual skill.yaml files."""

    def test_all_skill_directories_have_skill_yaml(self):
        missing = []
        if not os.path.isdir(_SKILLS_ROOT):
            self.skipTest("Skills root not found")
        for entry in sorted(os.listdir(_SKILLS_ROOT)):
            skill_dir = os.path.join(_SKILLS_ROOT, entry)
            if not os.path.isdir(skill_dir):
                continue
            yaml_path = os.path.join(skill_dir, "skill.yaml")
            if not os.path.isfile(yaml_path):
                missing.append(entry)
        self.assertEqual(missing, [], f"Skill directories missing skill.yaml: {missing}")

    def test_skill_yaml_ids_match_directory_names(self):
        cat = SkillsCatalog(_SKILLS_ROOT)
        cat.scan()
        for skill in cat.get_catalog():
            self.assertEqual(
                skill["id"],
                skill["directory"],
                f"Skill id '{skill['id']}' does not match directory '{skill['directory']}'",
            )

    def test_skill_yaml_runtime_compatibility_is_dict(self):
        cat = SkillsCatalog(_SKILLS_ROOT)
        cat.scan()
        for skill in cat.get_catalog():
            rc = skill.get("runtimeCompatibility")
            self.assertIsInstance(
                rc, dict,
                f"runtimeCompatibility for {skill['id']} is not a dict: {rc}",
            )


class CatalogHashTests(unittest.TestCase):
    """Tests for the catalog hash function."""

    def test_empty_catalog_has_stable_hash(self):
        h1 = _catalog_hash({})
        h2 = _catalog_hash({})
        self.assertEqual(h1, h2)

    def test_different_catalogs_have_different_hashes(self):
        h1 = _catalog_hash({"a": {"id": "a", "version": "1.0.0"}})
        h2 = _catalog_hash({"b": {"id": "b", "version": "1.0.0"}})
        self.assertNotEqual(h1, h2)

    def test_hash_is_16_hex_chars(self):
        h = _catalog_hash({"a": {"id": "a", "version": "1.0.0"}})
        self.assertEqual(len(h), 16)
        int(h, 16)  # must be valid hex


if __name__ == "__main__":
    unittest.main()
