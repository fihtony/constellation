"""Skill-loading tool for the Connect Agent runtime.

Loads SKILL.md files from .github/skills/ on demand.
Self-registers on import.
"""

from __future__ import annotations

import os
from pathlib import Path

from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import register_tool

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_skills_root: str = ""


def configure_skill_tool(*, skills_root: str) -> None:
    """Set the base directory for skill files."""
    global _skills_root
    _skills_root = skills_root


class LoadSkillTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="load_skill",
            description=(
                "Load a skill playbook by name from .github/skills/<name>/SKILL.md. "
                "The skill content is returned as text for use in the current context."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Skill name (directory name under .github/skills/).",
                    },
                },
                "required": ["name"],
            },
        )

    def execute(self, args: dict) -> dict:
        name = args.get("name", "").strip()
        if not name:
            return self.error("Skill name must not be empty.")

        # Sanitise name to prevent path traversal
        safe_name = name.replace("..", "").replace("/", "").replace("\\", "")
        if not safe_name:
            return self.error("Invalid skill name.")

        # Try multiple possible roots
        roots = [_skills_root] if _skills_root else []
        # Also try relative to CWD
        roots.append(os.path.join(os.getcwd(), ".github", "skills"))
        # Also try /app/.github/skills (Docker convention)
        roots.append("/app/.github/skills")

        for root in roots:
            skill_path = os.path.join(root, safe_name, "SKILL.md")
            if os.path.isfile(skill_path):
                try:
                    content = Path(skill_path).read_text(encoding="utf-8")
                    # Truncate if very large
                    if len(content) > 50_000:
                        content = content[:50_000] + "\n\n... [skill content truncated]"
                    return self.ok(f"Skill: {safe_name}\n\n{content}")
                except OSError as exc:
                    return self.error(f"Failed to read skill: {exc}")

        # List available skills for better UX
        available: list[str] = []
        for root in roots:
            if os.path.isdir(root):
                for entry in sorted(os.listdir(root)):
                    candidate = os.path.join(root, entry, "SKILL.md")
                    if os.path.isfile(candidate) and entry not in available:
                        available.append(entry)
        hint = ""
        if available:
            hint = f" Available: {', '.join(available[:20])}"
        return self.error(f"Skill '{safe_name}' not found.{hint}")


register_tool(LoadSkillTool())
