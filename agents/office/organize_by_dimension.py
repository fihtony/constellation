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

import datetime as _dt
import json
import os
import re
import shutil
from typing import Any

from framework.tools.base import BaseTool, ToolResult

from agents.office.integrity import (
    check_operations_plan_no_deletes as _integrity_check_no_deletes,
)
from agents.office.integrity import (
    cleanup_empty_dirs as _integrity_cleanup_empty_dirs,
)
from agents.office.integrity import (
    snapshot_source as _integrity_snapshot_source,
)
from agents.office.integrity import (
    verify_post as _integrity_verify_post,
)


# ---- shared helpers --------------------------------------------------------

_ORGANIZED_OUTPUT_ROOT = "organized-output"
_FILENAME_PLAN = "organization-plan.md"
CUSTOM_ORGANIZE_CONTROL_FILENAMES = {
    "custom-organize-plan.md",
    "custom-organize-plan.json",
    "organization-plan.md",
    "organization-plan.json",
}

# Each dimension tool records the operations-plan.json path it is
# associated with, if known.  The dimension tools themselves do not
# own the artifacts dir — the office node passes it in via the
# ``output_root`` convention.  We derive the operations-plan path
# from the artifacts parent: tools receive ``output_root`` which is
# either ``<artifacts>/organized-output/files`` (workspace) or the
# source folder (inplace).  In both cases the operations-plan.json
# is two levels up from output_root when workspace, and lives next
# to the output_root (in ``<artifacts>/operations-plan.json``) when
# inplace — we conservatively use a sibling of output_root for both.
_OPERATIONS_PLAN_NAME = "operations-plan.json"


def _operations_plan_path(output_root: str) -> str:
    """Best-effort path to the per-task operations audit log.

    For workspace mode the artifacts dir is the grandparent of
    ``output_root`` (``<artifacts>/organized-output/files``); for
    inplace mode the artifacts dir is the parent of ``output_root``
    in the office node's state but the dimension tool only sees the
    source folder.  We default to a sibling of ``output_root`` —
    ``integrity.py`` and the office node both look there.
    """
    return os.path.join(os.path.dirname(output_root), _OPERATIONS_PLAN_NAME)


def _integrity_audit(
    *,
    source: str,
    output_root: str,
    snapshot: list[dict[str, Any]],
    output_mode: str,
    produced_paths: list[str] | None = None,
) -> list[str]:
    """Append the post-run integrity verdict to operations-plan.json.

    Also performs the in-place empty-dir cleanup when applicable.
    Returns the list of integrity errors so the caller can decide
    whether to downgrade ``ToolResult.success``.

    ``produced_paths`` is forwarded to the integrity verifier as a
    path-aware allowlist.  In inplace mode the dimension tool writes
    its ``organization-plan.md`` to ``output_root`` (which equals
    ``source``), so the plan must be excluded from the
    "unexpected file" check — otherwise a user file that happens to
    share the basename would be flagged.  The verifier also treats
    a snapshot entry that points at a produced path as
    intentionally consumed, so the overwrite does not double-flag
    the same slot.
    """
    operations_path = _operations_plan_path(output_root)
    if output_mode == "inplace":
        _integrity_cleanup_empty_dirs(source)
    errors = _integrity_verify_post(
        snapshot,
        source_root=source,
        output_root=output_root,
        output_mode=output_mode,
        produced_paths=produced_paths,
    )
    errors.extend(_integrity_check_no_deletes(operations_path))
    try:
        os.makedirs(os.path.dirname(operations_path), exist_ok=True)
        with open(operations_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "action": "integrity_verify",
                "phase": "after",
                "source": os.path.realpath(source),
                "output_root": os.path.realpath(output_root),
                "output_mode": output_mode,
                "produced_paths": [
                    os.path.realpath(p) for p in (produced_paths or []) if p
                ],
                "errors": errors,
                "materialized_by": "dimension-tool",
            }) + "\n")
    except OSError:
        # The audit log is best-effort.  Integrity errors are
        # returned via the ``errors`` list regardless of whether
        # the append succeeded.
        pass
    return errors


