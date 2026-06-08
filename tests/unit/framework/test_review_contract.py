"""Tests for shared review contracts used across agents."""

from framework.review_contract import (
    REVIEW_ISSUE_SCHEMA,
    annotate_issue_blocking,
    issue_blocks_merge,
)


def test_review_issue_schema_mentions_blocking_field():
    assert '"blocking"' in REVIEW_ISSUE_SCHEMA


def test_issue_blocks_merge_for_blocking_security_issue():
    assert issue_blocks_merge(
        {
            "severity": "high",
            "source_phase": "security",
            "blocking": True,
            "message": "Unsafe shell execution.",
        }
    ) is True


def test_issue_blocks_merge_ignores_design_only_requirement_issue():
    assert issue_blocks_merge(
        {
            "severity": "high",
            "source_phase": "requirements",
            "blocking": True,
            "message": "Typography color token does not match the design spec.",
            "suggestion": "Use the design token from the spec.",
        }
    ) is False


def test_annotate_issue_blocking_preserves_requested_flag_and_adds_effective_flag():
    annotated = annotate_issue_blocking(
        {
            "severity": "high",
            "source_phase": "requirements",
            "blocking": True,
            "message": "Typography color token does not match the design spec.",
            "suggestion": "Use the design token from the spec.",
        }
    )

    assert annotated["blocking"] is True
    assert annotated["blocking_requested"] is True
    assert annotated["effective_blocking"] is False
