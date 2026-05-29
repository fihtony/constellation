"""Standards Loader — loads and merges coding standards from config/standards/.

Provides merged rule sets that can be injected into Code Review and Dev agent prompts.
Standards are frozen at task dispatch time — no hot-reload during execution.

Priority (high to low):
  1. tech-stack/<detected-stack>.yaml   — tech-specific overrides
  2. company/coding_standard.yaml       — company custom rules
  3. company/security_standard.yaml     — company security rules
  4. company/review_standard.yaml       — company review rules (code-review only)
  5. base/code_quality_base.yaml        — system baseline
  6. base/security_base.yaml            — security baseline
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml  # type: ignore


_STANDARDS_ROOT = Path(__file__).resolve().parent.parent / "config" / "standards"


def _load_yaml(path: Path) -> dict[str, Any]:
    """Load a single YAML file, returning empty dict on failure."""
    try:
        with open(path, encoding="utf-8") as fh:
            return yaml.safe_load(fh) or {}
    except Exception:
        return {}


def _extract_rules(data: dict[str, Any], source: str) -> list[dict[str, Any]]:
    """Extract rule dicts from a standards YAML file, enriching with source."""
    rules: list[dict[str, Any]] = []
    for rule in data.get("rules", []):
        rules.append({**rule, "source": source})
    return rules


def _apply_severity_overrides(
    rules: list[dict[str, Any]],
    overrides: list[dict[str, Any]],
) -> None:
    """Apply severity_overrides from higher-priority layers to existing rules."""
    if not overrides:
        return
    override_map = {o["rule"]: o["severity"] for o in overrides if "rule" in o and "severity" in o}
    for rule in rules:
        rule_id = rule.get("id", "")
        if rule_id in override_map:
            rule["severity"] = override_map[rule_id]


def load_standards(
    tech_stack: list[str] | None = None,
    agent_role: str = "development",
    standards_root: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Load and merge standards from base/ + company/ + tech-stack/ (filtered).

    Args:
        tech_stack: Detected project tech stack (e.g. ["react", "nextjs", "typescript"]).
        agent_role: Either "development" or "code-review". Filters rules by applies_to.
        standards_root: Override path to the standards directory.

    Returns a flat list of rule dicts with keys: id, severity, description, category, source.
    Rules are ordered from lowest priority (base) to highest (tech-stack).
    """
    root = Path(standards_root) if standards_root else _STANDARDS_ROOT
    rules: list[dict[str, Any]] = []
    all_overrides: list[dict[str, Any]] = []

    # Layer 1: Load base standards (lowest priority)
    base_folder = root / "base"
    if base_folder.is_dir():
        for yaml_file in sorted(base_folder.glob("*.yaml")):
            data = _load_yaml(yaml_file)
            applies_to = data.get("applies_to", ["development", "code-review"])
            if agent_role in applies_to:
                rules.extend(_extract_rules(data, f"base/{yaml_file.stem}"))
                all_overrides.extend(data.get("severity_overrides", []))

    # Layer 2: Load company standards (overrides base)
    company_folder = root / "company"
    if company_folder.is_dir():
        for yaml_file in sorted(company_folder.glob("*.yaml")):
            data = _load_yaml(yaml_file)
            applies_to = data.get("applies_to", ["development", "code-review"])
            if agent_role in applies_to:
                rules.extend(_extract_rules(data, f"company/{yaml_file.stem}"))
                all_overrides.extend(data.get("severity_overrides", []))

    # Layer 3: Load tech-stack standards (highest priority, overrides conflicts)
    ts_folder = root / "tech-stack"
    if ts_folder.is_dir() and tech_stack:
        normalized = {t.lower().replace(" ", "-").replace("_", "-") for t in tech_stack}
        for yaml_file in sorted(ts_folder.glob("*.yaml")):
            data = _load_yaml(yaml_file)
            file_stack = {s.lower().replace(" ", "-").replace("_", "-")
                          for s in data.get("tech_stack", [yaml_file.stem])}
            # Match if any declared stack token overlaps with requested tech_stack
            if file_stack & normalized:
                applies_to = data.get("applies_to", ["development", "code-review"])
                if agent_role in applies_to:
                    rules.extend(_extract_rules(data, f"tech-stack/{yaml_file.stem}"))
                    all_overrides.extend(data.get("severity_overrides", []))

    # Apply severity overrides from higher-priority layers
    _apply_severity_overrides(rules, all_overrides)

    # Deduplicate by rule id (keep last occurrence = highest priority)
    seen: dict[str, int] = {}
    for idx, rule in enumerate(rules):
        seen[rule.get("id", f"_anon_{idx}")] = idx
    rules = [rules[idx] for idx in sorted(seen.values())]

    return rules


