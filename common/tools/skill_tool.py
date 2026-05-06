"""Skill-loading tools for the Constellation agentic runtime.

Loads SKILL.md files from .github/skills/ on demand.
When a REGISTRY_URL is available, the load_skill tool also supports
registry-backed catalog lookup (Phase 4: skill catalog and dynamic discovery).
Self-registers on import.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import register_tool

# ---------------------------------------------------------------------------
# Shared state
# ---------------------------------------------------------------------------
_skills_root: str = ""
_registry_url: str = ""


def configure_skill_tool(*, skills_root: str, registry_url: str = "") -> None:
    """Set the base directory for skill files and optional Registry URL."""
    global _skills_root, _registry_url
    _skills_root = skills_root
    _registry_url = registry_url or os.environ.get("REGISTRY_URL", "")


def _registry_url_effective() -> str:
    return _registry_url or os.environ.get("REGISTRY_URL", "")


def _fetch_skill_from_registry(skill_id: str) -> str | None:
    """Try to fetch skill content from Registry /skills/{skillId}.

    Returns the SKILL.md content string if found and non-empty, else None.
    Falls back silently so local filesystem lookup can take over.
    """
    base = _registry_url_effective()
    if not base:
        return None
    safe_id = skill_id.replace("..", "").replace("/", "").replace("\\", "")
    if not safe_id:
        return None
    try:
        req = Request(
            f"{base}/skills/{safe_id}",
            headers={"Accept": "application/json"},
        )
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        # Registry returns { "id": ..., "content": "..." }
        content = data.get("content") or ""
        return content if content.strip() else None
    except (URLError, OSError, json.JSONDecodeError):
        return None


def _list_skills_from_registry() -> list[dict] | None:
    """Fetch the skill catalog from Registry /skills/catalog.

    Returns a list of skill summary dicts if successful, else None.
    """
    base = _registry_url_effective()
    if not base:
        return None
    try:
        req = Request(f"{base}/skills/catalog", headers={"Accept": "application/json"})
        with urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("skills") or []
    except (URLError, OSError, json.JSONDecodeError):
        return None


class LoadSkillTool(ConstellationTool):
    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="load_skill",
            description=(
                "Load a skill playbook by name. "
                "First tries the Registry catalog (Phase 4 dynamic discovery), "
                "then falls back to local .github/skills/<name>/SKILL.md. "
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

        # Phase 4: Try Registry catalog first (dynamic discovery)
        registry_content = _fetch_skill_from_registry(safe_name)
        if registry_content:
            if len(registry_content) > 50_000:
                registry_content = registry_content[:50_000] + "\n\n... [skill content truncated]"
            return self.ok(f"Skill: {safe_name} [source: registry]\n\n{registry_content}")

        # Fallback: local filesystem lookup
        roots = [_skills_root] if _skills_root else []
        roots.append(os.path.join(os.getcwd(), ".github", "skills"))
        roots.append("/app/.github/skills")

        for root in roots:
            skill_path = os.path.join(root, safe_name, "SKILL.md")
            if os.path.isfile(skill_path):
                try:
                    content = Path(skill_path).read_text(encoding="utf-8")
                    if len(content) > 50_000:
                        content = content[:50_000] + "\n\n... [skill content truncated]"
                    return self.ok(f"Skill: {safe_name} [source: local]\n\n{content}")
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
            hint = f" Available locally: {', '.join(available[:20])}"
        elif _registry_url_effective():
            hint = " (Registry reachable but skill not found in catalog)"
        return self.error(f"Skill '{safe_name}' not found.{hint}")


class ListSkillsTool(ConstellationTool):
    """List available skills from the Registry catalog or local filesystem."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="list_skills",
            description=(
                "List all available skill playbooks. "
                "Queries the Registry catalog first (dynamic discovery); "
                "falls back to listing local .github/skills/ directories. "
                "Returns skill IDs, descriptions, and version information."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "filter_tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional list of tags to filter skills by.",
                    },
                    "applies_to_agent": {
                        "type": "string",
                        "description": "Optional agent ID to filter skills that apply to a specific agent.",
                    },
                },
                "required": [],
            },
        )

    def execute(self, args: dict) -> dict:
        filter_tags = list(args.get("filter_tags") or [])
        applies_to_agent = str(args.get("applies_to_agent") or "").strip()

        # Phase 4: Try Registry catalog
        catalog = _list_skills_from_registry()
        if catalog is not None:
            if filter_tags:
                catalog = [
                    s for s in catalog
                    if any(t in (s.get("tags") or []) for t in filter_tags)
                ]
            if applies_to_agent:
                catalog = [
                    s for s in catalog
                    if applies_to_agent in (s.get("appliesTo", {}).get("agents") or [])
                    or "*" in (s.get("appliesTo", {}).get("agents") or [])
                ]
            return self.ok(
                json.dumps(
                    {"source": "registry", "count": len(catalog), "skills": catalog},
                    ensure_ascii=False,
                    indent=2,
                )
            )

        # Fallback: local filesystem listing
        roots = [_skills_root] if _skills_root else []
        roots.append(os.path.join(os.getcwd(), ".github", "skills"))
        roots.append("/app/.github/skills")

        seen: set[str] = set()
        skills: list[dict] = []
        for root in roots:
            if not os.path.isdir(root):
                continue
            for entry in sorted(os.listdir(root)):
                if entry in seen:
                    continue
                skill_md = os.path.join(root, entry, "SKILL.md")
                skill_yaml = os.path.join(root, entry, "skill.yaml")
                if os.path.isfile(skill_md):
                    seen.add(entry)
                    info: dict = {"id": entry, "source": "local"}
                    if os.path.isfile(skill_yaml):
                        try:
                            with open(skill_yaml, encoding="utf-8") as fh:
                                raw = fh.read()
                            # Simple YAML parsing for id/description/tags
                            for line in raw.splitlines():
                                for key in ("description", "version", "level"):
                                    if line.strip().startswith(f"{key}:"):
                                        info[key] = line.split(":", 1)[1].strip().strip('"').strip("'")
                        except OSError:
                            pass
                    skills.append(info)

        if filter_tags:
            skills = [s for s in skills if any(t in (s.get("tags") or []) for t in filter_tags)]

        return self.ok(
            json.dumps(
                {"source": "local", "count": len(skills), "skills": skills},
                ensure_ascii=False,
                indent=2,
            )
        )


register_tool(LoadSkillTool())
register_tool(ListSkillsTool())
