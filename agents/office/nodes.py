"""Office agent workflow nodes.

receive_task     — Parse task message: capability, source paths, output mode
analyze_request — Validate paths, check permissions, load skill prompts
execute_office_work — ReAct core: runtime.run_agentic() with office tools
report_result   — Write pr-evidence.json, return result
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from agents.office.office_tools import (
    _check_directory_limits,
)

logger = logging.getLogger(__name__)

AGENT_ID = "office"


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def _get_workspace_root(state: dict) -> str:
    """Get the workspace root for this task."""
    artifact_root = os.environ.get(
        "ARTIFACT_ROOT",
        os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "artifacts")
    )
    compass_id = state.get("_compass_task_id", "default")
    task_id = state.get("_task_id", "office")
    return os.path.join(artifact_root, compass_id, task_id, "office")


def _validate_source_path(path: str) -> tuple[str, str]:
    """Validate path is within OFFICE_SOURCE_ROOT."""
    source_root = os.environ.get("OFFICE_SOURCE_ROOT", "/")
    real_path = os.path.realpath(os.path.abspath(path))
    real_root = os.path.realpath(os.path.abspath(source_root))
    if not real_path.startswith(real_root + os.sep):
        return "", f"Path {path!r} is outside OFFICE_SOURCE_ROOT ({source_root})"
    return real_path, ""


# ---------------------------------------------------------------------------
# Capability parsing
# ---------------------------------------------------------------------------

def _parse_capability(text: str) -> str:
    """Infer capability from task text."""
    text_lower = text.lower()
    if "analyze" in text_lower and ("csv" in text_lower or "data" in text_lower or "sales" in text_lower):
        return "analyze"
    if "organize" in text_lower or "folder" in text_lower:
        return "organize"
    return "summarize"


def _extract_paths(text: str) -> list[str]:
    """Extract file/folder paths from task text."""
    # Match absolute Unix paths
    paths = re.findall(r'(?:^|\s)((?:/[a-zA-Z0-9_.~-]+)+)', text)
    # Also match relative paths
    rel_paths = re.findall(r'(?:^|\s)([a-zA-Z0-9_./-]*(?:tests?/data[/\w.-]+)[a-zA-Z0-9_./-]*)', text)
    all_paths = paths + rel_paths
    return [p.strip() for p in all_paths if len(p.strip()) > 3]


# ---------------------------------------------------------------------------
# Node: receive_task
# ---------------------------------------------------------------------------

def receive_task(state: dict) -> dict:
    """Parse the incoming task message to extract capability, paths, and output mode."""
    user_text = state.get("user_request", "")

    # Parse output mode
    output_mode = "workspace"
    if "inplace" in user_text.lower():
        output_mode = "inplace"

    # Parse capability
    capability = _parse_capability(user_text)

    # Parse source paths
    source_paths = _extract_paths(user_text)

    logger.info(f"receive_task: capability={capability} output_mode={output_mode} paths={source_paths}")

    return {
        "capability": capability,
        "output_mode": output_mode,
        "source_paths": source_paths,
    }


# ---------------------------------------------------------------------------
# Node: analyze_request
# ---------------------------------------------------------------------------

def analyze_request(state: dict) -> dict:
    """Validate source paths and check permissions. Returns updated state or error."""
    source_paths = state.get("source_paths", [])
    output_mode = state.get("output_mode", "workspace")
    capability = state.get("capability", "summarize")

    if not source_paths:
        return {"error": "No source paths found in task. Please provide file or folder paths."}

    validated_paths = []
    for p in source_paths:
        normalized, err = _validate_source_path(p)
        if normalized:
            validated_paths.append(normalized)
        else:
            logger.warning(f"Skipping invalid path: {p} — {err}")

    if not validated_paths and capability not in ("summarize", "organize"):
        return {"error": "No valid paths found under OFFICE_SOURCE_ROOT."}

    # Directory resource pre-check for organize capability
    if capability == "organize" and validated_paths:
        # Check first path (assuming single directory for organize)
        first_path = validated_paths[0]
        if os.path.isdir(first_path):
            limit_error = _check_directory_limits(first_path)
            if limit_error:
                return limit_error

    workspace_root = _get_workspace_root(state)
    os.makedirs(workspace_root, exist_ok=True)
    artifacts_dir = os.path.join(workspace_root, "artifacts")
    os.makedirs(artifacts_dir, exist_ok=True)

    # Check inplace permission
    if output_mode == "inplace":
        allow_inplace = os.environ.get("OFFICE_ALLOW_INPLACE_WRITES", "false").lower()
        if allow_inplace not in ("true", "1", "yes"):
            logger.warning("inplace mode requested but OFFICE_ALLOW_INPLACE_WRITES not set — falling back to workspace")
            state["output_mode"] = "workspace"

    logger.info(f"analyze_request: validated_paths={validated_paths} artifacts_dir={artifacts_dir}")

    return {
        "validated_paths": validated_paths,
        "workspace_root": workspace_root,
        "artifacts_dir": artifacts_dir,
    }


# ---------------------------------------------------------------------------
# System prompt loader
# ---------------------------------------------------------------------------

def _load_system_prompt() -> str:
    """Load the office agent system prompt."""
    prompt_path = os.path.join(os.path.dirname(__file__), "prompts", "system.md")
    if os.path.exists(prompt_path):
        with open(prompt_path, encoding="utf-8") as fh:
            return fh.read()
    return (
        "You are an expert office task agent. "
        "Use the read_pdf, read_docx, read_txt, read_csv tools to read files. "
        "Use write_workspace to save results to the workspace artifacts folder. "
        "Be concise and thorough."
    )


# ---------------------------------------------------------------------------
# Node: execute_office_work
# ---------------------------------------------------------------------------

def execute_office_work(state: dict) -> dict:
    """ReAct core: call runtime.run_agentic() with office tools to do the actual work."""
    runtime = state.get("_runtime")
    if not runtime:
        return {"error": "No runtime configured"}

    capability = state.get("capability", "summarize")
    validated_paths = state.get("validated_paths", [])
    artifacts_dir = state.get("artifacts_dir", "")
    output_mode = state.get("output_mode", "workspace")
    source_root = os.environ.get("OFFICE_SOURCE_ROOT", "")

    if capability == "organize":
        prompt = _build_organize_prompt(validated_paths, output_mode, source_root)
    elif capability == "summarize":
        prompt = _build_summarize_prompt(validated_paths, output_mode, source_root)
    elif capability == "analyze":
        prompt = _build_analyze_prompt(validated_paths, output_mode, source_root)
    else:
        return {"error": f"Capability {capability} not implemented."}

    # Set workspace root env for write_workspace tool
    if artifacts_dir:
        os.environ["OFFICE_WORKSPACE_ROOT"] = artifacts_dir

    logger.info(f"execute_office_work: running {capability} with {len(validated_paths)} paths")

    # Get tool schemas from registry
    from framework.tools.registry import get_registry
    registry = get_registry()
    tool_names = ["read_pdf", "read_docx", "read_txt", "read_csv", "read_xlsx", "read_xls",
              "read_pptx", "list_directory", "write_workspace", "write_file",
              "organize_folder", "organize_move_file"]
    tool_schemas = registry.list_schemas(tool_names)

    result = runtime.run_agentic(
        prompt,
        system_prompt=_load_system_prompt(),
        tools=tool_schemas,
        allowed_tools=tool_names,
        max_turns=30,
        timeout=1800,
    )

    if result.success:
        logger.info(f"execute_office_work: success")
    else:
        logger.error(f"execute_office_work: failed — {result.summary}")

    return {
        "summary": result.summary if result.success else "",
        "success": result.success,
        "capability": capability,
        "raw_output": getattr(result, "raw_output", ""),
    }


def _build_summarize_prompt(paths: list[str], output_mode: str, source_root: str) -> str:
    paths_list = "\n".join(f"- {p}" for p in paths)
    return f"""Summarize the following document(s):

