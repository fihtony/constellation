"""Office organize — deterministic dimension tools.

Each tool in this module materializes a folder layout for a single
dimension (``size``, ``type``, ``created_time``, ``modified_time``,
``accessed_time``, ``filename``) and writes the corresponding
``organization-plan.md``. Tools are zero-LLM: the agent calls them via
``execute_office_work`` (bounded path) or the agentic runtime's tool
surface (LLM path), and the existing plan-output gate continues to
verify the materialized layout against the plan.

The tools share helpers for path safety, plan rendering, and copy
execution; the per-dimension logic is isolated in the ``_bucket_for``
method of each tool.
"""
from __future__ import annotations

import json
import os
import re
import shutil
from typing import Any

from framework.tools.base import BaseTool, ToolResult


# ---- shared helpers --------------------------------------------------------

_ORGANIZED_OUTPUT_ROOT = "organized-output"
_FILENAME_PLAN = "organization-plan.md"


def _safe_segment(value: str) -> str:
    """Return a filesystem-safe directory/bucket name."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip())
    cleaned = cleaned.strip("_")
    return cleaned or "other"


def _validate_inside(child: str, parent: str) -> bool:
    rp = os.path.realpath(os.path.abspath(child))
    pr = os.path.realpath(os.path.abspath(parent))
    return rp == pr or rp.startswith(pr.rstrip(os.sep) + os.sep)


def _copy_into(src_root: str, rel: str, dst_root: str, bucket: str) -> str:
    """Copy ``<src_root>/<rel>`` to ``<dst_root>/<bucket>/<rel>`` safely."""
    src = os.path.realpath(os.path.join(src_root, rel))
    if not _validate_inside(src, src_root):
        raise ValueError(f"source escapes root: {rel}")
    dst_dir = os.path.join(dst_root, _safe_segment(bucket))
    dst = os.path.realpath(os.path.join(dst_dir, rel))
    if not _validate_inside(dst, dst_root):
        raise ValueError(f"destination escapes root: {rel}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return dst


def _walk_files(root: str) -> list[str]:
    out: list[str] = []
    for walk_root, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            out.append(os.path.relpath(os.path.join(walk_root, name), root))
    return out


def _format_plan_markdown(
    *,
    dimension: str,
    source_root: str,
    bucket_rules: list[str],
    entries: list[dict[str, str]],
    assumptions: list[str] | None = None,
    bucket_section_title: str = "Bucket rules",
) -> str:
    lines = [
        f"# Folder Organization Plan (dimension: {dimension})",
        "",
        f"**Source:** {source_root}",
        "**Mode:** workspace",
        "",
        f"## {bucket_section_title}",
        *[f"- {rule}" for rule in bucket_rules],
    ]
    if assumptions:
        lines.extend(["", "## Assumptions", *[f"- {a}" for a in assumptions]])
    lines.extend(
        [
            "",
            "## Files Organized",
            "| Source Path | Destination |",
            "| --- | --- |",
        ]
    )
    for entry in entries:
        lines.append(f"| {entry['source']} | {entry['destination']} |")
    lines.append("")
    return "\n".join(lines)


def _write_plan(output_root: str, text: str) -> str:
    os.makedirs(output_root, exist_ok=True)
    plan_path = os.path.join(output_root, _FILENAME_PLAN)
    with open(plan_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return plan_path


# ---- size ------------------------------------------------------------------


class OrganizeBySizeTool(BaseTool):
    name = "organize_by_size"
    description = (
        "Group files into small/medium/large buckets using quartile "
        "thresholds derived from the source tree's real size distribution. "
        "Zero-LLM: deterministic, fast, auditable."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "source": {"type": "string", "description": "Source folder path."},
            "output_root": {"type": "string", "description": "Destination root for organized-output/."},
        },
        "required": ["source", "output_root"],
    }

    def execute_sync(self, source: str = "", output_root: str = "", **_: Any) -> ToolResult:
        try:
            files = _walk_files(source)
            sizes = [os.path.getsize(os.path.join(source, rel)) for rel in files]
            small_max, large_min = self._quartile_thresholds(sizes)
            entries: list[dict[str, str]] = []
            for rel, size in zip(files, sizes):
                bucket = self._bucket_for(size, small_max, large_min)
                dst = _copy_into(source, rel, output_root, bucket)
                entries.append({
                    "source": rel,
                    "destination": os.path.relpath(dst, output_root),
                    "size_bytes": str(size),
                    "bucket": bucket,
                })
            rules = [
                f"small: < {small_max} B",
                f"medium: {small_max} B - {large_min} B",
                f"large: >= {large_min} B",
            ]
            _write_plan(
                output_root,
                _format_plan_markdown(
                    dimension="size",
                    source_root=source,
                    bucket_rules=rules,
                    entries=entries,
                    bucket_section_title="Size buckets",
                ),
            )
            return ToolResult(output=json.dumps({
                "dimension": "size",
                "entries": entries,
                "thresholds": {"small_max": small_max, "large_min": large_min},
            }))
        except Exception as exc:
            return ToolResult(output="", error=f"organize_by_size: {exc}")

    @staticmethod
    def _quartile_thresholds(sizes: list[int]) -> tuple[int, int]:
        if not sizes:
            return 0, 0
        ordered = sorted(sizes)
        # Quartile (25%) and (75%) by simple index. Keeps bucket counts
        # stable for any distribution.
        n = len(ordered)
        q1_idx = max(0, n // 4 - 1)
        q3_idx = min(n - 1, (3 * n) // 4)
        small_max = ordered[q1_idx]
        large_min = ordered[q3_idx]
        # Guarantee non-overlapping buckets when sizes collapse.
        if large_min <= small_max:
            large_min = small_max + 1
        return small_max, large_min

    @staticmethod
    def _bucket_for(size: int, small_max: int, large_min: int) -> str:
        if size < small_max:
            return "small"
        if size >= large_min:
            return "large"
        return "medium"


# ---- type ------------------------------------------------------------------


_TYPE_BUCKETS: dict[frozenset[str], str] = {
    frozenset({".pdf", ".doc", ".docx", ".docm", ".dotx", ".dotm", ".odt"}): "documents",
    frozenset({
        ".txt", ".md", ".markdown", ".rtf", ".html", ".htm", ".xml",
        ".json", ".jsonl", ".yaml", ".yml", ".log", ".ini", ".cfg", ".toml",
    }): "text",
    frozenset({
        ".csv", ".xlsx", ".xls", ".xlsm", ".xltx", ".xltm", ".xlsb", ".ods", ".tsv",
    }): "data",
    frozenset({".png", ".jpg", ".jpeg", ".gif", ".svg"}): "images",
    frozenset({
        ".ppt", ".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm", ".odp",
    }): "presentations",
    frozenset({".py", ".js", ".ts", ".java", ".cpp", ".c", ".h"}): "code",
}


def _bucket_for_extension(ext: str) -> str:
    ext_lower = ext.lower()
    for exts, bucket in _TYPE_BUCKETS.items():
        if ext_lower in exts:
            return bucket
    return "other"


class OrganizeByTypeTool(BaseTool):
    name = "organize_by_type"
    description = (
        "Group files by extension bucket: documents, text, data, images, "
        "presentations, code, or other. Zero-LLM, deterministic."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "output_root": {"type": "string"},
        },
        "required": ["source", "output_root"],
    }

    def execute_sync(self, source: str = "", output_root: str = "", **_: Any) -> ToolResult:
        try:
            files = _walk_files(source)
            entries: list[dict[str, str]] = []
            for rel in files:
                ext = os.path.splitext(rel)[1]
                bucket = _bucket_for_extension(ext)
                dst = _copy_into(source, rel, output_root, bucket)
                entries.append({
                    "source": rel,
                    "destination": os.path.relpath(dst, output_root),
                    "extension": ext,
                })
            rules = sorted({_bucket_for_extension(os.path.splitext(r)[1]) for r in files})
            _write_plan(
                output_root,
                _format_plan_markdown(
                    dimension="type",
                    source_root=source,
                    bucket_rules=[f"{b}/" for b in rules] or ["other/"],
                    entries=entries,
                ),
            )
            return ToolResult(output=json.dumps({"dimension": "type", "entries": entries}))
        except Exception as exc:
            return ToolResult(output="", error=f"organize_by_type: {exc}")


# ---- stubs for tools implemented in later tasks ----------------------------
# These exist so that `from agents.office.organize_by_dimension import ...`
# resolves cleanly. Each task that lands the real implementation replaces
# the stub with a functional class.


class _NotYetImplementedTool(BaseTool):
    name = ""
    description = ""
    parameters_schema = {
        "type": "object",
        "properties": {
            "source": {"type": "string"},
            "output_root": {"type": "string"},
        },
        "required": ["source", "output_root"],
    }

    def execute_sync(self, source: str = "", output_root: str = "", **_: Any) -> ToolResult:
        return ToolResult(output="", error=f"{self.name}: not yet implemented")


class OrganizeByCreatedTimeTool(_NotYetImplementedTool):
    name = "organize_by_created_time"


class OrganizeByModifiedTimeTool(_NotYetImplementedTool):
    name = "organize_by_modified_time"


class OrganizeByAccessedTimeTool(_NotYetImplementedTool):
    name = "organize_by_accessed_time"


class OrganizeByFilenameTool(_NotYetImplementedTool):
    name = "organize_by_filename"
