"""Per-agent manifest system prompt cache helper.

Usage in each agent's app.py:

    from common.agent_system_prompt import get_agent_manifest_prompt

    def _build_my_system_prompt(base_prompt: str) -> str:
        manifest = get_agent_manifest_prompt(__file__, agent_name="my-agent")
        if manifest and base_prompt:
            return f"{manifest}\\n\\n---\\n\\nTASK CONTEXT:\\n{base_prompt}"
        return manifest or base_prompt or ""
"""

from __future__ import annotations

import os

from common.prompt_builder import build_system_prompt_from_manifest


def get_agent_manifest_prompt(agent_file: str, *, agent_name: str = "") -> str:
    """Return the manifest-based system prompt for the agent owning `agent_file`.

    Falls back to empty string if no manifest.yaml exists.
    """
    del agent_name
    agent_dir = os.path.dirname(os.path.abspath(agent_file))
    return build_system_prompt_from_manifest(agent_dir)


def build_agent_system_prompt(
    agent_file: str,
    base_prompt: str = "",
    *,
    agent_name: str = "",
) -> str:
    """Build a system prompt combining the manifest context and a task-specific base.

    If a manifest-based prompt is available, it forms the structural context and
    `base_prompt` is appended as task-specific specialization.  Falls back to
    `base_prompt` alone if no manifest exists.

    Args:
        agent_file: Pass ``__file__`` from the agent's app.py.
        base_prompt: Optional short task-specific system prompt (e.g. from prompts.py).
        agent_name: Human-readable agent name for debugging (unused in output).

    Returns:
        Combined system prompt string.
    """
    manifest = get_agent_manifest_prompt(agent_file, agent_name=agent_name)
    if manifest and base_prompt:
        return f"{manifest}\n\n---\n\nTASK CONTEXT:\n{base_prompt}"
    return manifest or base_prompt or ""
