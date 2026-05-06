"""Validation and evidence tools for execution agents.

Provides the standard interface for running validation checks and collecting
evidence artifacts. Concrete implementations are registered by each agent's
validation provider.

Section 6.6–6.7 of the Constellation agentic redesign specification.
Self-registers on import.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any

from common.tools.base import ConstellationTool, ToolSchema
from common.tools.registry import register_tool

# ---------------------------------------------------------------------------
# Validation result data model
# ---------------------------------------------------------------------------

class ValidationResult:
    """Structured result returned by all validation tools."""

    def __init__(
        self,
        *,
        passed: bool,
        summary: str,
        details: list[dict] | None = None,
        evidence_paths: list[str] | None = None,
        retriable: bool = True,
        suggested_fix: str | None = None,
    ) -> None:
        self.passed = passed
        self.summary = summary
        self.details: list[dict] = details or []
        self.evidence_paths: list[str] = evidence_paths or []
        self.retriable = retriable
        self.suggested_fix = suggested_fix

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "summary": self.summary,
            "details": self.details,
            "evidencePaths": self.evidence_paths,
            "retriable": self.retriable,
            "suggestedFix": self.suggested_fix,
        }


# ---------------------------------------------------------------------------
# Agent-side validation provider registry
# ---------------------------------------------------------------------------

_validation_provider: Any | None = None


def register_validation_provider(provider: Any) -> None:
    """Register an agent-specific validation provider.

    The provider must implement:
        run_build(workspace_path, options) -> ValidationResult
        run_unit_test(workspace_path, options) -> ValidationResult
        run_integration_test(workspace_path, options) -> ValidationResult
        run_lint(workspace_path, options) -> ValidationResult
        run_e2e(workspace_path, options) -> ValidationResult
    """
    global _validation_provider
    _validation_provider = provider


def _default_run_command(
    cmd: str, cwd: str, options: dict
) -> ValidationResult:
    """Run a shell command and return a ValidationResult."""
    timeout = int(options.get("timeout", 300))
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        passed = result.returncode == 0
        output = (result.stdout or "") + (result.stderr or "")
        snippet = output[-3000:] if len(output) > 3000 else output
        return ValidationResult(
            passed=passed,
            summary=f"Command {'succeeded' if passed else 'failed'}: {cmd}",
            details=[{"check_name": "command", "status": "passed" if passed else "failed", "output_snippet": snippet}],
            retriable=not passed,
        )
    except subprocess.TimeoutExpired:
        return ValidationResult(
            passed=False,
            summary=f"Command timed out after {timeout}s: {cmd}",
            retriable=True,
            suggested_fix="Increase timeout or investigate hanging process.",
        )
    except Exception as exc:  # noqa: BLE001
        return ValidationResult(
            passed=False,
            summary=f"Command execution error: {exc}",
            retriable=False,
        )


# ---------------------------------------------------------------------------
# run_validation_command
# ---------------------------------------------------------------------------

class RunValidationCommandTool(ConstellationTool):
    """Run validation checks appropriate for the current project tech stack."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="run_validation_command",
            description=(
                "Run validation checks (build, unit_test, integration_test, lint, e2e, or custom) "
                "appropriate for the current project tech stack. "
                "Returns a structured ValidationResult with pass/fail status, details, "
                "and evidence file paths."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "validation_type": {
                        "type": "string",
                        "description": (
                            "Type of validation to run: "
                            "build | unit_test | integration_test | lint | e2e | custom"
                        ),
                        "enum": ["build", "unit_test", "integration_test", "lint", "e2e", "custom"],
                    },
                    "target_path": {
                        "type": "string",
                        "description": "Optional specific file or directory to validate.",
                    },
                    "command": {
                        "type": "string",
                        "description": "Custom command to run when validation_type is 'custom'.",
                    },
                    "options": {
                        "type": "object",
                        "description": "Optional key-value pairs for validation configuration (e.g. timeout).",
                    },
                },
                "required": ["validation_type"],
            },
        )

    def execute(self, args: dict) -> dict:
        validation_type = str(args.get("validation_type") or "").strip()
        target_path = str(args.get("target_path") or "").strip()
        command = str(args.get("command") or "").strip()
        options = dict(args.get("options") or {})
        cwd = target_path if target_path and os.path.isdir(target_path) else os.getcwd()

        if validation_type not in {"build", "unit_test", "integration_test", "lint", "e2e", "custom"}:
            return self.error(f"Invalid validation_type '{validation_type}'.")

        if _validation_provider is not None:
            method_name = f"run_{validation_type}"
            method = getattr(_validation_provider, method_name, None)
            if method is not None:
                try:
                    result = method(cwd, options)
                    return self.ok(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
                except Exception as exc:  # noqa: BLE001
                    return self.error(f"Validation provider error: {exc}")

        # Generic fallback: run custom command directly
        if validation_type == "custom":
            if not command:
                return self.error("'command' is required when validation_type is 'custom'.")
            result = _default_run_command(command, cwd, options)
            return self.ok(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))

        return self.ok(
            json.dumps(
                ValidationResult(
                    passed=False,
                    summary=(
                        f"No validation provider registered for '{validation_type}'. "
                        "Use validation_type='custom' with an explicit command."
                    ),
                    retriable=False,
                ).to_dict(),
                ensure_ascii=False,
                indent=2,
            )
        )


