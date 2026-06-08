"""Shared review contracts used by development and code-review agents."""

from __future__ import annotations

from typing import Any


REVIEW_ISSUE_SCHEMA = """\
Return a JSON array of issue objects. Each object must have:
- "severity": "critical" | "high" | "medium" | "low"
- "file": filename (or "" if general)
- "line": line number (integer or null)
- "message": clear description of the issue
- "suggestion": concrete fix recommendation

Optional field:
- "blocking": true | false

Severity guidance:
- critical: confirmed exploitable security issue, data loss/corruption risk, auth bypass, or a clear production-breaking defect.
- high: serious issue likely to break a required user flow, violate a hard requirement, or leave a core UI/UX path unusable.
- medium: meaningful but non-blocking issue that should be fixed soon.
- low: minor issue, maintainability note, or non-blocking suggestion.

Prefer medium over high for naming, maintainability, missing non-critical tests, duplication, or small UI fidelity gaps.
Set "blocking": true only when the issue should stop merge/review approval.

Return [] if no issues are found.
Return ONLY the JSON array — no markdown fences, no prose."""


def coerce_review_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return False


def issue_text_blob(issue: dict[str, Any]) -> str:
    parts = [
        str(issue.get("message", "")),
        str(issue.get("suggestion", "")),
        str(issue.get("requirement", "")),
        str(issue.get("category", "")),
    ]
    return " ".join(parts).strip().lower()


def is_design_fidelity_only_issue(issue: dict[str, Any]) -> bool:
    text = issue_text_blob(issue)
    if not text:
        return False

    design_keywords = (
        "design spec",
        "design token",
        "typography",
        "font",
        "color",
        "background",
        "spacing",
        "layout",
        "visual",
        "text-primary",
        "text-on-surface",
        "bg-",
        "tailwind",
        "token",
    )
    functional_keywords = (
        "cannot",
        "unable",
        "broken",
        "fails",
        "missing acceptance",
        "missing requirement",
        "user flow",
        "redirect",
        "submit",
        "select",
        "save",
        "login",
        "route",
        "unusable",
        "regression",
        "does not render",
    )

    return any(keyword in text for keyword in design_keywords) and not any(
        keyword in text for keyword in functional_keywords
    )


def issue_blocks_merge(issue: dict[str, Any]) -> bool:
    severity = str(issue.get("severity", "")).strip().lower()
    if severity == "critical":
        return True
    if severity != "high":
        return False

    source_phase = str(issue.get("source_phase", "")).strip().lower()
    if "blocking" in issue:
        if source_phase == "requirements" and is_design_fidelity_only_issue(issue):
            return False
        return coerce_review_bool(issue.get("blocking"))

    if source_phase in {"review-input", "security"}:
        return True
    if source_phase == "requirements":
        return not is_design_fidelity_only_issue(issue)
    if source_phase == "ui-design":
        return str(issue.get("category", "")).strip().lower() in {
            "icon_rendering",
            "footer_positioning",
            "components",
        }
    return False


def annotate_issue_blocking(issue: dict[str, Any]) -> dict[str, Any]:
    """Return a normalized issue payload with explicit blocking semantics.

    - ``blocking`` remains the original producer field for backward compatibility.
    - ``blocking_requested`` captures the raw requested blocking intent when present.
    - ``effective_blocking`` is the final merge-gate decision used by verdict logic.
    """
    normalized = dict(issue)
    normalized["blocking_requested"] = (
        coerce_review_bool(issue.get("blocking")) if "blocking" in issue else None
    )
    normalized["effective_blocking"] = issue_blocks_merge(normalized)
    return normalized
