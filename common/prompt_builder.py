"""Prompt Builder — assembles modular system prompts from agent prompts/ directories.

Reads a `prompts/system/manifest.yaml` file from an agent directory and assembles
a system prompt by concatenating the listed Markdown files in order.

Usage:
    from common.prompt_builder import build_system_prompt_from_manifest

    system_prompt = build_system_prompt_from_manifest("/app/team-lead")
    # optionally append skill playbooks
    system_prompt = build_system_prompt_from_manifest(
        "/app/team-lead",
        skill_names=["constellation-architecture-delivery"],
        skills_root="/app/.github/skills",
    )
"""

from __future__ import annotations

import json
import os
import re
from typing import Sequence
from urllib.error import URLError
from urllib.request import Request, urlopen


def build_system_prompt_from_manifest(
    agent_dir: str,
    skill_names: Sequence[str] | None = None,
    skills_root: str | None = None,
    registry_url: str | None = None,
) -> str:
    """Build a system prompt from `<agent_dir>/prompts/system/manifest.yaml`.

    Args:
        agent_dir: Root directory of the agent (e.g. `/app/team-lead`).
        skill_names: Optional list of skill IDs to append from skills_root.
            If not provided, reads `skillNames` from the manifest (if present).
        skills_root: Path to the local skills catalog root (e.g. `/app/.github/skills`).
            Defaults to `<agent_dir>/../.github/skills/` if not set.
        registry_url: Optional Registry base URL for registry-backed skill fetch.
            Defaults to `REGISTRY_URL` from the environment when not provided.

    Returns:
        A single assembled system prompt string. Falls back to empty string if
        the manifest is missing (so callers can degrade gracefully).
    """
    manifest_path = os.path.join(agent_dir, "prompts", "system", "manifest.yaml")
    if not os.path.isfile(manifest_path):
        return ""

    order = _read_manifest_order(manifest_path)
    include_skills = _read_manifest_include_skills(manifest_path)

    # Skill names: explicit arg > manifest skillNames > empty
    effective_skill_names: list[str] = list(skill_names or [])
    if not effective_skill_names and include_skills:
        effective_skill_names = _read_manifest_skill_names(manifest_path)

    # Skills root: explicit arg > <agent_dir>/../.github/skills
    if not skills_root:
        parent = os.path.dirname(os.path.abspath(agent_dir))
        skills_root = os.path.join(parent, ".github", "skills")

    system_dir = os.path.join(agent_dir, "prompts", "system")
    parts: list[str] = []

    for filename in order:
        file_path = os.path.join(system_dir, filename)
        if not os.path.isfile(file_path):
            continue
        try:
            with open(file_path, encoding="utf-8") as fh:
                content = fh.read().strip()
            if content:
                parts.append(content)
        except OSError:
            pass

    if include_skills and effective_skill_names:
        for skill_id in effective_skill_names:
            skill_content = _fetch_skill_from_registry(skill_id, registry_url=registry_url)
            if not skill_content and skills_root:
                skill_md = os.path.join(skills_root, skill_id, "SKILL.md")
                if os.path.isfile(skill_md):
                    try:
                        with open(skill_md, encoding="utf-8") as fh:
                            skill_content = fh.read()
                    except OSError:
                        skill_content = ""
            skill_content = _strip_frontmatter(skill_content or "").strip()
            if skill_content:
                parts.append(f"## Skill: {skill_id}\n\n{skill_content}")

    return "\n\n---\n\n".join(parts)


def build_task_prompt(agent_dir: str, task_name: str) -> str:
    """Load a task-specific prompt template from `<agent_dir>/prompts/tasks/<task_name>.md`.

    Args:
        agent_dir: Root directory of the agent.
        task_name: Name of the task prompt file (without .md extension), e.g. "intake".

    Returns:
        File contents as a string, or empty string if not found.
    """
    task_path = os.path.join(agent_dir, "prompts", "tasks", f"{task_name}.md")
    if not os.path.isfile(task_path):
        return ""
    try:
        with open(task_path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _read_manifest_order(manifest_path: str) -> list[str]:
    """Parse `systemOrder` list from manifest.yaml (no yaml dependency)."""
    order: list[str] = []
    in_system_order = False
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("systemOrder:"):
                    in_system_order = True
                    continue
                if in_system_order:
                    if stripped.startswith("- "):
                        order.append(stripped[2:].strip())
                    elif stripped and not stripped.startswith("#"):
                        # New top-level key — stop reading systemOrder
                        in_system_order = False
    except OSError:
        pass
    return order


def _read_manifest_include_skills(manifest_path: str) -> bool:
    """Parse `includeSkills` boolean from manifest.yaml."""
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            for line in fh:
                m = re.match(r"^includeSkills\s*:\s*(true|false)", line.strip(), re.IGNORECASE)
                if m:
                    return m.group(1).lower() == "true"
    except OSError:
        pass
    return False


def _read_manifest_skill_names(manifest_path: str) -> list[str]:
    """Parse `skillNames` list from manifest.yaml (no yaml dependency)."""
    names: list[str] = []
    in_skill_names = False
    try:
        with open(manifest_path, encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped.startswith("skillNames:"):
                    in_skill_names = True
                    continue
                if in_skill_names:
                    if stripped.startswith("- "):
                        names.append(stripped[2:].strip())
                    elif stripped and not stripped.startswith("#"):
                        in_skill_names = False
    except OSError:
        pass
    return names


def _strip_frontmatter(text: str) -> str:
    """Remove YAML frontmatter (--- ... ---) from Markdown text."""
    return re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL)


def _registry_url_effective(registry_url: str | None = None) -> str:
    return str(registry_url or os.environ.get("REGISTRY_URL") or "").strip()


def _fetch_skill_from_registry(skill_id: str, *, registry_url: str | None = None) -> str:
    """Return SKILL.md content from the Registry when available.

    Falls back to an empty string on any network/protocol error so callers can
    transparently continue with local filesystem lookup.
    """
    base = _registry_url_effective(registry_url)
    if not base:
        return ""

    safe_id = str(skill_id or "").strip()
    if not safe_id or ".." in safe_id or "/" in safe_id or "\\" in safe_id:
        return ""

    try:
        req = Request(f"{base}/skills/{safe_id}", headers={"Accept": "application/json"})
        with urlopen(req, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, OSError, json.JSONDecodeError):
        return ""

    return str(payload.get("content") or "")
