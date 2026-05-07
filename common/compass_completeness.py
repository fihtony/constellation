"""Backward-compatible re-export stub.

The canonical implementation has moved to compass/completeness.py.
This module exists so existing tests and external imports continue to work.
New code should import directly from compass.completeness.
"""
# ruff: noqa: F401, F403
from compass.completeness import *  # noqa: F401,F403
from compass.completeness import (  # noqa: F401
    extract_pr_evidence_from_artifacts,
    extract_team_lead_completeness_issues,
    build_completeness_follow_up_message,
    derive_task_card_status,
)
