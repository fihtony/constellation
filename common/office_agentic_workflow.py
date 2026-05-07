"""Backward-compatible re-export stub.

The canonical implementation has moved to office/agentic_workflow.py.
This module exists so existing tests and external imports continue to work.
New code should import directly from office.agentic_workflow.
"""
# ruff: noqa: F401, F403
from office.agentic_workflow import *  # noqa: F401,F403
from office.agentic_workflow import (  # noqa: F401
    OFFICE_AGENT_RUNTIME_TOOL_NAMES,
    DEFAULT_OFFICE_AGENT_SKILL_PLAYBOOKS,
    build_office_agent_runtime_config,
    configure_office_control_tools,
    build_office_task_prompt,
)
