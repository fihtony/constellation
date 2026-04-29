"""Load agent rules from Markdown files and build a combined context string.

Each agent directory may contain a ``rules/`` sub-directory with Markdown
files describing hard boundaries, output contracts, and safety constraints.
These files are loaded once at startup and injected into the LLM system
prompt so the model is aware of its operational constraints.

Usage::

    from common.rules_loader import load_rules

    # Load all rules for the web agent
    rules_context = load_rules("web")
    # → string ready to prepend/append to a system prompt

    # Load specific rules files only
    rules_context = load_rules("team-lead", files=["agent-principles.md"])
"""

from __future__ import annotations

import os

# Base directory of the project (one level up from common/)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SKILLS_ROOT = os.path.join(_PROJECT_ROOT, ".github", "skills")

# Default rule files to load, in order
DEFAULT_RULE_FILES = [
    "agent-principles.md",
    "output-contract.md",
    "safety-boundaries.md",
]

# Default workflow files to load
DEFAULT_WORKFLOW_FILES = [
    "default-workflow.md",
]

# Cache: agent_dir → combined rules string
_cache: dict[str, str] = {}


def _read_text_file(path: str) -> str:
    if not path or not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _strip_frontmatter(text: str) -> str:
    stripped = (text or "").strip()
    if not stripped.startswith("---\n"):
        return stripped
    parts = stripped.split("\n---\n", 1)
    if len(parts) == 2:
        return parts[1].strip()
    return stripped


def load_rules(
    agent_dir: str,
    *,
    files: list[str] | None = None,
    include_workflow: bool = False,
    max_chars: int = 6000,
) -> str:
    """Load and concatenate agent rule files into a single context string.

    Parameters
    ----------
    agent_dir:
        Agent directory name relative to project root (e.g. ``"team-lead"``).
    files:
        Explicit list of filenames under ``<agent_dir>/rules/`` to load.
        Defaults to ``DEFAULT_RULE_FILES``.
    include_workflow:
        If True, also load ``<agent_dir>/workflows/default-workflow.md``.
    max_chars:
        Truncate the combined output to this many characters.

    Returns
    -------
    str
        Combined rules text, or empty string if no files found.
    """
    cache_key = f"{agent_dir}:{','.join(files or DEFAULT_RULE_FILES)}:{include_workflow}"
    if cache_key in _cache:
        return _cache[cache_key]

    rules_dir = os.path.join(_PROJECT_ROOT, agent_dir, "rules")
    workflow_dir = os.path.join(_PROJECT_ROOT, agent_dir, "workflows")

    parts: list[str] = []

    # Load rule files
    for fname in (files or DEFAULT_RULE_FILES):
        fpath = os.path.join(rules_dir, fname)
        if os.path.isfile(fpath):
            try:
                with open(fpath, encoding="utf-8") as fh:
                    content = fh.read().strip()
                if content:
                    parts.append(content)
            except Exception:
                pass

    # Load workflow if requested
    if include_workflow:
        for fname in DEFAULT_WORKFLOW_FILES:
            fpath = os.path.join(workflow_dir, fname)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, encoding="utf-8") as fh:
                        content = fh.read().strip()
                    if content:
                        parts.append(content)
                except Exception:
                    pass

    combined = "\n\n---\n\n".join(parts)
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n...[rules truncated]"

    _cache[cache_key] = combined
    return combined


def load_skills(
    skill_names: list[str] | None,
    *,
    max_chars: int = 4500,
) -> str:
    """Load and concatenate skill guides from ``.github/skills``.

    Parameters
    ----------
    skill_names:
        Skill directory names under ``.github/skills``.
    max_chars:
        Truncate the combined output to this many characters.

    Returns
    -------
    str
        Combined skill text, or empty string if no skills were found.
    """
    if not skill_names:
        return ""

    normalized_names = [name.strip() for name in skill_names if name and name.strip()]
    if not normalized_names:
        return ""

    cache_key = f"skills:{','.join(normalized_names)}:{max_chars}"
    if cache_key in _cache:
        return _cache[cache_key]

    parts: list[str] = []
    for skill_name in normalized_names:
        skill_path = os.path.join(_SKILLS_ROOT, skill_name, "SKILL.md")
        content = _strip_frontmatter(_read_text_file(skill_path))
        if content:
            parts.append(content)

    combined = "\n\n---\n\n".join(parts)
    if len(combined) > max_chars:
        combined = combined[:max_chars] + "\n...[skills truncated]"

    _cache[cache_key] = combined
    return combined


def build_system_prompt(
    base_prompt: str,
    agent_dir: str,
    *,
    skill_names: list[str] | None = None,
    skill_max_chars: int = 4500,
    **kwargs,
) -> str:
    """Combine a base system prompt with loaded agent rules.

    Parameters
    ----------
    base_prompt:
        The original system prompt from ``prompts.py``.
    agent_dir:
        Agent directory name (e.g. ``"team-lead"``).
    **kwargs:
        Extra keyword arguments passed to :func:`load_rules`.

    Returns
    -------
    str
        The combined prompt: ``base_prompt + rules_context + skill_context``.
    """
    rules = load_rules(agent_dir, **kwargs)
    skills = load_skills(skill_names, max_chars=skill_max_chars)

    parts = [base_prompt]
    if rules:
        parts.append(
            "--- AGENT RULES (you MUST follow these constraints) ---\n\n"
            f"{rules}"
        )
    if skills:
        parts.append(
            "--- ADDITIONAL SKILLS (apply these playbooks when relevant) ---\n\n"
            f"{skills}"
        )
    return "\n\n".join(parts)
