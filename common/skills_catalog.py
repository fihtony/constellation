"""Skills Catalog Scanner and Index.

Scans .github/skills/ directories for skill.yaml + SKILL.md files and builds
an in-memory catalog index. Used by the Registry to serve skill catalog endpoints.

Registry exposes:
  GET  /skills/catalog          — full catalog listing
  GET  /skills/catalog/version  — current catalog version hash
  POST /skills/query            — filtered skill query
  GET  /skills/{skillId}        — single skill by id
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from typing import Any

try:
    import yaml as _yaml_lib  # type: ignore

    def _load_yaml(text: str) -> dict:
        return _yaml_lib.safe_load(text) or {}
except ImportError:
    _yaml_lib = None  # type: ignore

    def _load_yaml(text: str) -> dict:  # type: ignore[misc]
        """Minimal YAML subset parser (used when PyYAML is not installed).

        Handles the simple flat/nested-mapping/list format used in skill.yaml files.
        Supports both block lists (`- item`) and inline lists (`[a, b, c]`).
        """
        def _parse_value(v: str):
            v = v.strip()
            if v.startswith("[") and v.endswith("]"):
                inner = v[1:-1].strip()
                if not inner:
                    return []
                return [item.strip().strip('"').strip("'") for item in inner.split(",")]
            return v.strip('"').strip("'")

        result: dict[str, Any] = {}
        current_key: str | None = None
        current_list: list | None = None
        current_nested: dict | None = None
        current_nested_key: str | None = None

        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            stripped = line.lstrip()

            # Skip comments and blank lines
            if not stripped or stripped.startswith("#"):
                current_list = None
                current_nested = None
                current_nested_key = None
                continue

            indent = len(line) - len(stripped)

            # List item inside a nested mapping value (indent >= 4)
            if stripped.startswith("- ") and current_nested is not None and current_nested_key and indent >= 4:
                val = stripped[2:].strip().strip('"').strip("'")
                existing = current_nested.get(current_nested_key)
                if not isinstance(existing, list):
                    current_nested[current_nested_key] = []
                current_nested[current_nested_key].append(val)
                current_list = None
                continue

            # Top-level list item (indent == 2)
            if stripped.startswith("- ") and current_key and indent == 2:
                if current_list is None:
                    current_list = []
                    result[current_key] = current_list
                val = stripped[2:].strip().strip('"').strip("'")
                current_list.append(val)
                continue

            # Nested mapping key: value (indent == 2)
            if ":" in stripped and indent == 2 and current_key:
                k, _, v = stripped.partition(":")
                parsed_v = _parse_value(v)
                if current_nested is None:
                    current_nested = {}
                    result[current_key] = current_nested
                current_nested_key = k.strip()
                if parsed_v != "":
                    current_nested[current_nested_key] = parsed_v
                else:
                    current_nested[current_nested_key] = []
                current_list = None
                continue

            # Top-level key: value (indent == 0)
            if ":" in stripped and indent == 0:
                current_list = None
                current_nested = None
                current_nested_key = None
                k, _, v = stripped.partition(":")
                current_key = k.strip()
                parsed_v = _parse_value(v)
                if parsed_v != "":
                    result[current_key] = parsed_v
                # else: value follows on next lines
                continue

        return result


# ---------------------------------------------------------------------------
# Catalog scanner
# ---------------------------------------------------------------------------

class SkillsCatalog:
    """Thread-safe in-memory skills catalog built from skill.yaml files."""

    def __init__(self, skills_root: str | None = None):
        # Default: locate .github/skills/ relative to common/ or app root.
        if skills_root is None:
            # Try /app/.github/skills first (Docker), then repo root.
            candidates = [
                "/app/.github/skills",
                os.path.join(os.path.dirname(__file__), "..", ".github", "skills"),
            ]
            skills_root = next((p for p in candidates if os.path.isdir(p)), candidates[0])
        self._skills_root = os.path.abspath(skills_root)
        self._lock = threading.Lock()
        self._skills: dict[str, dict] = {}
        self._version: str = ""
        self._scanned_at: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(self) -> None:
        """(Re)scan the skills root directory and rebuild the catalog."""
        skills: dict[str, dict] = {}
        if not os.path.isdir(self._skills_root):
            with self._lock:
                self._skills = skills
                self._version = _catalog_hash(skills)
                self._scanned_at = time.time()
            return

        for entry in sorted(os.listdir(self._skills_root)):
            skill_dir = os.path.join(self._skills_root, entry)
            if not os.path.isdir(skill_dir):
                continue
            yaml_path = os.path.join(skill_dir, "skill.yaml")
            md_path = os.path.join(skill_dir, "SKILL.md")
            if not os.path.isfile(yaml_path):
                continue
            try:
                with open(yaml_path, encoding="utf-8") as fh:
                    raw_yaml = fh.read()
                meta = _load_yaml(raw_yaml)
            except Exception:
                continue

            skill_id = meta.get("id") or entry
            # Optionally attach SKILL.md summary (first non-frontmatter heading or description)
            description_md = ""
            if os.path.isfile(md_path):
                try:
                    with open(md_path, encoding="utf-8") as fh:
                        description_md = _extract_md_description(fh.read())
                except Exception:
                    pass

            skills[skill_id] = {
                "id": skill_id,
                "version": meta.get("version", "0.0.0"),
                "level": meta.get("level", "generic"),
                "owner": meta.get("owner", ""),
                "appliesTo": meta.get("appliesTo", {}),
                "runtimeCompatibility": meta.get("runtimeCompatibility", {}),
                "requiredTools": meta.get("requiredTools", []),
                "provides": meta.get("provides", []),
                "hashPolicy": meta.get("hashPolicy", "sha256"),
                "trustLevel": meta.get("trustLevel", "unreviewed"),
                "description": description_md,
                "directory": entry,
            }

        with self._lock:
            self._skills = skills
            self._version = _catalog_hash(skills)
            self._scanned_at = time.time()

    def get_version(self) -> str:
        with self._lock:
            return self._version

    def get_catalog(self) -> list[dict]:
        with self._lock:
            return list(self._skills.values())

    def get_skill(self, skill_id: str) -> dict | None:
        with self._lock:
            return self._skills.get(skill_id)

    def query(self, payload: dict) -> dict:
        """Filter skills based on agent role, capabilities, and task metadata.

        Expected payload (mirrors design doc §8.5.1):
        {
          "agentId": "team-lead-agent",
          "agentRole": "team-lead",
          "capabilities": ["team-lead.task.analyze"],
          "targetCategories": ["generic", "workflow:development", "agent:team-lead"],
          "taskMetadata": {
            "workflow": "development",
            "platform": "android",
            "languages": ["kotlin"],
            "tags": ["compose", "ui"]
          }
        }
        """
        agent_role = payload.get("agentRole", "")
        agent_id = payload.get("agentId", "")
        target_categories: list[str] = payload.get("targetCategories", [])
        task_meta: dict = payload.get("taskMetadata", {})

        # Extract filter hints from targetCategories
        accepted_workflows: set[str] = set()
        accepted_agent_roles: set[str] = set()
        for cat in target_categories:
            if cat.startswith("workflow:"):
                accepted_workflows.add(cat[len("workflow:"):])
            elif cat.startswith("agent:"):
                accepted_agent_roles.add(cat[len("agent:"):])
            elif cat == "generic":
                accepted_agent_roles.add("*")

        requested_languages: set[str] = {lang.lower() for lang in task_meta.get("languages", [])}
        requested_tags: set[str] = {t.lower() for t in task_meta.get("tags", [])}
        requested_workflow: str = task_meta.get("workflow", "")
        requested_platform: str = task_meta.get("platform", "")

        matched: list[dict] = []
        rejected: list[dict] = []

        with self._lock:
            skills_snapshot = dict(self._skills)

        for skill in skills_snapshot.values():
            applies_to: dict = skill.get("appliesTo", {})
            skill_roles: list[str] = applies_to.get("agentRoles", [])
            skill_workflows: list[str] = applies_to.get("workflows", [])
            skill_langs: list[str] = [lang.lower() for lang in applies_to.get("languages", [])]
            skill_tags: list[str] = [t.lower() for t in applies_to.get("taskTags", [])]

            # Role check: skill must apply to agent's role OR be generic
            role_match = (
                not skill_roles
                or agent_role in skill_roles
                or (agent_id and any(agent_id.startswith(r) or r in agent_id for r in skill_roles))
                or skill.get("level") == "generic"
            )
            if not role_match:
                rejected.append({"id": skill["id"], "reason": "role_mismatch"})
                continue

            # Workflow filter (optional — if requested, skill must match)
            if requested_workflow and skill_workflows:
                if requested_workflow not in skill_workflows:
                    rejected.append({"id": skill["id"], "reason": "workflow_mismatch"})
                    continue

            # Language filter (optional — only if both sides have values)
            if requested_languages and skill_langs:
                if not requested_languages.intersection(skill_langs):
                    rejected.append({"id": skill["id"], "reason": "language_mismatch"})
                    continue

            matched.append(skill)

        return {
            "catalogVersion": self._version,
            "matched": matched,
            "rejected": rejected,
            "filters": {
                "agentRole": agent_role,
                "agentId": agent_id,
                "taskMetadata": task_meta,
            },
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _catalog_hash(skills: dict) -> str:
    """Compute a stable sha256 fingerprint for the catalog state."""
    payload = json.dumps(
        {k: {"id": v["id"], "version": v["version"]} for k, v in sorted(skills.items())},
        sort_keys=True,
    ).encode()
    return hashlib.sha256(payload).hexdigest()[:16]


def _extract_md_description(md_text: str) -> str:
    """Extract a short description from a SKILL.md file.

    Returns the first non-frontmatter content (first heading or paragraph).
    """
    # Strip YAML frontmatter (--- ... ---)
    text = re.sub(r"^---\n.*?\n---\n", "", md_text, count=1, flags=re.DOTALL).strip()
    lines = text.splitlines()
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:200]
        if stripped.startswith("# "):
            return stripped[2:200]
    return ""
