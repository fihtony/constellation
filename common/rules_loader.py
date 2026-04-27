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


def build_system_prompt(base_prompt: str, agent_dir: str, **kwargs) -> str:
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
        The combined prompt: ``base_prompt + rules_context``.
    """
    rules = load_rules(agent_dir, **kwargs)
    if not rules:
        return base_prompt
    return (
        f"{base_prompt}\n\n"
        f"--- AGENT RULES (you MUST follow these constraints) ---\n\n"
        f"{rules}"
    )
