"""Deterministic plan-output gate for the Office agent.

The gate compares the materialized output tree of an Office task against
the plan artifact for the capability. It is pure: no network, no LLM calls,
no capability-specific hardcoded data.

Capabilities
------------
* ``organize``  — plan artifact: ``organization-plan.md``; root: organized-output/files/
* ``summarize`` — plan artifact: ``summary-plan.md``; root: workspace write dir
* ``analyze``   — plan artifact: ``analysis-plan.md``; root: workspace write dir
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateEntry:
    source_path: str
    expected_path: str
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutputContract:
    capability: str
    plan_path: str
    output_root: str
    ancillary_allowlist: frozenset[str]
    source_count: int
    expected_plan_kind: str


@dataclass(frozen=True)
class GateReport:
    capability: str
    plan_status: str               # ok | missing | unparseable | invalid
    planned_count: int
    actual_count: int
    missing: list[str]
    unexpected: list[str]
    mismatches: list[str]
    invalid_plan_entries: list[str] = field(default_factory=list)
    error_message: str = ""
    tool_unavailable: bool = False

    @property
    def is_clean(self) -> bool:
        return (
            self.plan_status == "ok"
            and not self.missing
            and not self.unexpected
            and not self.mismatches
            and not self.invalid_plan_entries
            and not self.error_message
        )


# ---------------------------------------------------------------------------
# Ancillary allowlist
# ---------------------------------------------------------------------------

_ANCILLARY_BASENAMES: dict[str, frozenset[str]] = {
    "organize": frozenset(
        {
            "organization-plan.md",
            "plan-output-gate-report.json",
            "task-report.json",
            "warnings.md",
            "agentic-output.txt",
        }
    ),
    "summarize": frozenset(
        {
            "summary-plan.md",
            "combined-summary.md",
            "plan-output-gate-report.json",
            "task-report.json",
            "warnings.md",
            "agentic-output.txt",
        }
    ),
    "analyze": frozenset(
        {
            "analysis-plan.md",
            "plan-output-gate-report.json",
            "task-report.json",
            "warnings.md",
            "agentic-output.txt",
        }
    ),
}

_PLAN_FILENAME = {
    "organize": "organization-plan.md",
    "summarize": "summary-plan.md",
    "analyze": "analysis-plan.md",
}

_PLAN_KIND = {
    "organize": "files_organized",
    "summarize": "source_summary_mapping",
    "analyze": "source_analysis_mapping",
}


def _inplace_target_dir(capability: str, source_paths: list[str]) -> str:
    """Return the resolved target directory for inplace mode.

    For organize, the target is the first source folder's organized-output/files/.
    For summarize/analyze, the target is the parent of the first source file.
    """
    if capability == "organize":
        first = next((p for p in source_paths if p and os.path.isdir(p)), source_paths[0])
        return os.path.join(first, "organized-output", "files")
    first = source_paths[0]
    return os.path.dirname(first)


def resolve_output_contract(
    capability: str,
    validated_paths: list[str],
    output_mode: str,
    artifacts_dir: str,
) -> OutputContract:
    """Resolve the gate's contract for a single Office task.

    Centralized here so the gate does not branch on path layout in
    multiple places.
    """
    if capability not in _PLAN_FILENAME:
        raise ValueError(f"unknown capability {capability!r}")
    plan_filename = _PLAN_FILENAME[capability]

    if output_mode == "inplace":
        if not validated_paths:
            raise ValueError("inplace mode requires validated_paths")
        output_root = _inplace_target_dir(capability, validated_paths)
        plan_path = os.path.join(output_root, plan_filename)
    else:
        # workspace mode
        workspace_root = artifacts_dir or os.environ.get(
            "OFFICE_WORKSPACE_ROOT", ""
        )
        if capability == "organize":
            output_root = os.path.join(workspace_root, "organized-output", "files")
        else:
            output_root = workspace_root
        plan_path = os.path.join(output_root, plan_filename)

    return OutputContract(
        capability=capability,
        plan_path=plan_path,
        output_root=output_root,
        ancillary_allowlist=_ANCILLARY_BASENAMES[capability],
        source_count=len(validated_paths),
        expected_plan_kind=_PLAN_KIND[capability],
    )
