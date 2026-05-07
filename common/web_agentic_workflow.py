"""Backward-compatible re-export stub.

The canonical implementation has moved to web/agentic_workflow.py.
This module exists so existing tests and external imports continue to work.
New code should import directly from web.agentic_workflow.
"""
# ruff: noqa: F401, F403
from web.agentic_workflow import *  # noqa: F401,F403
from web.agentic_workflow import (  # noqa: F401
    WEB_AGENT_RUNTIME_TOOL_NAMES,
    DEFAULT_WEB_AGENT_SKILL_PLAYBOOKS,
    build_web_agent_runtime_config,
    configure_web_agent_control_tools,
    build_web_task_prompt,
)
