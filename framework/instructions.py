"""Agent instructions loader.

Instructions are kept in Markdown files separate from code so they can be
reviewed, audited, and updated independently of the agent logic.

Convention:
  agents/<agent_id>/instructions/<filename>.md

The system prompt is always ``system.md``.  Additional skill-specific or
context-specific instructions may live in other Markdown files in the same
directory.
"""
from __future__ import annotations

import os
from functools import lru_cache

# Root of the project (two levels up from this file: framework/ → project root)
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_instructions(agent_id: str, filename: str = "system.md") -> str:
    """Load agent instructions from ``agents/<agent_id>/instructions/<filename>``.

    Parameters
    ----------
    agent_id:
        Agent identifier (e.g. ``"compass"``, ``"team-lead"``).
        Hyphens are converted to underscores for the directory name.
    filename:
        Markdown file name (default: ``system.md``).

    Returns
    -------
    str
        File contents, or an empty string if the file does not exist.
    """
    agent_dir = agent_id.replace("-", "_")
    path = os.path.join(
        _PROJECT_ROOT, "agents", agent_dir, "instructions", filename
    )
    if not os.path.isfile(path):
        return ""
    with open(path, encoding="utf-8") as fh:
        return fh.read()


@lru_cache(maxsize=32)
def load_instructions_cached(agent_id: str, filename: str = "system.md") -> str:
    """Cached version of :func:`load_instructions`.

    Use in production where instructions do not change at runtime.
    """
    return load_instructions(agent_id, filename)
