"""Unified agentic runtime package.

Backends (``AGENT_RUNTIME``):

- ``copilot-cli``: primary agentic CLI path using GitHub Copilot CLI
- ``claude-code``: optional Claude Code CLI path
- ``copilot-connect``: OpenAI-compatible API path for local integration tests
- ``mock``: deterministic unit-test backend

``COPILOT_GITHUB_TOKEN`` is normally passed from Compass to per-task agents via
``passThroughEnv``. Persistent agents should receive it through docker-compose or
their local ``.env`` when ``AGENT_RUNTIME=copilot-cli`` is desired.
"""

from common.runtime.adapter import AgentRuntimeAdapter, get_runtime

__all__ = ["AgentRuntimeAdapter", "get_runtime"]
