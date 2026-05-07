"""Backward-compatible re-export stub.

The canonical implementation has moved to compass/agentic_workflow.py.
This module exists so existing tests and external imports continue to work.
New code should import directly from compass.agentic_workflow.
"""
# ruff: noqa: F401, F403
from compass.agentic_workflow import *  # noqa: F401,F403
from compass.agentic_workflow import (  # noqa: F401
    COMPASS_RUNTIME_TOOL_NAMES,
    build_compass_workflow_prompt,
    run_compass_workflow,
    _load_orchestrate_template,
)
