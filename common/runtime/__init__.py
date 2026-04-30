"""Unified agentic runtime package.

Backends (``AGENT_RUNTIME``):

- ``connect-agent``: primary built-in runtime using the shared Copilot Connect transport
- ``copilot-cli``: optional GitHub Copilot CLI path
- ``claude-code``: optional Claude Code CLI path
- ``copilot-connect``: legacy OpenAI-compatible backend that reuses connect-agent transport
- ``mock``: deterministic unit-test backend

``COPILOT_GITHUB_TOKEN`` is normally passed from Compass to per-task agents via
``passThroughEnv`` only when the optional ``copilot-cli`` backend is selected.
"""

from common.runtime.adapter import AgentRuntimeAdapter, get_runtime

__all__ = ["AgentRuntimeAdapter", "get_runtime"]