{paths_list}

Source root: {source_root}
Output mode: {output_mode}

For each file:
1. Read the file using the appropriate tool (read_pdf, read_docx, or read_txt)
2. Write a summary using the write_workspace tool with filename pattern:
   - For PDF: {{original_filename}}.summary.md
   - For DOCX: {{original_filename}}.summary.md
   - For TXT: {{original_filename}}.summary.md
3. Summarize in English. For French or other foreign language documents, summarize accurately.

Output format for each file:
# Summary: {{filename}}

## Document Info
- Type: PDF/DOCX/TXT
- Size: N KB

## Key Points
- Point 1
- Point 2
- Point 3

## Executive Summary
1-paragraph summary of the document.
"""


def _build_analyze_prompt(paths: list[str], output_mode: str, source_root: str) -> str:
    paths_list = "\n".join(f"- {p}" for p in paths)
    return f"""Analyze the following CSV data file(s):

{paths_list}

Source root: {source_root}
Output mode: {output_mode}

For each CSV file:
1. Read the file using read_csv tool
2. Analyze the data: compute summary statistics, identify trends, note anomalies
3. Write an analysis report using write_workspace tool with filename:
   - {{original_filename}}.analysis.md

Output format:
# Data Analysis: {{filename}}

## File Overview
- Rows: N
- Columns: [list]

