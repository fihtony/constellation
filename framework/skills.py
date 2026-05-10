"""Hot-loadable skills framework (inspired by ADK Skills).

A Skill is a YAML metadata file + Markdown instructions file that provides
domain-specific knowledge to agents at runtime.  Skills can be loaded from
a directory, searched by tags, and composed into prompt context.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)


@dataclass
class Skill:
    """A loaded skill definition."""

    id: str                             # e.g. "react-nextjs"
    name: str                           # e.g. "React & Next.js Development"
    description: str = ""
    version: str = "1.0.0"
    tags: list[str] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    instructions: str = ""             # Markdown content (ReAct format)
    assets: dict = field(default_factory=dict)  # path → content
    priority: int = 0                  # Higher = override lower


class SkillsRegistry:
    """Discovers, loads, and serves Skills from a directory tree.

    Expected layout::

        skills/
          react-nextjs/
            skill.yaml
            instructions.md
            assets/
              ...
          testing/
            skill.yaml
            instructions.md
    """

    def __init__(self, skills_dir: str = "skills") -> None:
        self._skills: dict[str, Skill] = {}
        self._skills_dir = skills_dir

    def load_all(self) -> None:
        """Scan the skills directory and load every valid skill."""
        if not os.path.isdir(self._skills_dir):
            logger.warning("Skills directory not found: %s", self._skills_dir)
            return
        for name in sorted(os.listdir(self._skills_dir)):
            skill_dir = os.path.join(self._skills_dir, name)
            if os.path.isdir(skill_dir):
                self._load_skill(skill_dir)

    def _load_skill(self, skill_dir: str) -> None:
        """Load a single skill from its directory."""
        yaml_path = os.path.join(skill_dir, "skill.yaml")
        if not os.path.isfile(yaml_path):
            return
        try:
            with open(yaml_path, encoding="utf-8") as f:
                meta = yaml.safe_load(f)
        except Exception:
            logger.warning("Invalid YAML in %s — skipping", yaml_path, exc_info=True)
            return

        if not meta or "id" not in meta or "name" not in meta:
            logger.warning("Missing required fields in %s — skipping", yaml_path)
            return

        instructions = ""
        instr_path = os.path.join(skill_dir, "instructions.md")
        if os.path.isfile(instr_path):
            with open(instr_path, encoding="utf-8") as f:
                instructions = f.read()

        skill = Skill(
            id=meta["id"],
            name=meta["name"],
            description=meta.get("description", ""),
            version=meta.get("version", "1.0.0"),
            tags=meta.get("tags", []),
            allowed_tools=meta.get("allowed_tools", []),
            instructions=instructions,
            priority=meta.get("priority", 0),
        )
        self._skills[skill.id] = skill
        logger.debug("Loaded skill: %s", skill.id)

    def get(self, skill_id: str) -> Skill | None:
        """Return a skill by ID, or None if not found."""
        return self._skills.get(skill_id)

    def list_all(self) -> list[Skill]:
        """Return all loaded skills."""
        return list(self._skills.values())

    def find_by_tags(self, tags: list[str]) -> list[Skill]:
        """Find skills matching any of the given tags."""
        tag_set = set(tags)
        return [s for s in self._skills.values() if tag_set & set(s.tags)]

    def build_prompt_context(self, skill_ids: list[str]) -> str:
        """Combine instructions from selected skills into a single prompt context.

        Skills are ordered by priority (higher first).
        """
        selected = sorted(
            [self._skills[sid] for sid in skill_ids if sid in self._skills],
            key=lambda s: -s.priority,
        )
        parts = []
        for skill in selected:
            if skill.instructions:
                parts.append(f"## Skill: {skill.name}\n\n{skill.instructions}")
        return "\n\n---\n\n".join(parts)

    def reload(self) -> None:
        """Hot-reload: clear and re-scan the skills directory."""
        self._skills.clear()
        self.load_all()