# ---------------------------------------------------------------------------
# collect_task_evidence
# ---------------------------------------------------------------------------

class CollectTaskEvidenceTool(ConstellationTool):
    """Collect logs, diffs, screenshots, and artifact paths as task evidence."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="collect_task_evidence",
            description=(
                "Collect task evidence including logs, diffs, screenshots, and artifact file paths. "
                "Returns a structured evidence manifest that can be included in task artifacts."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "workspace_path": {
                        "type": "string",
                        "description": "Shared workspace path to scan for evidence files.",
                    },
                    "evidence_types": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Types of evidence to collect: logs | diffs | screenshots | artifacts | all. "
                            "Defaults to all."
                        ),
                    },
                    "agent_id": {
                        "type": "string",
                        "description": "Agent ID whose subdirectory to scan. Defaults to current agent.",
                    },
                },
                "required": [],
            },
        )

    def execute(self, args: dict) -> dict:
        workspace_path = str(args.get("workspace_path") or "").strip()
        evidence_types = list(args.get("evidence_types") or ["all"])
        agent_id = str(args.get("agent_id") or os.environ.get("AGENT_ID", "")).strip()

        collect_all = "all" in evidence_types
        collect_logs = collect_all or "logs" in evidence_types
        collect_diffs = collect_all or "diffs" in evidence_types
        collect_screenshots = collect_all or "screenshots" in evidence_types
        collect_artifacts = collect_all or "artifacts" in evidence_types

        evidence: dict[str, list[str]] = {
            "logs": [],
            "diffs": [],
            "screenshots": [],
            "artifacts": [],
        }

        search_root = workspace_path
        if agent_id and workspace_path:
            agent_dir = os.path.join(workspace_path, agent_id)
            if os.path.isdir(agent_dir):
                search_root = agent_dir

        if not search_root or not os.path.isdir(search_root):
            return self.ok(
                json.dumps(
                    {"evidence": evidence, "note": "No workspace directory found."},
                    ensure_ascii=False,
                    indent=2,
                )
            )

        for dirpath, _dirnames, filenames in os.walk(search_root):
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                lower = fname.lower()
                if collect_logs and (lower.endswith(".log") or lower.endswith(".txt") or "log" in lower):
                    evidence["logs"].append(fpath)
                if collect_diffs and lower.endswith(".diff") or lower.endswith(".patch"):
                    evidence["diffs"].append(fpath)
                if collect_screenshots and (lower.endswith(".png") or lower.endswith(".jpg") or lower.endswith(".webp")):
                    evidence["screenshots"].append(fpath)
                if collect_artifacts and lower.endswith(".json"):
                    evidence["artifacts"].append(fpath)

        return self.ok(
            json.dumps(
                {
                    "evidence": evidence,
                    "workspacePath": workspace_path,
                    "agentId": agent_id,
                    "collectedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
                },
                ensure_ascii=False,
                indent=2,
            )
        )


# ---------------------------------------------------------------------------
# check_definition_of_done
# ---------------------------------------------------------------------------

class CheckDefinitionOfDoneTool(ConstellationTool):
    """Check task completion against a Definition of Done (DoD) checklist."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="check_definition_of_done",
            description=(
                "Check the current task's completion status against a Definition of Done checklist. "
                "Each checklist item is evaluated as met or unmet. "
                "Returns an overall pass/fail and a detailed breakdown."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "checklist": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "item": {"type": "string", "description": "DoD checklist item description."},
                                "met": {"type": "boolean", "description": "Whether this item is met."},
                                "note": {"type": "string", "description": "Optional note or evidence."},
                            },
                            "required": ["item", "met"],
                        },
                        "description": "List of DoD checklist items with their met/unmet status.",
                    },
                    "task_summary": {
                        "type": "string",
                        "description": "Optional brief summary of what was accomplished.",
                    },
                },
                "required": ["checklist"],
            },
        )

    def execute(self, args: dict) -> dict:
        checklist = list(args.get("checklist") or [])
        task_summary = str(args.get("task_summary") or "").strip()

        if not checklist:
            return self.error("checklist must not be empty.")

        total = len(checklist)
        met_count = sum(1 for item in checklist if item.get("met", False))
        unmet = [item for item in checklist if not item.get("met", False)]
        all_done = met_count == total

        result = {
            "passed": all_done,
            "metCount": met_count,
            "totalCount": total,
            "unmetItems": unmet,
            "summary": task_summary or (
                "All DoD criteria met." if all_done
                else f"{len(unmet)} of {total} DoD items not yet met."
            ),
            "checklist": checklist,
        }

        return self.ok(json.dumps(result, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# summarize_failure_context
# ---------------------------------------------------------------------------

class SummarizeFailureContextTool(ConstellationTool):
    """Produce a structured failure summary with root cause and next steps."""

    @property
    def schema(self) -> ToolSchema:
        return ToolSchema(
            name="summarize_failure_context",
            description=(
                "Produce a structured failure analysis: root cause, affected components, "
                "and recommended next steps. Use this when a task or validation fails to "
                "give the orchestrator and user clear actionable context."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "failure_description": {
                        "type": "string",
                        "description": "Human-readable description of what failed.",
                    },
                    "error_output": {
                        "type": "string",
                        "description": "Raw error output, logs, or stack trace (optional, truncated if large).",
                    },
                    "affected_components": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of files, modules, or services affected.",
                    },
                    "suggested_next_steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Ordered list of recommended recovery or escalation steps.",
                    },
                    "retriable": {
                        "type": "boolean",
                        "description": "Whether the failure is likely recoverable by retrying.",
                    },
                },
                "required": ["failure_description"],
            },
        )

    def execute(self, args: dict) -> dict:
        failure_description = str(args.get("failure_description") or "").strip()
        error_output = str(args.get("error_output") or "").strip()
        affected_components = list(args.get("affected_components") or [])
        suggested_next_steps = list(args.get("suggested_next_steps") or [])
        retriable = bool(args.get("retriable", True))

        if not failure_description:
            return self.error("failure_description is required.")

        # Truncate very long error output
        if len(error_output) > 4000:
            error_output = error_output[:4000] + "\n... [truncated]"

        summary = {
            "failureDescription": failure_description,
            "errorOutput": error_output,
            "affectedComponents": affected_components,
            "suggestedNextSteps": suggested_next_steps,
            "retriable": retriable,
            "summarizedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }

        return self.ok(json.dumps(summary, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Self-register all validation tools
# ---------------------------------------------------------------------------

register_tool(RunValidationCommandTool())
register_tool(CollectTaskEvidenceTool())
register_tool(CheckDefinitionOfDoneTool())
register_tool(SummarizeFailureContextTool())