## Summary Statistics
For each numeric column: count, min, max, average

## Key Insights
- Insight 1
- Insight 2
- Insight 3
"""


def _build_organize_prompt(paths: list[str], output_mode: str, source_root: str) -> str:
    paths_list = "\n".join(f"- {p}" for p in paths)
    return f"""Organize the following folder(s) into logical groups:

{paths_list}

Source root: {source_root}
Output mode: {output_mode}

Workflow:
1. Use organize_folder tool to survey each folder and generate an organization plan
2. Review the plan — does the grouping make sense?
3. Write the organization plan using write_workspace tool with filename: organization-plan.md

Rules:
- Never delete original files
- Never overwrite existing files
- Group by: Documents (pdf/doc/docx), Text (txt/md), Data (csv/xlsx), Images (png/jpg), Code (py/js), Folders

Output format:
# Folder Organization Plan

## [Category] (N files)
- file1
- file2
"""


# ---------------------------------------------------------------------------
# Node: report_result
# ---------------------------------------------------------------------------

def report_result(state: dict) -> dict:
    """Write pr-evidence.json, warnings.md (if partial failures), and return final result."""
    import time
    workspace_root = state.get("workspace_root", "")
    capability = state.get("capability", "summarize")
    summary = state.get("summary", "")
    validated_paths = state.get("validated_paths", [])
    output_mode = state.get("output_mode", "workspace")
    warnings = state.get("warnings", [])

    if workspace_root:
        os.makedirs(workspace_root, exist_ok=True)

        # Write warnings.md if there are warnings
        if warnings:
            warnings_path = os.path.join(workspace_root, "warnings.md")
            try:
                with open(warnings_path, "w", encoding="utf-8") as f:
                    f.write("# Warnings\n\n")
                    for w in warnings:
                        f.write(f"- {w}\n")
                logger.info(f"report_result: warnings written to {warnings_path}")
            except OSError as exc:
                logger.error(f"report_result: failed to write warnings: {exc}")

        # Write pr-evidence.json
        evidence_path = os.path.join(workspace_root, "pr-evidence.json")
        evidence = {
            "metadata": {
                "agent_id": AGENT_ID,
                "step": "report_result",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            "data": {
                "capability": capability,
                "output_mode": output_mode,
                "source_paths": validated_paths,
                "summary": summary[:500] if summary else "",
                "success": True,
                "artifacts_dir": state.get("artifacts_dir", ""),
                "warnings_count": len(warnings),
            },
        }
        try:
            with open(evidence_path, "w", encoding="utf-8") as fh:
                json.dump(evidence, fh, ensure_ascii=False, indent=2)
            logger.info(f"report_result: evidence written to {evidence_path}")
        except OSError as exc:
            logger.error(f"report_result: failed to write evidence: {exc}")

    return {
        "status": "completed",
        "summary": summary,
        "capability": capability,
        "output_mode": output_mode,
        "warnings_count": len(warnings),
    }