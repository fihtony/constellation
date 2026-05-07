"""Compass completeness gate helpers.

Extracted from compass/app.py to keep the HTTP handler thin.

These functions check whether a downstream Team Lead deliverable satisfies
Compass completion criteria, and build follow-up messages for revision cycles.

All evidence is read from A2A artifacts delivered by Team Lead via callback —
never from the shared workspace filesystem directly.  Execution-agent workspace
files (pr-evidence.json, jira-actions.json, stage-summary.json) are internal
to the Team Lead ↔ dev-agent pipeline and must not be accessed directly by
Compass.
"""

from __future__ import annotations

import json
import os


# ---------------------------------------------------------------------------
# Evidence extraction
# ---------------------------------------------------------------------------

def extract_pr_evidence_from_artifacts(artifacts: list[dict]) -> dict:
    """Extract PR evidence (URL, branch, jiraInReview) from A2A artifacts.

    Returns a dict with keys: url, branch, jiraInReview.
    Returns an empty dict if no PR evidence is found.
    """
    for artifact in artifacts or []:
        metadata = artifact.get("metadata") or {}
        pr_url = metadata.get("prUrl") or metadata.get("url") or ""
        branch = metadata.get("branch") or ""
        if pr_url:
            return {
                "url": pr_url,
                "branch": branch,
                "jiraInReview": bool(metadata.get("jiraInReview", False)),
            }
    return {}


def _read_workspace_json(workspace_path: str, relative_path: str) -> dict:
    if not workspace_path:
        return {}
    full_path = os.path.join(workspace_path, relative_path)
    if not os.path.isfile(full_path):
        return {}
    try:
        with open(full_path, encoding="utf-8") as fh:
            payload = json.load(fh)
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Completeness gate
# ---------------------------------------------------------------------------

def extract_team_lead_completeness_issues(
    workspace_path: str,
    artifacts: list[dict],
) -> list[str]:
    """Check whether Team Lead's deliverable satisfies Compass completion criteria.

    Returns a list of issue strings.  An empty list means no issues — the task
    can be marked complete.

    Parameters
    ----------
    workspace_path:
        The shared workspace path for this task.  Used to read Team Lead's own
        output files (``team-lead/plan.json``, ``team-lead/stage-summary.json``).
        Execution-agent subdirectories are intentionally never read here.
    artifacts:
        A2A artifacts delivered by Team Lead via callback.
    """
    issues: list[str] = []

    # Find the Team Lead summary artifact
    summary_artifact: dict | None = None
    for artifact in artifacts or []:
        metadata = artifact.get("metadata") or {}
        if metadata.get("capability") == "team-lead.task.analyze":
            summary_artifact = artifact
            break
    summary_meta = (summary_artifact or {}).get("metadata") or {}

    # Intentional pre-dispatch stop — not a completeness failure.
    if summary_meta.get("validationCheckpoint"):
        return []

    # Team Lead exhausted review cycles and deliberately accepted the output.
    if summary_meta.get("reviewMaxCyclesReached"):
        print("[compass] Team Lead reached max review cycles and accepted with issues — skipping retry.")
        return []

    if summary_meta.get("reviewPassed") is False:
        issues.append("Team Lead review did not pass.")

    if summary_meta.get("reviewPassed") is True:
        # Team Lead reviewed and approved — trust the review completely.
        return []

    # reviewPassed is None: fall back to artifact-based evidence checks.
    if workspace_path:
        team_lead_plan = _read_workspace_json(workspace_path, "team-lead/plan.json")
        team_lead_stage = _read_workspace_json(workspace_path, "team-lead/stage-summary.json")
        analysis = (
            team_lead_stage.get("analysis")
            if isinstance(team_lead_stage.get("analysis"), dict)
            else {}
        )
        target_repo_url = (
            team_lead_plan.get("target_repo_url")
            or analysis.get("target_repo_url")
            or ""
        ).strip()

        if target_repo_url:
            pr_evidence = extract_pr_evidence_from_artifacts(artifacts)
            if not (pr_evidence.get("url") or pr_evidence.get("prUrl")):
                issues.append("Pull request URL is missing from execution agent artifacts.")
            if not pr_evidence.get("branch"):
                issues.append("Branch name is missing from execution agent artifacts.")

    return issues


# ---------------------------------------------------------------------------
# Follow-up message builder
# ---------------------------------------------------------------------------

def build_completeness_follow_up_message(
    original_message: dict,
    issues: list[str],
    revision_cycle: int,
) -> dict:
    """Build a revised A2A message for a Compass completeness retry.

    Parameters
    ----------
    original_message:
        The original task message sent to Team Lead.
    issues:
        List of completeness issue strings from ``extract_team_lead_completeness_issues``.
    revision_cycle:
        The 1-based revision number (used in the appended text).
    """
    import copy
    message = copy.deepcopy(original_message)

    base_text = ""
    for part in (message.get("parts") or []):
        base_text += str(part.get("text") or "")
    base_text = base_text.strip()

    issue_lines = "\n".join(f"- {issue}" for issue in issues)
    follow_up = (
        f"Compass completeness check revision {revision_cycle} found unresolved gaps:\n"
        f"{issue_lines}\n\n"
        "Continue from the existing shared workspace, preserve prior work, "
        "and use only registered boundary agents."
    )
    combined = (base_text + "\n\n" + follow_up).strip()
    message["parts"] = [{"text": combined}]

    metadata = dict(message.get("metadata") or {})
    metadata["compassCompletenessRevision"] = revision_cycle
    metadata["completenessIssues"] = issues
    message["metadata"] = metadata
    return message


# ---------------------------------------------------------------------------
# Task card status helper
# ---------------------------------------------------------------------------

def derive_task_card_status(
    task_state: str,
    pr_evidence: dict,
) -> tuple[str, str]:
    """Return (status_kind, status_label) for the Compass task card UI.

    Parameters
    ----------
    task_state:
        Current task state string.
    pr_evidence:
        Dict with ``url``, ``branch``, ``jiraInReview`` from
        ``extract_pr_evidence_from_artifacts``.
    """
    failed_states = {
        "TASK_STATE_FAILED",
        "FAILED",
        "NO_CAPABLE_AGENT",
        "CAPABILITY_TEMPORARILY_UNAVAILABLE",
        "POLICY_DENIED",
        "CAPACITY_EXHAUSTED",
    }
    if task_state == "TASK_STATE_INPUT_REQUIRED":
        return "waiting_for_info", "Waiting for Info"
    if task_state in failed_states:
        return "failed", "Failed"
    if task_state == "TASK_STATE_COMPLETED":
        pr_url = pr_evidence.get("url") or pr_evidence.get("prUrl") or ""
        if pr_url:
            if pr_evidence.get("jiraInReview"):
                return "completed", "Completed / In Review"
            return "completed", "Completed / PR Raised"
        return "completed", "Completed"
    return "in_progress", "In Progress"