def _record_integrity_snapshot(
    output_root: str,
    source: str,
    snapshot: list[dict[str, Any]],
    output_mode: str,
) -> None:
    """Append the pre-organize snapshot to operations-plan.json.

    Best-effort: a failure to write the snapshot must not break the
    organize flow, but it WILL be flagged as a missing
    ``audit_snapshot`` record in the post-run verify step.
    """
    operations_path = _operations_plan_path(output_root)
    try:
        os.makedirs(os.path.dirname(operations_path), exist_ok=True)
        with open(operations_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "action": "audit_snapshot",
                "phase": "before",
                "source": os.path.realpath(source),
                "output_root": os.path.realpath(output_root),
                "output_mode": output_mode,
                "files": snapshot,
                "materialized_by": "dimension-tool",
            }) + "\n")
    except OSError:
        pass


def _safe_segment(value: str) -> str:
    """Return a filesystem-safe directory/bucket name.

    Note: we intentionally do not strip leading/trailing underscores —
    tools deliberately use sentinel bucket names such as ``_other`` and
    those must survive the sanitization round-trip.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (value or "").strip())
    return cleaned or "other"


def _safe_bucket_path(value: str) -> str:
    """Sanitize a bucket name that may be a nested path.

    Custom-dimension plans routinely express the desired layout with
    slashes (e.g. ``Yan/January`` for "student name / month").  The
    agent must materialize the actual nested folder hierarchy, not a
    single sanitized segment.  This helper splits the value on path
    separators, sanitizes each segment with :func:`_safe_segment`, and
    rejoins them so the bucket name drives a real directory tree.

    Empty or whitespace-only input falls back to ``"other"`` so the
    executor never writes into the output root by accident.
    """
    if not value or not value.strip():
        return "other"
    parts = re.split(r"[\\/]+", value.strip())
    cleaned_parts = [_safe_segment(part) for part in parts if part and part.strip()]
    if not cleaned_parts:
        return "other"
    return "/".join(cleaned_parts)


def _validate_inside(child: str, parent: str) -> bool:
    rp = os.path.realpath(os.path.abspath(child))
    pr = os.path.realpath(os.path.abspath(parent))
    return rp == pr or rp.startswith(pr.rstrip(os.sep) + os.sep)


def _copy_into(
    src_root: str,
    rel: str,
    dst_root: str,
    bucket: str,
    *,
    move: bool = False,
) -> str:
    """Materialize ``<src_root>/<rel>`` to ``<dst_root>/<bucket>/<rel>`` safely.

    ``bucket`` may be a nested path (e.g. ``"Yan/January"``) — it is
    sanitized segment-by-segment so the resulting layout mirrors the
    approved plan's bucket names instead of collapsing to a single
    directory.

    When ``move`` is True, the source file is moved (not copied) into
    the destination.  Inplace organize passes ``move=True`` so the
    user's source folder does not end up with a duplicate copy of
    every file alongside the dimension buckets.  Workspace organize
    leaves ``move`` at its default ``False`` to keep the source
    read-only.
    """
    src = os.path.realpath(os.path.join(src_root, rel))
    if not _validate_inside(src, src_root):
        raise ValueError(f"source escapes root: {rel}")
    bucket_path = _safe_bucket_path(bucket)
    dst_dir = os.path.join(dst_root, bucket_path)
    dst = os.path.realpath(os.path.join(dst_dir, rel))
    if not _validate_inside(dst, dst_root):
        raise ValueError(f"destination escapes root: {rel}")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    if move:
        shutil.move(src, dst)
    else:
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
            output_mode = "inplace" if os.path.realpath(source) == os.path.realpath(output_root) else "workspace"
            integrity_snapshot = _integrity_snapshot_source(source)
            _record_integrity_snapshot(output_root, source, integrity_snapshot, output_mode)
            files = _walk_files(source)
            sizes = [os.path.getsize(os.path.join(source, rel)) for rel in files]
            small_max, large_min = self._quartile_thresholds(sizes)
            entries: list[dict[str, str]] = []
            for rel, size in zip(files, sizes):
                bucket = self._bucket_for(size, small_max, large_min)
                # Inplace organize passes the user source folder as
                # both source and output_root (see
                # ``_organized_output_root`` in nodes.py).  Detect
                # that case so the file is moved (not copied) and the
                # user's disk usage is not doubled.
                dst = _copy_into(
                    source, rel, output_root, bucket,
                    move=os.path.realpath(source) == os.path.realpath(output_root),
                )
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
            plan_path = _write_plan(
                output_root,
                _format_plan_markdown(
                    dimension="size",
                    source_root=source,
                    bucket_rules=rules,
                    entries=entries,
                    bucket_section_title="Size buckets",
                ),
            )
            integrity_errors = _integrity_audit(
                source=source,
                output_root=output_root,
                snapshot=integrity_snapshot,
                output_mode=output_mode,
                produced_paths=[plan_path],
            )
            if integrity_errors:
                return ToolResult(
                    output="",
                    error=(
                        "organize_by_size: integrity check failed: "
                        + "; ".join(integrity_errors)
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
            output_mode = "inplace" if os.path.realpath(source) == os.path.realpath(output_root) else "workspace"
            integrity_snapshot = _integrity_snapshot_source(source)
            _record_integrity_snapshot(output_root, source, integrity_snapshot, output_mode)
            files = _walk_files(source)
            entries: list[dict[str, str]] = []
            for rel in files:
                ext = os.path.splitext(rel)[1]
                bucket = _bucket_for_extension(ext)
                # Inplace organize passes the user source folder as
                # both source and output_root (see
                # ``_organized_output_root`` in nodes.py).  Detect
                # that case so the file is moved (not copied) and the
                # user's disk usage is not doubled.
                dst = _copy_into(
                    source, rel, output_root, bucket,
                    move=os.path.realpath(source) == os.path.realpath(output_root),
                )
                entries.append({
                    "source": rel,
                    "destination": os.path.relpath(dst, output_root),
                    "extension": ext,
                })
            rules = sorted({_bucket_for_extension(os.path.splitext(r)[1]) for r in files})
            plan_path = _write_plan(
                output_root,
                _format_plan_markdown(
                    dimension="type",
                    source_root=source,
                    bucket_rules=[f"{b}/" for b in rules] or ["other/"],
                    entries=entries,
                ),
            )
            integrity_errors = _integrity_audit(
                source=source,
                output_root=output_root,
                snapshot=integrity_snapshot,
                output_mode=output_mode,
                produced_paths=[plan_path],
            )
            if integrity_errors:
                return ToolResult(
                    output="",
                    error=(
                        "organize_by_type: integrity check failed: "
                        + "; ".join(integrity_errors)
                    ),
                )
            return ToolResult(output=json.dumps({"dimension": "type", "entries": entries}))
        except Exception as exc:
            return ToolResult(output="", error=f"organize_by_type: {exc}")


# ---- time-based dimensions -------------------------------------------------


_TIME_FMT = "%Y-%m"


def _fmt_time(ts: float) -> str:
    # Format in UTC so the output is identical on local machines (any
    # timezone) and inside containers (typically UTC). Without this,
    # year-month buckets would shift across timezones.
    return _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc).strftime(_TIME_FMT)


class _TimeBucketTool(BaseTool):
    """Shared base for the three time-based organize tools."""

    dimension_label: str = ""
    time_attr: str = ""  # "st_mtime" / "st_atime" / "st_birthtime"
    fallback_attr: str | None = None  # when birthtime is unavailable

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
            output_mode = "inplace" if os.path.realpath(source) == os.path.realpath(output_root) else "workspace"
            integrity_snapshot = _integrity_snapshot_source(source)
            _record_integrity_snapshot(output_root, source, integrity_snapshot, output_mode)
            files = _walk_files(source)
            entries: list[dict[str, str]] = []
            fallbacks: list[str] = []
            for rel in files:
                full = os.path.join(source, rel)
                stat = os.stat(full)
                # Linux stat_result has no st_birthtime; default to 0 so
                # the fallback branch handles the missing attribute.
                ts = getattr(stat, self.time_attr, 0) or 0
                attr_used = self.time_attr
                if (not ts) and self.fallback_attr is not None:
                    ts = getattr(stat, self.fallback_attr, 0) or 0
                    attr_used = self.fallback_attr
                    fallbacks.append(rel)
                bucket = _fmt_time(ts)
                # Inplace organize passes the user source folder as
                # both source and output_root (see
                # ``_organized_output_root`` in nodes.py).  Detect
                # that case so the file is moved (not copied) and the
                # user's disk usage is not doubled.
                dst = _copy_into(
                    source, rel, output_root, bucket,
                    move=os.path.realpath(source) == os.path.realpath(output_root),
                )
                entries.append({
                    "source": rel,
                    "destination": os.path.relpath(dst, output_root),
                    "time_attr": attr_used,
                    "inferred_from": attr_used,
                })
            rules = sorted({e["destination"].split("/")[0] for e in entries})
            assumptions: list[str] = []
            if self.fallback_attr is not None:
                if fallbacks:
                    assumptions.append(
                        f"{len(fallbacks)} file(s) lack {self.time_attr}; "
                        f"used {self.fallback_attr} as a fallback."
                    )
                else:
                    assumptions.append(
                        f"Used {self.time_attr}; falls back to "
                        f"{self.fallback_attr} when the filesystem does "
                        f"not report it. (inferred_from: {self.time_attr})"
                    )
            else:
                assumptions.append(
                    f"Bucketed by {self.time_attr} (inferred_from: "
                    f"{self.time_attr})."
                )
            plan_path = _write_plan(
                output_root,
                _format_plan_markdown(
                    dimension=self.dimension_label,
                    source_root=source,
                    bucket_rules=rules,
                    entries=entries,
                    assumptions=assumptions,
                ),
            )
            integrity_errors = _integrity_audit(
                source=source,
                output_root=output_root,
                snapshot=integrity_snapshot,
                output_mode=output_mode,
                produced_paths=[plan_path],
            )
            if integrity_errors:
                return ToolResult(
                    output="",
                    error=(
                        f"organize_by_{self.dimension_label}: integrity check failed: "
                        + "; ".join(integrity_errors)
                    ),
                )
            return ToolResult(output=json.dumps({
                "dimension": self.dimension_label,
                "entries": entries,
                "fallback_count": len(fallbacks),
            }))
        except Exception as exc:
            return ToolResult(output="", error=f"organize_by_{self.dimension_label}: {exc}")


class OrganizeByModifiedTimeTool(_TimeBucketTool):
    name = "organize_by_modified_time"
    description = "Group files by year-month of mtime. Zero-LLM."
    dimension_label = "modified_time"
    time_attr = "st_mtime"


class OrganizeByAccessedTimeTool(_TimeBucketTool):
    name = "organize_by_accessed_time"
    description = "Group files by year-month of atime. Zero-LLM."
    dimension_label = "accessed_time"
    time_attr = "st_atime"


class OrganizeByCreatedTimeTool(_TimeBucketTool):
    name = "organize_by_created_time"
    description = (
        "Group files by year-month of birth time; falls back to mtime "
        "on filesystems that do not report birthtime. Zero-LLM."
    )
    dimension_label = "created_time"
    time_attr = "st_birthtime"
    fallback_attr = "st_mtime"


class OrganizeByFilenameTool(BaseTool):
    name = "organize_by_filename"
    description = (
        "Group files by the first alphabetic character of the basename "
        "(A-Z, with _other for non-letters). Zero-LLM, deterministic."
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
            output_mode = "inplace" if os.path.realpath(source) == os.path.realpath(output_root) else "workspace"
            integrity_snapshot = _integrity_snapshot_source(source)
            _record_integrity_snapshot(output_root, source, integrity_snapshot, output_mode)
            files = _walk_files(source)
            entries: list[dict[str, str]] = []
            for rel in files:
                basename = os.path.basename(rel)
                bucket = self._bucket_for(basename)
                # Inplace organize passes the user source folder as
                # both source and output_root (see
                # ``_organized_output_root`` in nodes.py).  Detect
                # that case so the file is moved (not copied) and the
                # user's disk usage is not doubled.
                dst = _copy_into(
                    source, rel, output_root, bucket,
                    move=os.path.realpath(source) == os.path.realpath(output_root),
                )
                entries.append({
                    "source": rel,
                    "destination": os.path.relpath(dst, output_root),
                    "first_char": basename[:1],
                })
            rules = sorted({e["destination"].split("/")[0] for e in entries})
            plan_path = _write_plan(
                output_root,
                _format_plan_markdown(
                    dimension="filename",
                    source_root=source,
                    bucket_rules=rules,
                    entries=entries,
                ),
            )
            integrity_errors = _integrity_audit(
                source=source,
                output_root=output_root,
                snapshot=integrity_snapshot,
                output_mode=output_mode,
                produced_paths=[plan_path],
            )
            if integrity_errors:
                return ToolResult(
                    output="",
                    error=(
                        "organize_by_filename: integrity check failed: "
                        + "; ".join(integrity_errors)
                    ),
                )
            return ToolResult(output=json.dumps({"dimension": "filename", "entries": entries}))
        except Exception as exc:
            return ToolResult(output="", error=f"organize_by_filename: {exc}")

    @staticmethod
    def _bucket_for(basename: str) -> str:
        if not basename:
            return "_other"
        first = basename[0].upper()
        if "A" <= first <= "Z":
            return first
        return "_other"


# ---- dispatcher ------------------------------------------------------------


from framework.office.dimensions import CUSTOM_DIMENSION, VALID_DIMENSIONS


_DIMENSION_TOOL = {
    "size": OrganizeBySizeTool,
    "type": OrganizeByTypeTool,
    "created_time": OrganizeByCreatedTimeTool,
    "modified_time": OrganizeByModifiedTimeTool,
    "accessed_time": OrganizeByAccessedTimeTool,
    "filename": OrganizeByFilenameTool,
}


def run_dimension_tool(
    dimension: str,
    source: str,
    output_root: str,
    *,
    custom_hint: str = "",
    custom_plan: dict | None = None,
) -> ToolResult:
    """Run the bounded dimension tool for ``dimension``.

    Returns a ``ToolResult`` whose ``output`` is the JSON payload the
    dimension tool produced. Used by the bounded path inside
    ``execute_office_work`` — never invoked through the agentic runtime.
    """
    if dimension == CUSTOM_DIMENSION:
        # The custom-dimension path is LLM-driven and lives in
        # ``execute_office_work`` rather than in a zero-LLM tool.  We
        # route the call through the dispatcher anyway so the same
        # entry point handles both built-in and custom dimensions.
        return _dispatch_custom_dimension(
            source=source,
            output_root=output_root,
            custom_hint=custom_hint,
            custom_plan=custom_plan,
        )
    if dimension not in VALID_DIMENSIONS:
        return ToolResult(output="", error=f"unsupported dimension: {dimension!r}")
    tool_cls = _DIMENSION_TOOL.get(dimension)
    if tool_cls is None:
        return ToolResult(output="", error=f"no tool registered for dimension: {dimension!r}")
    return tool_cls().execute_sync(source=source, output_root=output_root)


# ---------------------------------------------------------------------------
# Custom-dimension planning + execution (LLM-driven)
# ---------------------------------------------------------------------------


from typing import Callable
import json as _json


def _read_sample_files(source: str, *, max_files: int = 5, max_chars: int = 600) -> list[dict]:
    """Read up to ``max_files`` sample files from ``source``.

    Returns a list of ``{"path": rel, "excerpt": str}`` dicts.
    Skips hidden files and non-text extensions so the planner prompt
    stays small and focused.
    """
    if not source or not os.path.isdir(source):
        return []
    text_exts = {".txt", ".md", ".csv", ".json", ".yaml", ".yml", ".log",
                ".html", ".htm", ".xml", ".rst", ".tsv"}
    candidates: list[tuple[str, str, str]] = []
    for walk_root, dirs, files in os.walk(source):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            if name in CUSTOM_ORGANIZE_CONTROL_FILENAMES:
                continue
            ext = os.path.splitext(name)[1].lower()
            if ext not in text_exts:
                continue
            full = os.path.join(walk_root, name)
            candidates.append((os.path.relpath(full, source), full, ext))

    if not candidates:
        return []

    if len(candidates) <= max_files:
        selected = candidates
    elif max_files <= 1:
        selected = [candidates[0]]
    else:
        last_index = len(candidates) - 1
        selected_indexes = sorted({
            round(index * last_index / (max_files - 1))
            for index in range(max_files)
        })
        selected = [candidates[index] for index in selected_indexes]

    samples: list[dict] = []
    for rel_path, full, ext in selected:
        try:
            with open(full, "r", encoding="utf-8", errors="replace") as fh:
                excerpt = fh.read(max_chars)
        except OSError:
            continue
        samples.append({
            "path": rel_path,
            "ext": ext,
            "excerpt": excerpt,
        })
    return samples


def _build_planning_prompt(
    hint: str,
    source: str,
    samples: list[dict],
    *,
    existing_plan: dict | None = None,
    revision_note: str = "",
) -> str:
    """Render the LLM prompt that produces a custom-dimension plan."""
    sample_block = "\n\n".join(
        f"--- {s['path']} ({s['ext']}) ---\n{s['excerpt']}"
        for s in samples
    )
    revision_block = ""
    if existing_plan or revision_note:
        revision_block = (
            "\nThe previous draft plan was:\n"
            f"{json.dumps(existing_plan or {}, ensure_ascii=False, indent=2)}\n\n"
            "Revise that draft to address this user feedback:\n"
            f"{revision_note or '(no additional note supplied)'}\n\n"
            "Preserve any parts of the prior plan that still fit the files, "
            "but change the buckets and classification rule where the "
            "feedback requires it.\n"
        )
    return (
        f"You are helping organize files in this folder:\n{source}\n\n"
        f"The user wants to group them by **{hint}**.\n\n"
        f"{revision_block}"
        "Read the sample files below and propose an organize plan with:\n"
        "1. A list of source-relative bucket folder paths you recommend "
        "(3-12 buckets).\n"
        "2. For each sample file, which source-relative bucket folder path "
        "it belongs to and why.\n"
        "3. A general rule the agent can use to classify the\n"
        "   remaining (unsampled) files into the same buckets.\n\n"
        "Bucket names and sample_mapping values must be source-relative "
        "bucket folder paths under the selected source folder, not absolute "
        "filesystem paths and not partial labels. Do not include the "
        "absolute source folder, `/app/userdata`, host paths, or a leading "
        "`/`. If the user asks for multiple folder levels, express every "
        "level with `/` separators, e.g. `person/month`, and make the "
        "sample_mapping values use the same hierarchy. Do not write or "
        "create any files; return JSON only.\n\n"
        "Reply in JSON only, with this exact schema:\n"
        "{\n"
        '  "buckets": ["name1", "name2", ...],\n'
        '  "sample_mapping": {"<sample_path>": "<bucket_name>", ...},\n'
        '  "classification_rule": "Plain-English rule the agent can apply to all files",\n'
        '  "rationale": "One paragraph explaining the plan."\n'
        "}\n\n"
        "Keep the JSON compact. Do not include reasoning, markdown, or prose outside "
        "the JSON object. Keep classification_rule and rationale concise.\n\n"
        f"Sample files ({len(samples)}):\n\n{sample_block}\n"
    )


def _summarize_remaining_path_evidence(remaining: list[dict]) -> str:
    """Return a short, methodology-agnostic summary of the
    directory-naming patterns in the *remaining* file list.

    Why: classifiers that look at file excerpts in isolation have
    no way to detect a global pattern (e.g. all top-level folders
    are 4-digit numeric, day-month-month-day, student initials,
    ISO dates, etc.).  Pre-computing the pattern and feeding it as
    evidence lets the LLM reason about the *whole* layout, not
    just the excerpt of one file at a time.  This is methodology-
    level — it does not hard-code any specific format, it just
    surfaces what the data actually looks like and lets the LLM
    pick the rule.
    """
    if not remaining:
        return ""

    # Bucket the source-relative paths by depth.  At each depth,
    # look at the directory segment; if the entire segment set
    # shares a recognisable shape (numeric, alpha, alphanumeric,
    # dotted, hyphenated, etc.) surface it so the model sees the
    # full pattern at once.
    by_depth: dict[int, list[str]] = {}
    for item in remaining:
        path = str(item.get("path") or "").strip().strip("/")
        if not path:
            continue
        segments = [seg for seg in path.split("/") if seg]
        for depth, seg in enumerate(segments[:-1], start=1):
            by_depth.setdefault(depth, []).append(seg)

    lines: list[str] = []
    total = len(remaining)
    lines.append(
        f"The {total} file(s) below sit under these top-level directories "
        "(whole-pattern view, not per-file guesses):"
    )
    seen: set[int] = set()
    for depth in sorted(by_depth):
        if depth in seen:
            continue
        seen.add(depth)
        segments = sorted(set(by_depth[depth]))
        pattern = _classify_segment_shape(segments)
        sample = ", ".join(repr(s) for s in segments[:12])
        if len(segments) > 12:
            sample += f", ... ({len(segments)} total)"
        lines.append(f"  - depth-{depth} folders ({pattern}): {sample}")

    if len(by_depth) == 1:
        lines.append(
            "All files share the same depth-1 folder; the bucket hierarchy "
            "should derive from these folder names (and/or from content)."
        )
    elif len(by_depth) >= 2:
        lines.append(
            "Files span multiple folder depths; preserve the same depth in "
            "the bucket path or justify collapsing it explicitly in the rule."
        )
    return "\n".join(lines)


def _classify_segment_shape(segments: list[str]) -> str:
    """Return a short human description of the dominant shape.

    Generic categories (no test-case-specific knowledge):
      - "all 4-digit numeric"     e.g. ["0103", "0110", "0124"]
      - "all numeric"             e.g. ["1", "2", "3"]
      - "all 2-letter alpha"      e.g. ["AB", "CD"]
      - "all lowercase alpha"     e.g. ["jan", "feb"]
      - "mixed"
    """
    if not segments:
        return "empty"
    if all(re.fullmatch(r"\d{4}", s or "") for s in segments):
        return "all 4-digit numeric"
    if all(re.fullmatch(r"\d+", s or "") for s in segments):
        return "all numeric"
    if all(re.fullmatch(r"[A-Za-z]{2,4}", s or "") for s in segments):
        length = len(segments[0]) if segments else 0
        if all(s == s.lower() for s in segments if s):
            return f"all {length}-letter lowercase alpha"
        return f"all {length}-letter alpha"
    if all(re.fullmatch(r"[a-z]+", s or "") for s in segments):
        return "all lowercase alpha"
    if all(re.fullmatch(r"\d{4}-\d{2}-\d{2}", s or "") for s in segments):
        return "all ISO date"
    return "mixed"


def _build_execution_prompt(
    hint: str,
    plan: dict,
    remaining: list[dict],
    path_evidence: str = "",
) -> str:
    """Render the LLM prompt that classifies unsampled files into buckets.

    ``path_evidence`` is the methodology-agnostic directory-pattern
    summary produced by :func:`_summarize_remaining_path_evidence`;
    when non-empty it is inserted before the file list so the
    classifier reasons about the *whole* layout, not per-file
    guesses.
    """
    file_block = "\n".join(
        f"- {item['path']} (excerpt: {item['excerpt'][:200]!r})"
        for item in remaining
    )
    bucket_list = ", ".join(repr(b) for b in plan.get("buckets", []))
    evidence_block = (
        f"\n\nLayout evidence (use this to derive the classification rule):\n"
        f"{path_evidence}\n"
        if path_evidence
        else ""
    )
    return (
        f"You classified a sample of files into these example buckets for "
        f"organizing by **{hint}**:\n{bucket_list}\n\n"
        f"Plan rationale: {plan.get('rationale', '')}\n\n"
        f"Classification rule from the planner: {plan.get('classification_rule', '')}\n"
        f"{evidence_block}\n"
        "Now classify the following remaining files. For each, output "
        "the source-relative bucket path that follows the classification "
        "rule. Do not include the absolute source folder, `/app/userdata`, "
        "host paths, or a leading `/`. The example buckets are not a closed "
        "list: create a new source-relative bucket when the rule applies to "
        "a value that was not present in the samples. Use `__unmatched__` "
        "only when the source file lacks enough evidence to apply the rule. "
        "Reply in JSON only:\n"
        "{\n"
        '  "mapping": {"<file_path>": "<bucket_name>", ...}\n'
        "}\n\n"
        f"Files to classify ({len(remaining)}):\n\n{file_block}\n"
    )


def _plan_published(
    plan: dict,
    *,
    source: str,
    output_root: str,
) -> str:
    """Write ``plan`` to ``output_root/custom-organize-plan.md``.

    Returns the plan path. The markdown rendering is deliberately
    small — compass surfaces the same JSON to the UI, and the file
    is the durable record.
    """
    os.makedirs(output_root, exist_ok=True)
    plan_path = os.path.join(output_root, "custom-organize-plan.md")
    buckets = plan.get("buckets") or []
    sample_mapping = plan.get("sample_mapping") or {}
    rule = plan.get("classification_rule", "")
    rationale = plan.get("rationale", "")
    lines = [
        "# Custom Organize Plan",
        "",
        f"**Source:** {source}",
        f"**Output:** {output_root}",
        "",
        "## Buckets",
        *[f"- `{b}`" for b in buckets],
        "",
        "## Classification rule",
        rule or "(none)",
        "",
        "## Rationale",
        rationale or "(none)",
        "",
        "## Sample mapping",
        "| Source file | Bucket |",
        "| --- | --- |",
        *[f"| `{path}` | `{bucket}` |" for path, bucket in sorted(sample_mapping.items())],
        "",
    ]
    with open(plan_path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
    return plan_path


def _dispatch_custom_dimension(
    *,
    source: str,
    output_root: str,
    custom_hint: str,
    custom_plan: dict | None,
) -> ToolResult:
    """Stub dispatcher for the custom-dimension path.

    The real LLM call lives in :func:`execute_office_work` so the
    runtime context is available.  This stub exists to keep
    :func:`run_dimension_tool` a single dispatch entry point and
    returns a structured "needs planner" payload the office node
    can act on.
    """
    payload = {
        "dimension": CUSTOM_DIMENSION,
        "stage": "plan_required" if not custom_plan else "execute",
        "custom_hint": custom_hint,
        "plan": custom_plan or {},
        "source": source,
        "output_root": output_root,
    }
    return ToolResult(output=_json.dumps(payload))
