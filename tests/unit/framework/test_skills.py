"""Tests for framework.skills — Skills registry."""
import os
import tempfile

import pytest
import yaml

from framework.skills import SkillsRegistry


def _create_skill(base_dir, skill_id, name, tags=None, priority=0, instructions=""):
    """Helper to create a skill directory with skill.yaml and instructions.md."""
    skill_dir = os.path.join(base_dir, skill_id)
    os.makedirs(skill_dir, exist_ok=True)

    meta = {
        "id": skill_id,
        "name": name,
        "description": f"Test skill: {name}",
        "version": "1.0.0",
        "tags": tags or [],
        "priority": priority,
    }
    with open(os.path.join(skill_dir, "skill.yaml"), "w") as f:
        yaml.dump(meta, f)

    if instructions:
        with open(os.path.join(skill_dir, "instructions.md"), "w") as f:
            f.write(instructions)


class TestSkillsRegistry:

    @pytest.fixture
    def skills_dir(self, tmp_path):
        d = str(tmp_path / "skills")
        os.makedirs(d)
        return d

    def test_load_skill_from_directory(self, skills_dir):
        _create_skill(skills_dir, "react", "React Dev", tags=["frontend"])
        registry = SkillsRegistry(skills_dir)
        registry.load_all()

        skill = registry.get("react")
        assert skill is not None
        assert skill.name == "React Dev"

    def test_find_by_tags(self, skills_dir):
        _create_skill(skills_dir, "react", "React", tags=["frontend", "react"])
        _create_skill(skills_dir, "django", "Django", tags=["backend", "python"])
        registry = SkillsRegistry(skills_dir)
        registry.load_all()

        results = registry.find_by_tags(["frontend"])
        assert len(results) == 1
        assert results[0].id == "react"

    def test_build_prompt_context(self, skills_dir):
        _create_skill(skills_dir, "react", "React", instructions="Use React 18.")
        _create_skill(skills_dir, "testing", "Testing", instructions="Write tests.")
        registry = SkillsRegistry(skills_dir)
        registry.load_all()

        context = registry.build_prompt_context(["react", "testing"])
        assert "React" in context
        assert "Write tests." in context

    def test_priority_ordering(self, skills_dir):
        _create_skill(skills_dir, "low", "Low", priority=1, instructions="Low priority.")
        _create_skill(skills_dir, "high", "High", priority=10, instructions="High priority.")
        registry = SkillsRegistry(skills_dir)
        registry.load_all()

        context = registry.build_prompt_context(["low", "high"])
        # High priority should appear first
        assert context.index("High") < context.index("Low")

    def test_reload_skills(self, skills_dir):
        _create_skill(skills_dir, "react", "React v1")
        registry = SkillsRegistry(skills_dir)
        registry.load_all()
        assert registry.get("react").name == "React v1"

        # Modify and reload
        _create_skill(skills_dir, "react", "React v2")
        registry.reload()
        assert registry.get("react").name == "React v2"

    def test_missing_instructions_md(self, skills_dir):
        _create_skill(skills_dir, "bare", "Bare Skill")
        registry = SkillsRegistry(skills_dir)
        registry.load_all()

        skill = registry.get("bare")
        assert skill.instructions == ""

    def test_invalid_yaml_skipped(self, skills_dir):
        bad_dir = os.path.join(skills_dir, "bad")
        os.makedirs(bad_dir)
        with open(os.path.join(bad_dir, "skill.yaml"), "w") as f:
            f.write(":::invalid yaml{{{")

        registry = SkillsRegistry(skills_dir)
        registry.load_all()
        assert registry.get("bad") is None

    def test_list_all(self, skills_dir):
        _create_skill(skills_dir, "a", "A")
        _create_skill(skills_dir, "b", "B")
        registry = SkillsRegistry(skills_dir)
        registry.load_all()

        assert len(registry.list_all()) == 2
