"""Code Review Agent workflow nodes.

Design pattern — "Graph outside, ReAct inside":
- Graph drives the deterministic review pipeline (load → quality → security → tests → requirements → report).
- Each review phase uses a single-shot LLM call (runtime.run()) — bounded and auditable.
- Nodes degrade gracefully when no runtime is available (unit-test path).
- Checkpoints are saved after load and after the final report for crash recovery.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path as _Path
from typing import Any

from framework.config import load_agent_config as _load_agent_cfg

# Load own agent_id from config.yaml — single source of truth
_AGENT_ID: str = _load_agent_cfg(
    _Path(__file__).parent.name.replace("_", "-")
).get("agent_id", _Path(__file__).parent.name.replace("_", "-"))

# Cross-agent workspace dir reference — loaded from the corresponding config
_TEAM_LEAD_AGENT_ID: str = _load_agent_cfg("team-lead").get("agent_id", "team-lead")

# Cross-agent workspace dir reference — loaded from the corresponding config
_WEB_DEV_AGENT_ID: str = _load_agent_cfg("web-dev").get("agent_id", "web-dev")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_issue_list(text: str) -> list[dict]:
    """Extract a JSON array of issue objects from LLM response text.

    Returns an empty list when parsing fails.
    """
    # Try direct parse first
    try:
        parsed = json.loads(text.strip())
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass

    # Extract the first JSON array from mixed text
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    return []


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------

async def load_pr_context(state: dict) -> dict:
    """Load PR diff, changed files, description, and Jira/design context.

    In a full deployment the SCM adapter fetches PR data.
    For MVP the calling agent (Team Lead) passes it via metadata.
    Also loads Jira and design context for requirements-aware review.
    """
    metadata = state.get("metadata", {})

    # PR context
    pr_diff = metadata.get("prDiff") or state.get("pr_diff") or ""
    changed_files = metadata.get("changedFiles") or state.get("changed_files") or []
    pr_description = metadata.get("prDescription") or state.get("pr_description") or ""
    commit_messages = metadata.get("commitMessages") or state.get("commit_messages") or []

    # Jira and design context (passed by Team Lead)
    jira_context = metadata.get("jiraContext") or state.get("jira_context") or {}
    design_context = metadata.get("designContext") or state.get("design_context") or {}
    workspace_path = metadata.get("workspacePath") or state.get("workspace_path") or ""
    context_manifest_path = (
        metadata.get("contextManifestPath")
        or state.get("context_manifest_path")
        or ""
    )

    # Extract original requirements from Jira context
    original_requirements = state.get("original_requirements", "")
    if not original_requirements and jira_context:
        fields = jira_context.get("fields", jira_context)
        criteria = fields.get("acceptanceCriteria", [])
        desc = fields.get("description", "")
        if criteria:
            original_requirements = "\n".join(f"- {c}" for c in criteria)
        elif desc:
            original_requirements = desc

    # Write review start log
    if workspace_path:
        review_dir = os.path.join(workspace_path, "code-review")
        checkpoints_dir = os.path.join(review_dir, "review-checkpoints")
        os.makedirs(review_dir, exist_ok=True)
        os.makedirs(checkpoints_dir, exist_ok=True)
        try:
            log_file = os.path.join(review_dir, "task-log.json")
            with open(log_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "code-review",
                        "step": "load_pr_context",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": {
                        "pr_url": metadata.get("prUrl", ""),
                        "changed_files_count": len(changed_files) if isinstance(changed_files, list) else 0,
                        "has_jira_context": bool(jira_context),
                        "has_design_context": bool(design_context),
                    },
                }, fh, ensure_ascii=False, indent=2)

            checkpoint_file = os.path.join(checkpoints_dir, "review-start.json")
            with open(checkpoint_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "checkpoint_id": "CP_REVIEW_STARTED",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "agent_id": "code-review",
                    "state": {
                        "pr_url": metadata.get("prUrl", ""),
                        "workspace_path": workspace_path,
                        "context_manifest_path": context_manifest_path,
                        "has_jira_context": bool(jira_context),
                        "has_design_context": bool(design_context),
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    return {
        "pr_diff": pr_diff,
        "changed_files": changed_files if isinstance(changed_files, list) else [changed_files],
        "pr_description": pr_description,
        "commit_messages": commit_messages,
        "jira_context": jira_context,
        "design_context": design_context,
        "original_requirements": original_requirements,
        "workspace_path": workspace_path,
        "context_manifest_path": context_manifest_path,
    }


async def review_quality(state: dict) -> dict:
    """Check code quality, style, and patterns using a single-shot LLM call."""
    runtime = state.get("_runtime")

    if not runtime or not state.get("pr_diff"):
        return {"quality_issues": []}

    from agents.code_review.prompts import QUALITY_SYSTEM, QUALITY_TEMPLATE

    prompt = QUALITY_TEMPLATE.format(
        pr_description=state.get("pr_description", "N/A"),
        changed_files=", ".join(state.get("changed_files", [])) or "N/A",
        pr_diff=state.get("pr_diff", ""),
    )
    result = runtime.run(prompt, system_prompt=QUALITY_SYSTEM, max_tokens=2048,
                         plugin_manager=state.get("_plugin_manager"))
    issues = _parse_issue_list(result.get("raw_response", ""))

    return {"quality_issues": issues}


async def review_security(state: dict) -> dict:
    """Check for security vulnerabilities (OWASP Top 10) using a single-shot LLM call."""
    runtime = state.get("_runtime")

    if not runtime or not state.get("pr_diff"):
        return {"security_issues": []}

    from agents.code_review.prompts import SECURITY_SYSTEM, SECURITY_TEMPLATE

    prompt = SECURITY_TEMPLATE.format(
        pr_description=state.get("pr_description", "N/A"),
        changed_files=", ".join(state.get("changed_files", [])) or "N/A",
        pr_diff=state.get("pr_diff", ""),
    )
    result = runtime.run(prompt, system_prompt=SECURITY_SYSTEM, max_tokens=2048,
                         plugin_manager=state.get("_plugin_manager"))
    issues = _parse_issue_list(result.get("raw_response", ""))

    return {"security_issues": issues}


async def review_tests(state: dict) -> dict:
    """Check test coverage and test quality using a single-shot LLM call."""
    runtime = state.get("_runtime")

    if not runtime or not state.get("pr_diff"):
        return {"test_issues": []}

    from agents.code_review.prompts import TESTS_SYSTEM, TESTS_TEMPLATE

    prompt = TESTS_TEMPLATE.format(
        pr_description=state.get("pr_description", "N/A"),
        changed_files=", ".join(state.get("changed_files", [])) or "N/A",
        pr_diff=state.get("pr_diff", ""),
    )
    result = runtime.run(prompt, system_prompt=TESTS_SYSTEM, max_tokens=2048,
                         plugin_manager=state.get("_plugin_manager"))
    issues = _parse_issue_list(result.get("raw_response", ""))

    return {"test_issues": issues}


async def review_requirements(state: dict) -> dict:
    """Check requirements compliance against Jira acceptance criteria."""
    runtime = state.get("_runtime")

    if not runtime or not state.get("pr_diff"):
        return {"requirement_gaps": []}

    original_requirements = state.get("original_requirements", "")
    if not original_requirements:
        return {"requirement_gaps": []}

    from agents.code_review.prompts import REQUIREMENTS_SYSTEM, REQUIREMENTS_TEMPLATE

    jira_ctx = state.get("jira_context", {})
    prompt = REQUIREMENTS_TEMPLATE.format(
        original_requirements=original_requirements,
        jira_context=json.dumps(jira_ctx, ensure_ascii=False) if jira_ctx else "N/A",
        pr_description=state.get("pr_description", "N/A"),
        changed_files=", ".join(state.get("changed_files", [])) or "N/A",
        pr_diff=state.get("pr_diff", ""),
    )
    result = runtime.run(prompt, system_prompt=REQUIREMENTS_SYSTEM, max_tokens=2048,
                         plugin_manager=state.get("_plugin_manager"))
    issues = _parse_issue_list(result.get("raw_response", ""))

    return {"requirement_gaps": issues}


async def generate_report(state: dict) -> dict:
    """Aggregate all review phases and produce a final verdict.

    Pure Python — no LLM call needed.
    Verdict: "approved" only when there are zero critical or high severity issues.
    Writes review-report.json to the workspace for audit.
    """
    quality = state.get("quality_issues", [])
    security = state.get("security_issues", [])
    tests = state.get("test_issues", [])
    requirements = state.get("requirement_gaps", [])

    all_comments = quality + security + tests + requirements

    # Count by severity
    critical = sum(1 for c in all_comments if c.get("severity") == "critical")
    high = sum(1 for c in all_comments if c.get("severity") == "high")
    medium = sum(1 for c in all_comments if c.get("severity") == "medium")
    low = sum(1 for c in all_comments if c.get("severity") == "low")

    verdict = "approved" if (critical == 0 and high == 0) else "rejected"

    summary_parts = [
        f"Review complete: {len(all_comments)} issue(s) found.",
        f"Critical: {critical}, High: {high}, Medium: {medium}, Low: {low}.",
        f"Verdict: {verdict}.",
    ]

    report = {
        "verdict": verdict,
        "all_comments": all_comments,
        "severity_levels": {
            "critical": critical,
            "high": high,
            "medium": medium,
            "low": low,
        },
        "checked_artifacts": [
            p for p in [
                f"{_TEAM_LEAD_AGENT_ID}/jira-ticket.json" if state.get("jira_context") else "",
                f"{_TEAM_LEAD_AGENT_ID}/design-spec.json" if state.get("design_context") else "",
                state.get("context_manifest_path", ""),
                f"{_WEB_DEV_AGENT_ID}/self-assessment.json",
                f"{_WEB_DEV_AGENT_ID}/pr-evidence.json",
            ] if p
        ],
    }

    # Write review report to workspace
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        review_dir = os.path.join(workspace_path, "code-review")
        checkpoints_dir = os.path.join(review_dir, "review-checkpoints")
        os.makedirs(review_dir, exist_ok=True)
        os.makedirs(checkpoints_dir, exist_ok=True)
        try:
            report_file = os.path.join(review_dir, "review-report.json")
            with open(report_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "code-review",
                        "step": "generate_report",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": report,
                }, fh, ensure_ascii=False, indent=2)

            checkpoint_file = os.path.join(checkpoints_dir, "review-summary.json")
            with open(checkpoint_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "checkpoint_id": "CP_REVIEW_SUMMARIZED",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "agent_id": "code-review",
                    "state": {
                        "verdict": verdict,
                        "severity_levels": report["severity_levels"],
                        "checked_artifacts": report["checked_artifacts"],
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    return {
        "verdict": verdict,
        "all_comments": all_comments,
        "report_summary": " ".join(summary_parts),
        "severity_levels": report["severity_levels"],
        "checked_artifacts": report["checked_artifacts"],
    }

