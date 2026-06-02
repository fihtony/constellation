"""Validation Gates - deterministic checkpoints after LLM outputs.

Each gate is a pure function that verifies LLM output meets format/completeness
requirements. Gates return a ValidationResult indicating pass/fail with details.

Usage:
    result = validate_plan_schema(plan_output)
    if not result.passed:
        # Feed result.feedback back to LLM for retry
        ...
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ValidationResult:
    """Result of a validation gate check."""

    passed: bool
    gate_name: str
    feedback: str = ""  # Human-readable feedback for retry
    details: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Gate: Classification output validation (Step 1)
# ---------------------------------------------------------------------------

VALID_CLASSIFICATIONS = {"development", "office", "general"}


def validate_classification(result: str) -> ValidationResult:
    """Validate LLM classification output is a known enum value."""
    cleaned = result.strip().lower()
    if cleaned in VALID_CLASSIFICATIONS:
        return ValidationResult(passed=True, gate_name="classification")
    return ValidationResult(
        passed=False,
        gate_name="classification",
        feedback=f"Classification must be one of {VALID_CLASSIFICATIONS}, got: '{result}'",
        details={"raw_output": result, "valid_values": list(VALID_CLASSIFICATIONS)},
    )


# ---------------------------------------------------------------------------
# Gate: Requirements analysis schema validation (Step 3)
# ---------------------------------------------------------------------------

ANALYSIS_REQUIRED_FIELDS = {"task_type", "complexity", "skills"}


def validate_analysis_schema(analysis: dict[str, Any]) -> ValidationResult:
    """Validate LLM analysis output has required fields."""
    missing = ANALYSIS_REQUIRED_FIELDS - set(analysis.keys())
    if not missing:
        return ValidationResult(passed=True, gate_name="analysis_schema")
    return ValidationResult(
        passed=False,
        gate_name="analysis_schema",
        feedback=f"Analysis output missing required fields: {missing}",
        details={"missing_fields": list(missing), "provided_fields": list(analysis.keys())},
    )


# ---------------------------------------------------------------------------
# Gate: Readiness validation (Step 3 - after gather_context)
# ---------------------------------------------------------------------------

def validate_readiness(
    *,
    jira_downloaded: bool,
    jira_key_matches: bool,
    repo_cloned: bool,
    repo_non_empty: bool,
    is_ui_task: bool = False,
    design_spec_exists: bool = False,
    tech_stack_identified: bool = False,
    requirements_clarified: bool = True,
) -> ValidationResult:
    """Validate all context is ready before planning/dispatch.

    Returns:
        ValidationResult with details about what's missing.
    """
    checks = {
        "jira_downloaded": jira_downloaded,
        "jira_key_matches": jira_key_matches,
        "repo_cloned": repo_cloned,
        "repo_non_empty": repo_non_empty,
        "tech_stack_identified": tech_stack_identified,
        "requirements_clarified": requirements_clarified,
    }
    if is_ui_task:
        checks["design_spec_exists"] = design_spec_exists

    failed = {k: v for k, v in checks.items() if not v}
    if not failed:
        return ValidationResult(passed=True, gate_name="readiness")

    return ValidationResult(
        passed=False,
        gate_name="readiness",
        feedback=f"Readiness check failed: {list(failed.keys())}",
        details={"checks": checks, "failed": list(failed.keys())},
    )


# ---------------------------------------------------------------------------
# Gate: Plan schema validation (Step 4)
# ---------------------------------------------------------------------------

def validate_plan_schema(plan: Any) -> ValidationResult:
    """Validate LLM plan output has required structure."""
    if not isinstance(plan, dict):
        return ValidationResult(
            passed=False, gate_name="plan_schema",
            feedback="Plan must be a dict with 'steps' array",
        )
    agent_type = plan.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type.strip():
        return ValidationResult(
            passed=False, gate_name="plan_schema",
            feedback="Plan must declare a non-empty 'agent_type' string.",
        )
    steps = plan.get("steps", [])
    if not steps:
        return ValidationResult(
            passed=False, gate_name="plan_schema",
            feedback="Plan must contain at least one step in 'steps' array",
        )
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or "action" not in step:
            return ValidationResult(
                passed=False, gate_name="plan_schema",
                feedback=f"Step {i} must be a dict with 'action' field",
                details={"invalid_step_index": i},
            )
    return ValidationResult(passed=True, gate_name="plan_schema")


# ---------------------------------------------------------------------------
# Gate: Implementation plan / test plan validation (Step 5)
# ---------------------------------------------------------------------------

IMPL_PLAN_REQUIRED = {"implementation_steps", "test_plan"}


def validate_implementation_plan(plan: dict[str, Any]) -> ValidationResult:
    """Validate analyze_task output has structured plan and test plan."""
    missing = IMPL_PLAN_REQUIRED - set(plan.keys())
    if missing:
        return ValidationResult(
            passed=False, gate_name="implementation_plan",
            feedback=f"Implementation plan missing: {missing}. Must include implementation_steps[] and test_plan[].",
        )
    if not plan.get("implementation_steps"):
        return ValidationResult(
            passed=False, gate_name="implementation_plan",
            feedback="implementation_steps must be a non-empty array",
        )
    if not plan.get("test_plan"):
        return ValidationResult(
            passed=False, gate_name="implementation_plan",
            feedback="test_plan must be a non-empty array",
        )
    return ValidationResult(passed=True, gate_name="implementation_plan")


# ---------------------------------------------------------------------------
# Gate: File change verification (Step 5/6 - after implement/fix)
# ---------------------------------------------------------------------------

def _git_env() -> dict[str, str] | None:
    try:
        from framework.env_utils import build_isolated_git_env

        return build_isolated_git_env(scope="validation-gates")
    except Exception:
        return None


def _verified_git_ref(repo_path: str, ref: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", ref],
            cwd=repo_path,
            env=_git_env(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    return ref if result.returncode == 0 else None


def _origin_head_ref(repo_path: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
            cwd=repo_path,
            env=_git_env(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    ref = result.stdout.strip()
    return ref if result.returncode == 0 and ref else None


def _candidate_base_refs(repo_path: str) -> list[str]:
    refs: list[str] = []
    origin_head = _origin_head_ref(repo_path)
    if origin_head:
        refs.append(origin_head)
    refs.extend(["origin/main", "origin/master", "main", "master", "origin/develop", "develop"])

    unique_refs: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        if ref not in seen:
            unique_refs.append(ref)
            seen.add(ref)
    return unique_refs


def _branch_changed_files(repo_path: str) -> tuple[str | None, list[str]]:
    for ref in _candidate_base_refs(repo_path):
        verified_ref = _verified_git_ref(repo_path, ref)
        if not verified_ref:
            continue
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=ACMRTUXB", f"{verified_ref}...HEAD"],
                cwd=repo_path,
                env=_git_env(),
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (subprocess.TimeoutExpired, OSError):
            continue
        if result.returncode != 0:
            continue
        changed_files = sorted({line.strip() for line in result.stdout.splitlines() if line.strip()})
        if changed_files:
            return verified_ref, changed_files
    return None, []


def validate_files_changed(repo_path: str) -> ValidationResult:
    """Verify git working tree or current branch has actual file changes."""
    try:
        result = subprocess.run(
            ["git", "status", "--short"],
            cwd=repo_path,
            env=_git_env(),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        changed_files = [
            line.strip() for line in result.stdout.strip().split("\n")
            if line.strip()
        ]
    except (subprocess.TimeoutExpired, OSError) as exc:
        return ValidationResult(
            passed=False, gate_name="files_changed",
            feedback=f"Could not check git status: {exc}",
        )

    if changed_files:
        return ValidationResult(
            passed=True, gate_name="files_changed",
            details={"changed_files": changed_files[:20], "count": len(changed_files)},
        )

    base_ref, branch_files = _branch_changed_files(repo_path)
    if branch_files:
        return ValidationResult(
            passed=True,
            gate_name="files_changed",
            details={
                "base_ref": base_ref,
                "committed_files": branch_files[:20],
                "count": len(branch_files),
            },
        )

    return ValidationResult(
        passed=False, gate_name="files_changed",
        feedback="No file changes detected after implementation/fix. LLM must produce actual code changes.",
    )


# ---------------------------------------------------------------------------
# Gate: Self-assessment criteria coverage (Step 7)
# ---------------------------------------------------------------------------

def validate_self_assessment(
    assessment: dict[str, Any],
    acceptance_criteria_count: int,
) -> ValidationResult:
    """Validate self-assessment covers all acceptance criteria and score is consistent.

    Checks:
    1. JSON has required fields (score, verdict, criteria_checks)
    2. criteria_checks count >= acceptance_criteria_count
    3. If any criteria_check.status == "not_implemented", score must be < 0.9
    4. score is a float in [0, 1]
    """
    required_fields = {"score", "verdict", "criteria_checks"}
    missing = required_fields - set(assessment.keys())
    if missing:
        return ValidationResult(
            passed=False, gate_name="self_assessment",
            feedback=f"Assessment missing required fields: {missing}",
        )

    score = assessment.get("score")
    if not isinstance(score, (int, float)) or score < 0 or score > 1:
        return ValidationResult(
            passed=False, gate_name="self_assessment",
            feedback=f"Score must be a float in [0, 1], got: {score}",
        )

    criteria_checks = assessment.get("criteria_checks", [])
    if acceptance_criteria_count > 0 and len(criteria_checks) < acceptance_criteria_count:
        return ValidationResult(
            passed=False, gate_name="self_assessment",
            feedback=(
                f"criteria_checks has {len(criteria_checks)} items but Jira has "
                f"{acceptance_criteria_count} acceptance criteria. All must be covered."
            ),
        )

    # Consistency check: not_implemented items must suppress score
    has_not_implemented = any(
        c.get("status") == "not_implemented" for c in criteria_checks
        if isinstance(c, dict)
    )
    if has_not_implemented and score >= 0.9:
        return ValidationResult(
            passed=False, gate_name="self_assessment",
            feedback=(
                f"Score is {score} but some criteria are 'not_implemented'. "
                "Score must be < 0.9 when any criteria is not implemented."
            ),
        )

    return ValidationResult(passed=True, gate_name="self_assessment")


# ---------------------------------------------------------------------------
# Gate: Code review verdict consistency (Step 9)
# ---------------------------------------------------------------------------

def validate_review_verdict(report: dict[str, Any]) -> ValidationResult:
    """Validate code review report has consistent verdict.

    Checks:
    1. Report has 'verdict', 'issues', 'summary' fields
    2. If any issue has severity=critical, verdict must be 'rejected'
    3. Verdict must be 'approved' or 'rejected'
    """
    required = {"verdict", "issues", "summary"}
    missing = required - set(report.keys())
    if missing:
        return ValidationResult(
            passed=False, gate_name="review_verdict",
            feedback=f"Review report missing required fields: {missing}",
        )

    verdict = report.get("verdict", "")
    if verdict not in ("approved", "rejected"):
        return ValidationResult(
            passed=False, gate_name="review_verdict",
            feedback=f"Verdict must be 'approved' or 'rejected', got: '{verdict}'",
        )

    issues = report.get("issues", [])
    has_critical = any(
        i.get("severity") == "critical" for i in issues
        if isinstance(i, dict)
    )
    if has_critical and verdict == "approved":
        return ValidationResult(
            passed=False, gate_name="review_verdict",
            feedback="Critical issues found but verdict is 'approved'. Must be 'rejected'.",
            details={"critical_issues": [i for i in issues if i.get("severity") == "critical"]},
        )

    return ValidationResult(passed=True, gate_name="review_verdict")


# ---------------------------------------------------------------------------
# Gate: PR creation verification (Step 8)
# ---------------------------------------------------------------------------

def validate_pr_created(pr_url: str | None, pr_number: int | None = None) -> ValidationResult:
    """Validate PR was successfully created."""
    if not pr_url or not pr_url.startswith("http"):
        return ValidationResult(
            passed=False, gate_name="pr_created",
            feedback=f"PR URL is missing or invalid: '{pr_url}'",
        )
    return ValidationResult(
        passed=True, gate_name="pr_created",
        details={"pr_url": pr_url, "pr_number": pr_number},
    )


# ---------------------------------------------------------------------------
# Gate: Screenshot upload verification (Step 8 - UI tasks)
# ---------------------------------------------------------------------------

def validate_screenshot_upload(
    screenshot_required: bool,
    screenshot_uploaded: bool,
    screenshot_url: str | None = None,
) -> ValidationResult:
    """Validate screenshot was uploaded if required."""
    if not screenshot_required:
        return ValidationResult(passed=True, gate_name="screenshot_upload")

    if not screenshot_uploaded or not screenshot_url:
        return ValidationResult(
            passed=False, gate_name="screenshot_upload",
            feedback="Screenshot upload is required for UI tasks but failed or URL is missing.",
        )
    return ValidationResult(
        passed=True, gate_name="screenshot_upload",
        details={"screenshot_url": screenshot_url},
    )