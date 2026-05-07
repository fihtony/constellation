"""Backward-compatible re-export stub.

The canonical implementation has moved to compass/office_routing.py.
This module exists so existing tests and external imports continue to work.
New code should import directly from compass.office_routing.
"""
# ruff: noqa: F401, F403
from compass.office_routing import *  # noqa: F401,F403
from compass.office_routing import (  # noqa: F401
    validate_office_target_paths,
    path_within_base,
    is_containerized,
    can_defer_office_path_existence_check,
    build_output_target_question,
    build_write_permission_question,
    build_office_dispatch_context,
    resume_office_clarification,
)