def format_standards_for_prompt(
    rules: list[dict[str, Any]],
    max_rules: int = 50,
    agent_role: str = "development",
) -> str:
    """Format rules into a prompt-friendly text block.

    Groups rules by category for readability. Used by both Development and
    Code Review agents to ensure consistent enforcement.
    """
    if not rules:
        return ""

    # Group by category
    categorized: dict[str, list[dict[str, Any]]] = {}
    for rule in rules[:max_rules]:
        cat = rule.get("category", "general")
        categorized.setdefault(cat, []).append(rule)

    lines: list[str] = []
    if agent_role == "code-review":
        lines.append("## Coding & Security Standards (enforce during review)\n")
    else:
        lines.append("## Coding & Security Standards (follow during implementation)\n")

    lines.append("These standards are enforced by both development and review pipelines.\n")

    for cat, cat_rules in sorted(categorized.items()):
        lines.append(f"\n### {cat.replace('-', ' ').replace('_', ' ').title()}\n")
        for rule in cat_rules:
            sev = rule.get("severity", "info").upper()
            desc = rule.get("description", "")
            rule_id = rule.get("id", "unknown")
            owasp = rule.get("owasp_ref", "")
            suffix = f" [OWASP {owasp}]" if owasp else ""
            lines.append(f"- [{sev}] {desc}{suffix} (rule: {rule_id})")

    return "\n".join(lines)


def detect_tech_stack_from_repo(repo_path: str) -> list[str]:
    """Auto-detect tech stack from a repository's files and config.

    Returns a list of tech stack identifiers (e.g. ["react", "nextjs", "typescript"]).
    Used by Team Lead when dispatching dev/CR agents.
    """
    if not repo_path or not os.path.isdir(repo_path):
        return []

    stack: set[str] = set()
    files = set()
    try:
        for entry in os.listdir(repo_path):
            files.add(entry.lower())
    except OSError:
        return []

    # Package.json detection
    pkg_json_path = os.path.join(repo_path, "package.json")
    if os.path.isfile(pkg_json_path):
        stack.add("nodejs")
        try:
            import json
            with open(pkg_json_path, encoding="utf-8") as fh:
                pkg = json.load(fh)
            all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "react" in all_deps or "react-dom" in all_deps:
                stack.add("react")
            if "next" in all_deps:
                stack.add("nextjs")
            if "vue" in all_deps:
                stack.add("vue")
            if "express" in all_deps:
                stack.add("express")
            if "typescript" in all_deps or "tsconfig.json" in files:
                stack.add("typescript")
            if "@angular/core" in all_deps:
                stack.add("angular")
        except Exception:
            pass

    # Python detection
    if any(f in files for f in ("requirements.txt", "pyproject.toml", "setup.py", "pipfile")):
        stack.add("python")
        # Check for FastAPI
        for fname in ("requirements.txt", "pyproject.toml"):
            fpath = os.path.join(repo_path, fname)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, encoding="utf-8") as fh:
                        content = fh.read().lower()
                    if "fastapi" in content:
                        stack.add("fastapi")
                    if "django" in content:
                        stack.add("django")
                    if "flask" in content:
                        stack.add("flask")
                except OSError:
                    pass

    # Java/Spring detection
    if any(f in files for f in ("pom.xml", "build.gradle", "build.gradle.kts")):
        stack.add("java")
        for fname in ("pom.xml", "build.gradle", "build.gradle.kts"):
            fpath = os.path.join(repo_path, fname)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, encoding="utf-8") as fh:
                        content = fh.read().lower()
                    if "spring" in content:
                        stack.add("spring")
                    if "kotlin" in content:
                        stack.add("kotlin")
                    if "android" in content:
                        stack.add("android")
                except OSError:
                    pass

    # Android specific
    if "settings.gradle" in files or "settings.gradle.kts" in files:
        for fname in ("settings.gradle", "settings.gradle.kts", "build.gradle", "build.gradle.kts"):
            fpath = os.path.join(repo_path, fname)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, encoding="utf-8") as fh:
                        content = fh.read().lower()
                    if "com.android" in content or "android {" in content:
                        stack.add("android")
                        stack.add("kotlin")
                        break
                except OSError:
                    pass

    # TypeScript config
    if "tsconfig.json" in files:
        stack.add("typescript")

    return sorted(stack)
