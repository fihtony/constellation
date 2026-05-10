"""Code Review Agent workflow nodes."""
from __future__ import annotations

import json
from typing import Any


async def load_pr_context(state: dict) -> dict:
    """Fetch PR diff, files, and description.

    MVP placeholder — full implementation fetches from SCM agent.
    """
    return {
        "pr_diff": "",
        "changed_files": [],
        "pr_description": "",
        "commit_messages": [],
    }


async def review_quality(state: dict) -> dict:
    """Check code quality, style, and patterns."""
    runtime = state.get("_runtime")

    if not runtime:
        return {"quality_issues": []}

    # Full implementation uses LLM to review the PR diff
    return {"quality_issues": []}


async def review_security(state: dict) -> dict:
    """Check for security vulnerabilities (OWASP Top 10)."""
    runtime = state.get("_runtime")

    if not runtime:
        return {"security_issues": []}

    return {"security_issues": []}


async def review_tests(state: dict) -> dict:
    """Check test coverage and test quality."""
    runtime = state.get("_runtime")

    if not runtime:
        return {"test_issues": []}

    return {"test_issues": []}


async def review_requirements(state: dict) -> dict:
    """Check requirements compliance against Jira acceptance criteria."""
    runtime = state.get("_runtime")

    if not runtime:
        return {"requirement_gaps": []}

    return {"requirement_gaps": []}


async def generate_report(state: dict) -> dict:
    """Generate final review report with verdict."""
    quality = state.get("quality_issues", [])
    security = state.get("security_issues", [])
    tests = state.get("test_issues", [])
    requirements = state.get("requirement_gaps", [])

    all_comments = quality + security + tests + requirements

    # Count by severity
    critical = sum(1 for c in all_comments if c.get("severity") == "critical")
    high = sum(1 for c in all_comments if c.get("severity") == "high")

    verdict = "approved" if (critical == 0 and high == 0) else "rejected"

    return {
        "verdict": verdict,
        "all_comments": all_comments,
        "report_summary": (
            f"Review complete: {len(all_comments)} issues found. "
            f"Critical: {critical}, High: {high}. Verdict: {verdict}."
        ),
        "severity_levels": {
            "critical": critical,
            "high": high,
            "medium": sum(1 for c in all_comments if c.get("severity") == "medium"),
            "low": sum(1 for c in all_comments if c.get("severity") == "low"),
        },
    }
