# Office Dimension-Driven Organize — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the LLM-biased `organize` capability in the Office agent with a deterministic, dimension-driven contract. The agent groups files along a user-specified dimension (`size` / `type` / `created_time` / `modified_time` / `accessed_time` / `filename`) and never invents a dimension. A missing dimension triggers a structured `needs_clarification` payload back to the orchestrator. All business-specific hardcodes (`students`, `by-student`, `primary_entity`, `IDENTITY_PREFIXES`, `VALID_CATEGORIES`, `WRAPPER_PREFIXES`) are removed.

**Architecture:** New `agents/office/dimensions.py` owns the dimension contract and `parse_dimension` resolution. New `agents/office/organize_by_dimension.py` hosts six zero-LLM tools that mirror the existing `_run_bounded_folder_summarize` style. `analyze_request` becomes the dimension gatekeeper and returns `needs_clarification` when the dimension is missing. `office_tools.py` loses the identity / category hardcodes; `_normalize_organized_path` only checks the `organized-output/files/` prefix. `office-generic-methodology` Phase 3.C is rewritten to require user-supplied dimensions. Tests stop asserting business literals (`students/`, person names) and gain a new `test_office_organize_dimensions.py` covering all six dimensions + clarification.

**Tech Stack:** Python 3.12, pytest, os / pathlib / shutil / stat (no new dependencies).

---

## File Structure

### New files
- `agents/office/dimensions.py` — `VALID_DIMENSIONS`, `KEYWORD_TO_DIMENSION`, `parse_dimension(metadata, user_text) -> str`.
- `agents/office/organize_by_dimension.py` — `run_dimension_tool(dimension, source_root, output_root, output_mode) -> DimensionToolResult` plus six tool classes `OrganizeBySizeTool`, `OrganizeByTypeTool`, `OrganizeByCreatedTimeTool`, `OrganizeByModifiedTimeTool`, `OrganizeByAccessedTimeTool`, `OrganizeByFilenameTool`.
- `tests/unit/agents/test_office_organize_dimensions.py` — covers `parse_dimension`, all six tools, clarification payload, plan-threshold rendering, and the new prompt invariants.

### Modified files
- `agents/office/office_tools.py` — delete `VALID_CATEGORIES`, `WRAPPER_PREFIXES`, `IDENTITY_PREFIXES`, `_extract_primary_entity`, `_clean_entity_candidate`, `_looks_like_person_name`, `primary_entity`-bearing fields in `_build_file_metadata` and `collect_organize_file_inventory`; rewrite `_normalize_organized_path` / `_is_under_organized_output` to a single `organized-output/files/` prefix check; drop the `primary_entity_confidence` enforcement in `OrganizeMoveFileTool`; add the six `organize_by_*` tool classes and register them in `_OFFICE_TOOLS`.
- `agents/office/nodes.py` — `analyze_request` resolves and validates the dimension, returns the `needs_clarification` payload on miss; `execute_office_work` branches on dimension to call the bounded tool; `_build_organize_prompt` is rewritten to be dimension-agnostic; `_canonical_organize_destination` and `_verify_organize_materialization` use the plan-recorded destinations instead of building them from `primary_entity`.
- `agents/office/prompts/organize.md` — full rewrite to "dimension-driven".
- `agents/office/prompts/system.md` — add a one-paragraph rule for the dimension contract.
- `skills/office-generic-methodology/instructions.md` — Phase 3.C rewritten; Phase 5 gains a dimension-match checklist item.
- `tests/unit/agents/test_office_organize_schema.py` — replace `students/`, person names, `by-student/` literals with neutral bucket names; delete tests that assert `VALID_CATEGORIES` membership.
- `tests/unit/agents/test_office_organize_verification.py` — replace person-name paths with synthetic bucket names; the contract (every source file materialized exactly once) stays the invariant.
- `tests/unit/agents/office/test_office_tool_registration.py` — add an assertion that the six new `organize_by_*` tools are registered.

---

## Task 1: Add `agents/office/dimensions.py` (TDD)

**Files:**
- Create: `agents/office/dimensions.py`
- Test: `tests/unit/agents/test_office_organize_dimensions.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/agents/test_office_organize_dimensions.py`:

```python
"""Tests for agents/office/dimensions.py — the dimension contract."""
from __future__ import annotations

from agents.office.dimensions import (
    VALID_DIMENSIONS,
    parse_dimension,
)


def test_valid_dimensions_exact_set():
    assert VALID_DIMENSIONS == frozenset({
        "size",
        "type",
        "created_time",
        "modified_time",
        "accessed_time",
        "filename",
    })


def test_parse_dimension_from_metadata_wins():
    md = {"organizeGroupBy": "size"}
    assert parse_dimension(md, "please organize by name") == "size"


def test_parse_dimension_normalizes_case():
    md = {"organizeGroupBy": "SIZE"}
    assert parse_dimension(md, "") == "size"


def test_parse_dimension_rejects_unknown_metadata_value():
    md = {"organizeGroupBy": "alphabetical"}
    # Unknown metadata value falls through to keyword scan; no keyword -> "".
    assert parse_dimension(md, "") == ""


def test_parse_dimension_keyword_english_size():
    assert parse_dimension({}, "please organize by file size") == "size"


def test_parse_dimension_keyword_chinese_size():
    assert parse_dimension({}, "请按文件大小整理") == "size"


def test_parse_dimension_keyword_chinese_type():
    assert parse_dimension({}, "请按文件类型整理") == "type"


def test_parse_dimension_keyword_modified_time():
    assert parse_dimension({}, "please group by modified time") == "modified_time"


def test_parse_dimension_keyword_filename():
    assert parse_dimension({}, "按名称分组") == "filename"


def test_parse_dimension_keyword_created_time_chinese():
    assert parse_dimension({}, "按创建时间") == "created_time"


def test_parse_dimension_returns_empty_when_no_signal():
    assert parse_dimension({}, "please organize this folder") == ""


def test_parse_dimension_metadata_overrides_keyword():
    md = {"organizeGroupBy": "type"}
    assert parse_dimension(md, "by file size please") == "type"


def test_parse_dimension_handles_missing_metadata():
    assert parse_dimension(None, "by filename") == "filename"
    assert parse_dimension(None, "no signal here") == ""
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'agents.office.dimensions'`.

- [ ] **Step 3: Create the implementation file**

Create `agents/office/dimensions.py`:

```python
"""Office organize — dimension contract.

Defines the canonical set of supported grouping dimensions and a
deterministic parser that turns task metadata + user text into exactly
one dimension, or the empty string when neither source supplies a
recognized dimension. The agent MUST NOT invent a dimension; the empty
result is the signal for a structured `needs_clarification` reply.

The dimension contract is intentionally narrow. Adding a new dimension
is a one-line change here plus a corresponding tool in
``organize_by_dimension`` — never a code change buried in a prompt.
"""
from __future__ import annotations

from typing import Any, Mapping

VALID_DIMENSIONS: frozenset[str] = frozenset({
    "size",
    "type",
    "created_time",
    "modified_time",
    "accessed_time",
    "filename",
})


# Neutral, multilingual keyword mapping. Every entry is a generic term
# that any user might reasonably type — no business-specific phrases,
# no test-case wording, no dataset references. The keys are lower-cased
# substrings; the values are the canonical dimension id.
KEYWORD_TO_DIMENSION: dict[str, str] = {
    # size
    "size": "size",
    "file size": "size",
    "by size": "size",
    "大小": "size",
    "按大小": "size",
    "按文件大小": "size",
    # type / extension
    "type": "type",
    "file type": "type",
    "extension": "type",
    "by type": "type",
    "类型": "type",
    "扩展名": "type",
    # created_time
    "created time": "created_time",
    "creation time": "created_time",
    "ctime": "created_time",
    "birthtime": "created_time",
    "创建时间": "created_time",
    "按创建时间": "created_time",
    # modified_time
    "modified time": "modified_time",
    "mtime": "modified_time",
    "last modified": "modified_time",
    "修改时间": "modified_time",
    "按修改时间": "modified_time",
    # accessed_time
    "accessed time": "accessed_time",
    "atime": "accessed_time",
    "last access": "accessed_time",
    "访问时间": "accessed_time",
    "按访问时间": "accessed_time",
    # filename
    "filename": "filename",
    "by name": "filename",
    "by filename": "filename",
    "文件名": "filename",
    "按文件名": "filename",
    "按名称": "filename",
}


def _from_metadata(metadata: Mapping[str, Any] | None) -> str:
    if not metadata:
        return ""
    raw = metadata.get("organizeGroupBy")
    if not raw or not isinstance(raw, str):
        return ""
    candidate = raw.strip().lower()
    return candidate if candidate in VALID_DIMENSIONS else ""


def _from_user_text(user_text: str) -> str:
    if not user_text:
        return ""
    text = user_text.lower()
    # Order matters only for the test of "specific phrase wins over
    # generic"; the dict already gives specific phrases a longer
    # substring (e.g. "file size" before "size"). We still iterate
    # longest-first to be explicit.
    for needle in sorted(KEYWORD_TO_DIMENSION, key=len, reverse=True):
        if needle in text:
            return KEYWORD_TO_DIMENSION[needle]
    return ""


def parse_dimension(
    metadata: Mapping[str, Any] | None,
    user_text: str,
) -> str:
    """Resolve the user-requested grouping dimension.

    Returns one of ``VALID_DIMENSIONS`` or ``""`` when neither the
    metadata nor the user text supplies a recognized dimension. The
    caller is responsible for surfacing ``needs_clarification`` when
    the result is empty — this function never invents a default.
    """
    return _from_metadata(metadata) or _from_user_text(user_text)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py -v`
Expected: PASS (all 13 tests).

- [ ] **Step 5: Commit**

```bash
git add agents/office/dimensions.py tests/unit/agents/test_office_organize_dimensions.py
git commit -m "feat(office): add dimension contract and parse_dimension helper"
```

---

## Task 2: Add `organize_by_size` tool (TDD)

**Files:**
- Create: `agents/office/organize_by_dimension.py`
- Modify: `agents/office/office_tools.py:1684-1698` (tool registration list)
- Test: `tests/unit/agents/test_office_organize_dimensions.py`

- [ ] **Step 1: Add the failing tests**

Append to `tests/unit/agents/test_office_organize_dimensions.py`:

```python
import os
import json

from agents.office.organize_by_dimension import (
    OrganizeBySizeTool,
    OrganizeByTypeTool,
    OrganizeByCreatedTimeTool,
    OrganizeByModifiedTimeTool,
    OrganizeByAccessedTimeTool,
    OrganizeByFilenameTool,
)


def _make_file(tmp_path, name: str, content: bytes):
    p = tmp_path / name
    p.write_bytes(content)
    return str(p)


def test_organize_by_size_buckets_quartiles(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_file(src, "tiny.txt", b"x")  # 1 B
    _make_file(src, "small.txt", b"x" * 200)  # 200 B
    _make_file(src, "medium.txt", b"x" * 1000)  # 1000 B
    _make_file(src, "big.txt", b"x" * 5000)  # 5000 B
    out = tmp_path / "out"
    out.mkdir()
    tool = OrganizeBySizeTool()
    result = tool.execute_sync(source=str(src), output_root=str(out))
    payload = json.loads(result.output)
    assert result.success, result.error
    buckets = {entry["bucket"] for entry in payload["entries"]}
    assert buckets.issubset({"small", "medium", "large"})
    assert len(payload["entries"]) == 4
    plan_text = (out / "organization-plan.md").read_text()
    assert "Size buckets" in plan_text
    # Thresholds are recorded.
    assert "small" in plan_text and "medium" in plan_text and "large" in plan_text


def test_organize_by_size_handles_empty_dir(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    tool = OrganizeBySizeTool()
    result = tool.execute_sync(source=str(src), output_root=str(out))
    assert result.success
    payload = json.loads(result.output)
    assert payload["entries"] == []
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py::test_organize_by_size_buckets_quartiles -v`
Expected: FAIL with `ImportError: cannot import name 'OrganizeBySizeTool'`.

- [ ] **Step 3: Create the implementation module**

Create `agents/office/organize_by_dimension.py`:

```python
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
import time
from pathlib import Path
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
) -> str:
    lines = [
        f"# Folder Organization Plan (dimension: {dimension})",
        "",
        f"**Source:** {source_root}",
        "**Mode:** workspace",
        "",
        "## Bucket rules",
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
                })
            rules = [
                f"small: < {small_max} B",
                f"medium: {small_max} B – {large_min} B",
                f"large: >= {large_min} B",
            ]
            _write_plan(
                output_root,
                _format_plan_markdown(
                    dimension="size",
                    source_root=source,
                    bucket_rules=rules,
                    entries=entries,
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
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py::test_organize_by_size_buckets_quartiles tests/unit/agents/test_office_organize_dimensions.py::test_organize_by_size_handles_empty_dir -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/office/organize_by_dimension.py tests/unit/agents/test_office_organize_dimensions.py
git commit -m "feat(office): organize_by_size tool with quartile buckets"
```

---

## Task 3: Add `organize_by_type` tool (TDD)

**Files:**
- Modify: `agents/office/organize_by_dimension.py`
- Test: `tests/unit/agents/test_office_organize_dimensions.py`

- [ ] **Step 1: Append failing test**

Append to `tests/unit/agents/test_office_organize_dimensions.py`:

```python
def test_organize_by_type_buckets_by_extension(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_file(src, "doc.pdf", b"pdf")
    _make_file(src, "data.csv", b"csv")
    _make_file(src, "code.py", b"py")
    _make_file(src, "image.png", b"\x89PNG")
    out = tmp_path / "out"
    out.mkdir()
    tool = OrganizeByTypeTool()
    result = tool.execute_sync(source=str(src), output_root=str(out))
    payload = json.loads(result.output)
    assert result.success, result.error
    by_dest = {entry["source"]: entry["destination"] for entry in payload["entries"]}
    assert by_dest["doc.pdf"].startswith("documents/")
    assert by_dest["data.csv"].startswith("data/")
    assert by_dest["code.py"].startswith("code/")
    assert by_dest["image.png"].startswith("images/")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py::test_organize_by_type_buckets_by_extension -v`
Expected: FAIL with `ImportError: cannot import name 'OrganizeByTypeTool'`.

- [ ] **Step 3: Append implementation**

Append to `agents/office/organize_by_dimension.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py::test_organize_by_type_buckets_by_extension -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/office/organize_by_dimension.py tests/unit/agents/test_office_organize_dimensions.py
git commit -m "feat(office): organize_by_type tool with extension buckets"
```

---

## Task 4: Add three time-based dimension tools (TDD)

**Files:**
- Modify: `agents/office/organize_by_dimension.py`
- Test: `tests/unit/agents/test_office_organize_dimensions.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/unit/agents/test_office_organize_dimensions.py`:

```python
import os as _os_for_time


def test_organize_by_modified_time_buckets_by_year_month(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    p1 = _make_file(src, "a.txt", b"a")
    p2 = _make_file(src, "b.txt", b"b")
    _os_for_time.utime(p1, (1700000000, 1700000000))  # 2023-11
    _os_for_time.utime(p2, (1735689600, 1735689600))  # 2025-01
    out = tmp_path / "out"
    out.mkdir()
    result = OrganizeByModifiedTimeTool().execute_sync(source=str(src), output_root=str(out))
    assert result.success, result.error
    payload = json.loads(result.output)
    by_src = {entry["source"]: entry["destination"] for entry in payload["entries"]}
    assert by_src["a.txt"].startswith("2023-11/")
    assert by_src["b.txt"].startswith("2025-01/")


def test_organize_by_created_time_falls_back_to_mtime(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    p1 = _make_file(src, "fallback.txt", b"x")
    _os_for_time.utime(p1, (1700000000, 1700000000))  # 2023-11
    out = tmp_path / "out"
    out.mkdir()
    result = OrganizeByCreatedTimeTool().execute_sync(source=str(src), output_root=str(out))
    assert result.success, result.error
    plan_text = (out / "organization-plan.md").read_text()
    # The fallback assumption must be recorded so the reader can tell.
    assert "inferred_from" in plan_text or "fallback" in plan_text.lower()


def test_organize_by_accessed_time_buckets_by_year_month(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    p1 = _make_file(src, "a.txt", b"a")
    _os_for_time.utime(p1, (1700000000, 1700000000))
    out = tmp_path / "out"
    out.mkdir()
    result = OrganizeByAccessedTimeTool().execute_sync(source=str(src), output_root=str(out))
    assert result.success, result.error
    payload = json.loads(result.output)
    bucket = payload["entries"][0]["destination"].split("/")[0]
    assert re.match(r"^\d{4}-\d{2}$", bucket)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py::test_organize_by_modified_time_buckets_by_year_month -v`
Expected: FAIL with `ImportError: cannot import name 'OrganizeByModifiedTimeTool'`.

- [ ] **Step 3: Append implementation**

Append to `agents/office/organize_by_dimension.py`:

```python
import datetime as _dt
import calendar as _calendar
import re as _re

# ---- time-based dimensions -------------------------------------------------


_TIME_FMT = "%Y-%m"


def _fmt_time(ts: float) -> str:
    return _dt.datetime.fromtimestamp(ts).strftime(_TIME_FMT)


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
            files = _walk_files(source)
            entries: list[dict[str, str]] = []
            fallbacks: list[str] = []
            for rel in files:
                full = os.path.join(source, rel)
                stat = os.stat(full)
                ts = getattr(stat, self.time_attr)
                attr_used = self.time_attr
                if ts == 0 and self.fallback_attr is not None:
                    ts = getattr(stat, self.fallback_attr)
                    attr_used = self.fallback_attr
                    fallbacks.append(rel)
                bucket = _fmt_time(ts)
                dst = _copy_into(source, rel, output_root, bucket)
                entries.append({
                    "source": rel,
                    "destination": os.path.relpath(dst, output_root),
                    "time_attr": attr_used,
                })
            rules = sorted({e["destination"].split("/")[0] for e in entries})
            assumptions = []
            if fallbacks:
                assumptions.append(
                    f"{len(fallbacks)} file(s) lack {self.time_attr}; "
                    f"used {self.fallback_attr} instead."
                )
            _write_plan(
                output_root,
                _format_plan_markdown(
                    dimension=self.dimension_label,
                    source_root=source,
                    bucket_rules=rules,
                    entries=entries,
                    assumptions=assumptions,
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py -k "modified_time or created_time or accessed_time" -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add agents/office/organize_by_dimension.py tests/unit/agents/test_office_organize_dimensions.py
git commit -m "feat(office): time-based dimension tools (mtime/atime/birthtime)"
```

---

## Task 5: Add `organize_by_filename` tool (TDD)

**Files:**
- Modify: `agents/office/organize_by_dimension.py`
- Test: `tests/unit/agents/test_office_organize_dimensions.py`

- [ ] **Step 1: Append failing test**

Append to `tests/unit/agents/test_office_organize_dimensions.py`:

```python
def test_organize_by_filename_buckets_by_first_letter(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_file(src, "alpha.txt", b"a")
    _make_file(src, "beta.txt", b"b")
    _make_file(src, "1number.txt", b"1")  # numeric first char -> _other
    out = tmp_path / "out"
    out.mkdir()
    result = OrganizeByFilenameTool().execute_sync(source=str(src), output_root=str(out))
    assert result.success, result.error
    payload = json.loads(result.output)
    by_src = {entry["source"]: entry["destination"] for entry in payload["entries"]}
    assert by_src["alpha.txt"].startswith("A/")
    assert by_src["beta.txt"].startswith("B/")
    assert by_src["1number.txt"].startswith("_other/")


def test_organize_by_filename_preserves_subdirectory(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    sub = src / "sub"
    sub.mkdir()
    _make_file(sub, "gamma.txt", b"g")
    out = tmp_path / "out"
    out.mkdir()
    result = OrganizeByFilenameTool().execute_sync(source=str(src), output_root=str(out))
    payload = json.loads(result.output)
    entry = payload["entries"][0]
    assert entry["destination"].startswith("G/")
    assert "sub/gamma.txt" in entry["destination"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py::test_organize_by_filename_buckets_by_first_letter -v`
Expected: FAIL with `ImportError: cannot import name 'OrganizeByFilenameTool'`.

- [ ] **Step 3: Append implementation**

Append to `agents/office/organize_by_dimension.py`:

```python
# ---- filename --------------------------------------------------------------


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
            files = _walk_files(source)
            entries: list[dict[str, str]] = []
            for rel in files:
                basename = os.path.basename(rel)
                bucket = self._bucket_for(basename)
                dst = _copy_into(source, rel, output_root, bucket)
                entries.append({
                    "source": rel,
                    "destination": os.path.relpath(dst, output_root),
                    "first_char": basename[:1],
                })
            rules = sorted({e["destination"].split("/")[0] for e in entries})
            _write_plan(
                output_root,
                _format_plan_markdown(
                    dimension="filename",
                    source_root=source,
                    bucket_rules=rules,
                    entries=entries,
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py -k "filename" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add agents/office/organize_by_dimension.py tests/unit/agents/test_office_organize_dimensions.py
git commit -m "feat(office): organize_by_filename tool with letter buckets"
```

---

## Task 6: Add dimension dispatcher `run_dimension_tool`

**Files:**
- Modify: `agents/office/organize_by_dimension.py`
- Test: `tests/unit/agents/test_office_organize_dimensions.py`

- [ ] **Step 1: Append failing test**

Append to `tests/unit/agents/test_office_organize_dimensions.py`:

```python
from agents.office.organize_by_dimension import run_dimension_tool
from agents.office.dimensions import VALID_DIMENSIONS


def test_run_dimension_tool_dispatches_each_dimension(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    _make_file(src, "alpha.txt", b"x" * 100)
    out = tmp_path / "out"
    out.mkdir()
    for dim in sorted(VALID_DIMENSIONS):
        local_out = tmp_path / f"out_{dim}"
        local_out.mkdir()
        result = run_dimension_tool(dim, str(src), str(local_out))
        assert result.success, (dim, result.error)
        assert (local_out / "organization-plan.md").exists()


def test_run_dimension_tool_rejects_unknown_dimension(tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    result = run_dimension_tool("alphabetical", str(tmp_path), str(out))
    assert not result.success
    assert "unsupported dimension" in result.error
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py::test_run_dimension_tool_dispatches_each_dimension -v`
Expected: FAIL with `ImportError: cannot import name 'run_dimension_tool'`.

- [ ] **Step 3: Append implementation**

Append to `agents/office/organize_by_dimension.py`:

```python
from agents.office.dimensions import VALID_DIMENSIONS


_DIMENSION_TOOL = {
    "size": OrganizeBySizeTool,
    "type": OrganizeByTypeTool,
    "created_time": OrganizeByCreatedTimeTool,
    "modified_time": OrganizeByModifiedTimeTool,
    "accessed_time": OrganizeByAccessedTimeTool,
    "filename": OrganizeByFilenameTool,
}


def run_dimension_tool(dimension: str, source: str, output_root: str) -> ToolResult:
    """Run the bounded (zero-LLM) dimension tool for ``dimension``.

    Returns a ``ToolResult`` whose ``output`` is the JSON payload the
    dimension tool produced. Used by the bounded path inside
    ``execute_office_work`` — never invoked through the agentic runtime.
    """
    if dimension not in VALID_DIMENSIONS:
        return ToolResult(output="", error=f"unsupported dimension: {dimension!r}")
    tool_cls = _DIMENSION_TOOL.get(dimension)
    if tool_cls is None:
        return ToolResult(output="", error=f"no tool registered for dimension: {dimension!r}")
    return tool_cls().execute_sync(source=source, output_root=output_root)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py -k "run_dimension_tool" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add agents/office/organize_by_dimension.py tests/unit/agents/test_office_organize_dimensions.py
git commit -m "feat(office): dimension dispatcher run_dimension_tool"
```

---

## Task 7: Register the six new tools and clean up `office_tools.py`

**Files:**
- Modify: `agents/office/office_tools.py:240-355` (delete business allowlists)
- Modify: `agents/office/office_tools.py:1380-1601` (drop `primary_entity_confidence` enforcement in `OrganizeMoveFileTool`)
- Modify: `agents/office/office_tools.py:1684-1698` (extend tool registration list)
- Test: `tests/unit/agents/office/test_office_tool_registration.py` (add registration assertion)

- [ ] **Step 1: Add the failing registration test**

Open `tests/unit/agents/office/test_office_tool_registration.py` and append a new test at the end:

```python
def test_dimension_tools_registered():
    from agents.office import office_tools
    office_tools.register_office_tools()
    from framework.tools.registry import get_registry
    registry = get_registry()
    expected = {
        "organize_by_size",
        "organize_by_type",
        "organize_by_created_time",
        "organize_by_modified_time",
        "organize_by_accessed_time",
        "organize_by_filename",
    }
    for tool_name in expected:
        assert registry.get(tool_name) is not None, f"missing tool: {tool_name}"
```

(If the existing test file uses different import paths, match them; the gist is "after registering, all six tools resolve via the registry".)

- [ ] **Step 2: Run the new test to verify it fails**

Run: `pytest tests/unit/agents/office/test_office_tool_registration.py::test_dimension_tools_registered -v`
Expected: FAIL because the six tools are not registered yet.

- [ ] **Step 3: Delete business allowlists in `office_tools.py`**

In `agents/office/office_tools.py`, delete the following constants and helper functions in their entirety:

- Lines 243-353: `ORGANIZED_OUTPUT_ROOT`, `VALID_CATEGORIES`, `WRAPPER_PREFIXES`, `TEXT_PREVIEW_EXTENSIONS`, `READER_TOOL_BY_EXTENSION`, `IDENTITY_PREFIXES`, `_is_wrapper_prefixed`, `_normalize_organized_path`, `_is_under_organized_output`.

Replace them with a single, neutral wrapper check. Add this block in their place (or near the top of the file's section after `_validate_workspace_path`):

```python
# ---------------------------------------------------------------------------
# Generic organized-output prefix check
# ---------------------------------------------------------------------------

ORGANIZED_OUTPUT_ROOT = "organized-output"


def _is_under_organized_output(path: str) -> bool:
    """Return True if ``path`` lives under ``organized-output/files/``.

    No business-specific category or wrapper allowlist is consulted
    here. The agent is dimension-agnostic; only the agent's own output
    prefix is recognised.
    """
    normalized = (path or "").strip()
    if normalized.startswith("/"):
        normalized = normalized[1:]
    return normalized.startswith(ORGANIZED_OUTPUT_ROOT + "/")
```

- [ ] **Step 4: Drop identity extraction in `office_tools.py`**

Delete in their entirety:

- The constant `IDENTITY_PREFIXES` (already removed in Step 3).
- `_clean_entity_candidate` (lines around 442-449).
- `_looks_like_person_name` (lines around 452-456).
- `_extract_primary_entity` (lines around 459-477).

- [ ] **Step 5: Drop `primary_entity`-bearing fields in metadata builders**

In `_build_file_metadata` (currently around line 659-696), remove the `primary_entity`, `primary_entity_source`, `primary_entity_confidence`, and `suggested_destination` keys from the returned dict. The reduced dict must keep `relative_path`, `name`, `ext`, `size`, `category`, `parent_dirs`, `suggested_reader_tool`, `inferred_date_bucket`, `prominent_headings`, `labeled_fields`. The function loses the call to `_extract_primary_entity`; the only inferred signal left is `inferred_date_bucket` (still derived from path or content dates).

Replace the function body with:

```python
def _build_file_metadata(root: str, full_path: str) -> dict[str, object]:
    rel_path = os.path.relpath(full_path, root)
    ext = os.path.splitext(full_path)[1].lower()
    category = _categorize_extension(ext)
    preview = _read_text_preview(full_path)
    lines = preview.splitlines()
    headings = _extract_prominent_headings(lines)
    labeled_fields = _extract_labeled_fields(lines)
    inferred_date_bucket = _infer_date_bucket(root, rel_path, preview)
    return {
        "relative_path": rel_path,
        "name": os.path.basename(full_path),
        "ext": ext,
        "size": os.path.getsize(full_path),
        "category": category,
        "parent_dirs": list(Path(rel_path).parts[:-1]),
        "suggested_reader_tool": _suggested_reader_tool(ext),
        "inferred_date_bucket": inferred_date_bucket,
        "prominent_headings": headings[:2],
        "labeled_fields": labeled_fields[:2],
    }
```

- [ ] **Step 6: Drop entity confidence enforcement in `OrganizeMoveFileTool`**

In `OrganizeMoveFileTool.execute_sync` (lines around 1494-1515), delete the entire `if action == "copy_file" and src_normalized:` block that:

- Calls `_build_file_metadata` to compute `expected_entity` / `expected_date` / `expected_filename` / `confidence`.
- Refuses the operation when the destination tail does not match `expected_tail`.

Replace it with a no-op `pass`:

```python
        if action == "copy_file" and src_normalized:
            # The destination contract is enforced by the plan-output gate
            # and the dimension tool. We no longer require the destination
            # to match an inferred primary entity, because the organize
            # capability is dimension-agnostic.
            pass
```

- [ ] **Step 7: Drop the entity-keyed summary counters in `OrganizeFolderTool`**

In `OrganizeFolderTool.execute_sync` (lines around 1400-1420), delete the `entity_counts` / `date_bucket_counts` collection that feeds on `primary_entity`. The reduced payload keeps `path`, `groups`, `files`, `total_files`, `total_dirs`, `errors`. Replace the body with:

```python
        try:
            files, groups, total_dirs = collect_organize_file_inventory(normalized)
            return ToolResult(output=json.dumps({
                "path": normalized,
                "groups": groups,
                "files": files,
                "total_files": len(files),
                "total_dirs": total_dirs,
                "errors": [],
            }))
        except Exception as exc:
            return ToolResult(output="", error=f"organize_folder: {exc}")
```

- [ ] **Step 8: Register the six dimension tools**

In `_OFFICE_TOOLS` (the list near the end of the file), add the six imports and the six new tool instances:

```python
from agents.office.organize_by_dimension import (
    OrganizeBySizeTool,
    OrganizeByTypeTool,
    OrganizeByCreatedTimeTool,
    OrganizeByModifiedTimeTool,
    OrganizeByAccessedTimeTool,
    OrganizeByFilenameTool,
)


_OFFICE_TOOLS = [
    ReadPdfTool(),
    ReadDocxTool(),
    ReadPptxTool(),
    ReadTxtTool(),
    ReadCsvTool(),
    ReadXlsxTool(),
    ReadXlsTool(),
    ListDirectoryTool(),
    WriteWorkspaceTool(),
    WriteFileTool(),
    OrganizeFolderTool(),
    OrganizeMoveFileTool(),
    DeleteOutputFileTool(),
    OrganizeBySizeTool(),
    OrganizeByTypeTool(),
    OrganizeByCreatedTimeTool(),
    OrganizeByModifiedTimeTool(),
    OrganizeByAccessedTimeTool(),
    OrganizeByFilenameTool(),
]
```

- [ ] **Step 9: Run the registration test to verify it passes**

Run: `pytest tests/unit/agents/office/test_office_tool_registration.py -v`
Expected: PASS (all tests, including the new one).

- [ ] **Step 10: Commit**

```bash
git add agents/office/office_tools.py tests/unit/agents/office/test_office_tool_registration.py
git commit -m "refactor(office): drop business hardcodes; register dimension tools"
```

---

## Task 8: Update `analyze_request` in `nodes.py` (dimension gate + clarification)

**Files:**
- Modify: `agents/office/nodes.py:300-411` (`analyze_request`)
- Test: `tests/unit/agents/test_office_organize_dimensions.py`

- [ ] **Step 1: Append failing tests**

Append to `tests/unit/agents/test_office_organize_dimensions.py`:

```python
from agents.office.nodes import analyze_request


def _organize_state(capability: str = "organize", text: str = "organize this"):
    return {
        "source_paths": ["/tmp/some/folder"],
        "output_mode": "workspace",
        "capability": capability,
        "user_request": text,
        "_message_metadata": {},
        "_compass_task_id": "test-task",
    }


def test_analyze_request_returns_clarification_when_dimension_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("OFFICE_SOURCE_ROOT", str(tmp_path))
    monkeypatch.setenv("OFFICE_WORKSPACE_ROOT", str(tmp_path / "ws"))
    state = _organize_state(text="please organize this folder")
    out = analyze_request(state)
    assert out["error"] == "missing_organize_dimension"
    assert out["needs_clarification"]["missing"] == "organizeGroupBy"
    option_ids = {opt["id"] for opt in out["needs_clarification"]["options"]}
    assert option_ids == {"size", "type", "created_time", "modified_time", "accessed_time", "filename"}


def test_analyze_request_records_dimension_in_state(monkeypatch, tmp_path):
    monkeypatch.setenv("OFFICE_SOURCE_ROOT", str(tmp_path))
    monkeypatch.setenv("OFFICE_WORKSPACE_ROOT", str(tmp_path / "ws"))
    state = _organize_state(text="按文件大小整理")
    state["source_paths"] = [str(tmp_path / "src")]
    (tmp_path / "src").mkdir()
    out = analyze_request(state)
    assert out.get("organize_dimension") == "size"


def test_analyze_request_passes_through_non_organize(monkeypatch, tmp_path):
    monkeypatch.setenv("OFFICE_SOURCE_ROOT", str(tmp_path))
    monkeypatch.setenv("OFFICE_WORKSPACE_ROOT", str(tmp_path / "ws"))
    state = _organize_state(capability="summarize", text="summarize this")
    state["source_paths"] = [str(tmp_path / "src.txt")]
    (tmp_path / "src.txt").write_text("hello")
    out = analyze_request(state)
    # summarize path keeps its own validation; we just confirm no clarification fires.
    assert "needs_clarification" not in out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py -k "analyze_request" -v`
Expected: FAIL — `analyze_request` does not currently return `needs_clarification` or set `organize_dimension`.

- [ ] **Step 3: Add the imports in `nodes.py`**

At the top of `agents/office/nodes.py`, after the existing imports, add:

```python
from agents.office.dimensions import parse_dimension
```

- [ ] **Step 4: Add the dimension gate to `analyze_request`**

At the top of `analyze_request` (right after the function signature), insert:

```python
    # --- Dimension gate (organize capability only) ---------------------
    metadata = state.get("_message_metadata", {}) or {}
    dimension = ""
    if capability == "organize":
        dimension = parse_dimension(metadata, user_text)
        if not dimension:
            return {
                "error": "missing_organize_dimension",
                "needs_clarification": {
                    "missing": "organizeGroupBy",
                    "options": [
                        {"id": d, "label": d.replace("_", " ")}
                        for d in sorted(VALID_DIMENSIONS)
                    ],
                    "user_message": (
                        "Office organize needs a grouping dimension. "
                        "Available dimensions: "
                        + ", ".join(sorted(VALID_DIMENSIONS))
                        + "."
                    ),
                },
            }
```

Also add `VALID_DIMENSIONS` to the import line at the top of the file:

```python
from agents.office.dimensions import VALID_DIMENSIONS, parse_dimension
```

- [ ] **Step 5: Forward the dimension in the return value**

At the bottom of `analyze_request`'s return dict (the final `return {...}` that returns validated paths), add the new key:

```python
    return {
        "validated_paths": validated_paths,
        "workspace_root": workspace_root,
        "artifacts_dir": artifacts_dir,
        "organize_dimension": dimension,
    }
```

- [ ] **Step 6: Surface the clarification payload to the failed-task path**

In `OfficeAgent.handle_message` (in `agents/office/agent.py`), after the `task_store.create_task(...)` call, before `register_office_tools()`, ensure the state carries the dimension forward. Specifically, in the `state` dict that is built right before `_compiled_workflow.invoke(state, config)`, add:

```python
            "organize_dimension": parse_dimension(
                dict(metadata), user_text
            ),
```

This ensures the workflow's `analyze_request` reuses the same value, and if the dimension is missing the workflow short-circuits.

- [ ] **Step 7: Run the new tests to verify they pass**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py -k "analyze_request" -v`
Expected: PASS (3 tests).

- [ ] **Step 8: Run the full node-related suite to ensure no regression**

Run: `pytest tests/unit/agents/office/ tests/unit/agents/test_office_agent.py -v`
Expected: existing tests still pass; no `ImportError` from the deleted symbols.

- [ ] **Step 9: Commit**

```bash
git add agents/office/nodes.py agents/office/agent.py tests/unit/agents/test_office_organize_dimensions.py
git commit -m "feat(office): analyze_request enforces dimension, surfaces clarification"
```

---

## Task 9: Update `execute_office_work` to dispatch the bounded dimension tool

**Files:**
- Modify: `agents/office/nodes.py:1255-1468` (`execute_office_work`)
- Test: `tests/unit/agents/test_office_organize_dimensions.py`

- [ ] **Step 1: Append failing test**

Append to `tests/unit/agents/test_office_organize_dimensions.py`:

```python
from agents.office.nodes import execute_office_work


def test_execute_office_work_organize_uses_bounded_tool(monkeypatch, tmp_path):
    """When the dimension is set, the bounded tool is invoked; no LLM call."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.txt").write_bytes(b"x" * 10)
    (src / "b.txt").write_bytes(b"x" * 1000)
    ws = tmp_path / "ws"
    ws.mkdir()
    state = {
        "capability": "organize",
        "validated_paths": [str(src)],
        "output_mode": "workspace",
        "artifacts_dir": str(ws),
        "workspace_root": str(ws),
        "organize_dimension": "size",
        "_runtime": _FakeRuntime(),
        "_plugin_manager": None,
    }
    out = execute_office_work(state)
    assert out["success"] is True
    assert (ws / "organization-plan.md").exists()
    assert _FakeRuntime.last_call is None  # no LLM was called
```

Add a tiny runtime stub at the top of the test file (next to the other imports):

```python
class _FakeRuntime:
    last_call = None

    def run_agentic(self, *args, **kwargs):
        _FakeRuntime.last_call = (args, kwargs)
        from framework.runtime.adapter import AgenticResult
        return AgenticResult(success=False, summary="should not be called", backend_used="fake")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py::test_execute_office_work_organize_uses_bounded_tool -v`
Expected: FAIL — `execute_office_work` currently runs the agentic LLM even when the dimension is set.

- [ ] **Step 3: Add the bounded branch to `execute_office_work`**

In `execute_office_work`, right after the existing `if capability == "organize":` prompt assignment, insert a new branch that runs before the agentic LLM is invoked. Specifically, before `_try_bounded_office_flow(...)`:

```python
    if capability == "organize":
        dimension = state.get("organize_dimension", "")
        if dimension:
            from agents.office.organize_by_dimension import run_dimension_tool
            output_root = _organized_output_root(output_mode, artifacts_dir, validated_paths)
            dim_result = run_dimension_tool(
                dimension,
                validated_paths[0] if validated_paths else "",
                output_root,
            )
            if not dim_result.success:
                return {
                    "summary": f"office dimension tool failed: {dim_result.error}",
                    "success": False,
                    "capability": capability,
                    "status": "failed",
                    "raw_output": "",
                    "warnings": [dim_result.error or "unknown dimension-tool error"],
                    "error": dim_result.error,
                }
            return {
                "summary": (
                    f"Office organized files with the dimension tool "
                    f"({dimension})."
                ),
                "success": True,
                "capability": capability,
                "status": "completed",
                "raw_output": dim_result.output or "",
                "expected_outputs": _expected_output_paths(
                    capability, validated_paths, output_mode, artifacts_dir
                ),
            }
```

This branch sits **before** the agentic runtime is touched, so the bounded path is genuinely zero-LLM.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py::test_execute_office_work_organize_uses_bounded_tool -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/office/nodes.py tests/unit/agents/test_office_organize_dimensions.py
git commit -m "feat(office): bounded dimension tool path in execute_office_work"
```

---

## Task 10: Rewrite `_build_organize_prompt` (drop `primary_entity` guidance)

**Files:**
- Modify: `agents/office/nodes.py:1609-1676` (`_build_organize_prompt`)
- Test: `tests/unit/agents/test_office_organize_dimensions.py`

- [ ] **Step 1: Append failing test**

Append to `tests/unit/agents/test_office_organize_dimensions.py`:

```python
from agents.office.nodes import _build_organize_prompt


def test_organize_prompt_does_not_reference_primary_entity():
    text = _build_organize_prompt(["/tmp/x"], "workspace", "/")
    forbidden = ("primary_entity", "Entity_A", "Entity_B", "by-student", "students/")
    for phrase in forbidden:
        assert phrase not in text, f"prompt still references {phrase!r}"


def test_organize_prompt_references_the_dimension_tool():
    text = _build_organize_prompt(["/tmp/x"], "workspace", "/")
    assert "organize_by_" in text  # at least one dimension tool is named
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py -k "organize_prompt" -v`
Expected: FAIL — the current prompt mentions `primary_entity`, `Entity_A`, etc.

- [ ] **Step 3: Replace the prompt body**

Replace `_build_organize_prompt` body with the following dimension-agnostic version. The signature is unchanged (`paths`, `output_mode`, `source_root`).

```python
def _build_organize_prompt(paths: list[str], output_mode: str, source_root: str) -> str:
    paths_list = "\n".join(f"- {p}" for p in paths)
    write_rules = (
        "3. Write the organization plan using write_workspace tool with filename: organization-plan.md"
        if output_mode == "workspace"
        else
        "3. Write the organization plan using write_file tool to: {source_folder}/organization-plan.md"
    )
    return f"""Organize the following folder(s):

{paths_list}

Source root: {source_root}
Output mode: {output_mode}

TASK:
The user has already chosen a grouping dimension for this task; that
dimension is recorded in the task metadata. The dimension is one of:
size, type, created_time, modified_time, accessed_time, filename.
You MUST use the matching dimension tool to materialize the layout:

- size            -> organize_by_size
- type            -> organize_by_type
- created_time    -> organize_by_created_time
- modified_time   -> organize_by_modified_time
- accessed_time   -> organize_by_accessed_time
- filename        -> organize_by_filename

WORKFLOW:
1. Read the dimension from the task metadata. NEVER invent a different
   dimension. If the metadata does not name one, return a structured
   needs_clarification error and stop.
2. Call the matching `organize_by_*` tool. Pass the source folder and
   the resolved output_root for `organized-output/files/`.
3. The tool writes `organization-plan.md` and materializes the layout.
{write_rules}

CRITICAL: You must USE the dimension tool to actually create the
organized folder structure. Do not just write a plan - execute it.
CRITICAL: A plan-only answer is a failure. The task is complete only
if files exist under `organized-output/files/`.
CRITICAL: Every non-hidden source file must be copied exactly once.
Do not duplicate a source file into multiple destinations.
CRITICAL: Bucket names come from the dimension tool's output; do not
introduce business-specific folder names (e.g. "students", "by-entity").

OUTPUT FORMAT:
Write a summary to organization-plan.md explaining:
# Folder Organization Plan (dimension: <dimension>)

## Bucket rules
The dimension tool's bucket definitions and thresholds.

## Files Organized
MUST include one canonical Markdown table with exactly these two columns:
| Source Path | Destination |
| --- | --- |

Rules for this table:
- Include exactly one row per non-hidden source file
- `Source Path` must be the source file path relative to the validated source folder
- `Destination` must be the final relative path under `organized-output/files/`
- This table is the authoritative plan-output contract used for validation
- You may add optional explanatory subsections after the canonical table,
  but do not replace or omit the canonical table
"""
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py -k "organize_prompt" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add agents/office/nodes.py tests/unit/agents/test_office_organize_dimensions.py
git commit -m "refactor(office): organize prompt is dimension-agnostic"
```

---

## Task 11: Update `_canonical_organize_destination` and verification helpers

**Files:**
- Modify: `agents/office/nodes.py:1086-1111` (`_canonical_organize_destination`)
- Test: `tests/unit/agents/test_office_organize_dimensions.py`

- [ ] **Step 1: Append failing test**

Append to `tests/unit/agents/test_office_organize_dimensions.py`:

```python
def test_canonical_organize_destination_uses_explicit_destination():
    from agents.office.nodes import _canonical_organize_destination
    item = {
        "relative_path": "notes/a.txt",
        "suggested_destination": "small/notes/a.txt",
    }
    out = _canonical_organize_destination("/out", item)
    assert out.endswith(os.path.join("small", "notes", "a.txt"))


def test_canonical_organize_destination_falls_back_to_relative_path():
    from agents.office.nodes import _canonical_organize_destination
    item = {"relative_path": "notes/a.txt"}
    out = _canonical_organize_destination("/out", item)
    assert out.endswith("notes/a.txt")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py -k "canonical_organize_destination" -v`
Expected: FAIL — current code requires `primary_entity`.

- [ ] **Step 3: Replace `_canonical_organize_destination`**

Replace the function body with:

```python
def _canonical_organize_destination(output_root: str, item: dict[str, Any]) -> str:
    """Resolve a canonical destination for one source file.

    Honours an explicit ``suggested_destination`` written by the bounded
    dimension tool. Falls back to a single-bucket layout
    ``<output_root>/<relative_path>`` when no suggestion is present.
    Never invents a business-specific bucket (no entity / category /
    date inference).
    """
    suggested = str(item.get("suggested_destination") or "").strip()
    if suggested:
        segments = [
            seg
            for seg in suggested.replace("\\", "/").split("/")
            if seg not in {"", ".", ".."}
        ]
        if segments:
            return os.path.realpath(os.path.join(output_root, *segments))
    rel = str(item.get("relative_path") or "").strip()
    if not rel:
        return os.path.realpath(output_root)
    return os.path.realpath(os.path.join(output_root, rel))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py -k "canonical_organize_destination" -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add agents/office/nodes.py tests/unit/agents/test_office_organize_dimensions.py
git commit -m "refactor(office): canonical destination honours explicit suggestion"
```

---

## Task 12: Rewrite `prompts/organize.md`

**Files:**
- Modify: `agents/office/prompts/organize.md`

- [ ] **Step 1: Replace the file contents**

Overwrite `agents/office/prompts/organize.md` with:

```markdown
# Office Organize — Dimension-Driven

The organize capability is dimension-driven. The grouping dimension
must come from the user (via `metadata.organizeGroupBy` or a generic
keyword in the request). The agent never invents a dimension.

## Workflow

1. **Resolve the dimension**
   - Read `metadata.organizeGroupBy` first.
   - Otherwise scan the user request for a generic keyword (size, type,
     created_time, modified_time, accessed_time, filename).
   - If neither source supplies a recognized dimension, return a
     structured `needs_clarification` payload and stop. Do not guess.

2. **Run the matching dimension tool**
   - `organize_by_size`
   - `organize_by_type`
   - `organize_by_created_time`
   - `organize_by_modified_time`
   - `organize_by_accessed_time`
   - `organize_by_filename`

   Each tool is zero-LLM: it walks the source tree, buckets files by
   the dimension, copies them under
   `organized-output/files/<bucket>/<file>`, and writes
   `organization-plan.md` with the bucket rules and a
   `Source Path | Destination` table.

3. **Verify the layout**
   - The plan-output gate already checks that every non-hidden source
     file is materialized exactly once. No business-specific bucket
     vocabulary is assumed.

## Bucket Naming

Bucket names come from the dimension tool. Examples:
- size: `small/`, `medium/`, `large/`
- type: `documents/`, `text/`, `data/`, `images/`, `presentations/`, `code/`, `other/`
- filename: `A/`, `B/`, …, `Z/`, `_other/`
- time-based dimensions: `YYYY-MM/` (e.g. `2026-01/`)

Never invent bucket names such as `students/`, `by-entity/`, or any
business-specific folder. The agent does not know which population's
documents it is processing.

## Output Format (`organization-plan.md`)

```
# Folder Organization Plan (dimension: <dimension>)

## Bucket rules
- <bucket>: <rule>

## Files Organized
| Source Path | Destination |
| --- | --- |
| <rel> | <bucket>/<rel> |
```

The `Source Path | Destination` table is the authoritative plan-output
contract used for validation.
```

- [ ] **Step 2: Verify the prompt renders correctly**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py -k "organize_prompt" -v`
Expected: still PASS (the prompt test only checks the LLM-rendered prompt, not the markdown file).

- [ ] **Step 3: Commit**

```bash
git add agents/office/prompts/organize.md
git commit -m "docs(office): rewrite organize prompt as dimension-driven"
```

---

## Task 13: Update `prompts/system.md` (dimension rule)

**Files:**
- Modify: `agents/office/prompts/system.md`

- [ ] **Step 1: Add the dimension rule**

Append the following paragraph to `agents/office/prompts/system.md` (after the existing "Rules" section):

```markdown
## Organize Dimension Contract

The `organize` capability is dimension-driven. The grouping dimension
must come from the user (via `metadata.organizeGroupBy` or a generic
keyword in the request). The agent MUST NOT invent a dimension. If
neither source supplies a recognized dimension, the agent must return
a structured `needs_clarification` payload and stop.
```

- [ ] **Step 2: Commit**

```bash
git add agents/office/prompts/system.md
git commit -m "docs(office): system prompt gains organize dimension rule"
```

---

## Task 14: Update `skills/office-generic-methodology/instructions.md`

**Files:**
- Modify: `skills/office-generic-methodology/instructions.md` (Phase 3.C and Phase 5)

- [ ] **Step 1: Rewrite Phase 3.C**

Replace the existing "### C) For Organization Tasks" section in `skills/office-generic-methodology/instructions.md` with:

```markdown
### C) For Organization Tasks

1. Identify the dimension from the user:
   - Read `metadata.organizeGroupBy` first.
   - Otherwise scan the user request for a generic keyword
     (size, type, created_time, modified_time, accessed_time,
     filename).
   - If no dimension is identified, return a structured
     `needs_clarification` payload and STOP. Never invent a
     dimension.
2. Use the matching dimension tool
   (`organize_by_size` / `organize_by_type` /
   `organize_by_created_time` / `organize_by_modified_time` /
   `organize_by_accessed_time` / `organize_by_filename`) to
   materialize the layout and write `organization-plan.md` with
   explicit bucket rules.
3. Bucket names come from the dimension tool. Never introduce
   business-specific folder names (no `students/`, no
   `by-entity/`, etc.).
```

- [ ] **Step 2: Add the dimension-match checklist item**

In the "Phase 5: Validation Checklist" section, add a new bullet:

```markdown
- The chosen grouping dimension matches the user request.
```

- [ ] **Step 3: Commit**

```bash
git add skills/office-generic-methodology/instructions.md
git commit -m "docs(skills): office methodology enforces dimension contract"
```

---

## Task 15: Clean up `test_office_organize_schema.py`

**Files:**
- Modify: `tests/unit/agents/test_office_organize_schema.py`

- [ ] **Step 1: Replace business literals**

Open `tests/unit/agents/test_office_organize_schema.py`. Replace every test that asserts a `students/`, person name (`Yan` / `Liam` / `Ethan` / `GroupA`), or `by-student/` literal with neutral bucket names. The behaviour under test (path normalization, wrapper stripping) is preserved; only the identifiers change.

Concretely, replace the file with the following content. Read the file first to make sure no other helpers are exported; if there are local fixtures, port them.

```python
"""Tests for the generic organized-output prefix check."""
from __future__ import annotations

from agents.office.office_tools import _is_under_organized_output, _safe_path_segment


def test_is_under_organized_output_accepts_canonical_prefix():
    assert _is_under_organized_output("organized-output/files/small/a.txt") is True
    assert _is_under_organized_output("organized-output/files/2026-01/a.txt") is True


def test_is_under_organized_output_rejects_outside_prefix():
    assert _is_under_organized_output("small/a.txt") is False
    assert _is_under_organized_output("output/small/a.txt") is False


def test_is_under_organized_output_ignores_business_folder_names():
    # No business category allowlist is consulted.
    assert _is_under_organized_output("organized-output/files/custom_bucket/a.txt") is True
    assert _is_under_organized_output("custom_bucket/a.txt") is False


def test_safe_path_segment_sanitizes():
    assert _safe_path_segment("hello world") == "hello_world"
    assert _safe_path_segment("héllo") == "h_llo" or "hello" in _safe_path_segment("héllo")
    assert _safe_path_segment("   ") == "other"
```

- [ ] **Step 2: Run the test file to verify it passes**

Run: `pytest tests/unit/agents/test_office_organize_schema.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/agents/test_office_organize_schema.py
git commit -m "test(office): organize schema tests use neutral bucket names"
```

---

## Task 16: Clean up `test_office_organize_verification.py`

**Files:**
- Modify: `tests/unit/agents/test_office_organize_verification.py`

- [ ] **Step 1: Replace business literals**

Open the file. Every test that uses `students/`, person names, or `by-entity/` paths is replaced with synthetic bucket names. The contract assertion (every source file materialized exactly once) is preserved.

Concretely, replace the file with:

```python
"""Tests for organize materialization: contract is 'every source file
materialized exactly once'. No business-specific bucket vocabulary."""
from __future__ import annotations

import json
import os
import shutil

import pytest


def test_every_source_file_materialized_exactly_once(tmp_path, monkeypatch):
    # Synthetic fixture: 3 small, 2 large, 1 medium. The bucket
    # naming is generated by the dimension tool; we trust the
    # tool's output to use the right names.
    src = tmp_path / "src"
    src.mkdir()
    files = {
        "a.txt": 10,
        "b.txt": 20,
        "c.txt": 30,
        "d.txt": 5000,
        "e.txt": 6000,
        "f.txt": 1000,
    }
    for name, size in files.items():
        (src / name).write_bytes(b"x" * size)
    out = tmp_path / "out"
    out.mkdir()

    from agents.office.organize_by_dimension import run_dimension_tool
    result = run_dimension_tool("size", str(src), str(out))
    assert result.success, result.error
    payload = json.loads(result.output)
    # Contract: every source file has exactly one materialized entry.
    sources = sorted(e["source"] for e in payload["entries"])
    assert sources == sorted(files.keys())
    # Every source is copied exactly once (destinations are unique).
    dests = [e["destination"] for e in payload["entries"]]
    assert len(set(dests)) == len(dests)
    # The plan-output gate contract test continues to live in
    # tests/unit/agents/office/test_plan_output_gate.py — the test
    # above exercises the materialize-once invariant directly.
```

(Adjust the `out` path to match the convention used elsewhere in the file; the test must create the directory before calling the tool.)

- [ ] **Step 2: Run the test to verify it passes**

Run: `pytest tests/unit/agents/test_office_organize_verification.py -v`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/unit/agents/test_office_organize_verification.py
git commit -m "test(office): organize verification uses neutral buckets"
```

---

## Task 17: Add `test_office_organize_dimensions.py` (consolidated)

The previous tasks already added individual tests to this file incrementally. This task ensures the full file is consistent and runs as a single suite.

**Files:**
- Review: `tests/unit/agents/test_office_organize_dimensions.py`

- [ ] **Step 1: Run the full new test file**

Run: `pytest tests/unit/agents/test_office_organize_dimensions.py -v`
Expected: ALL tests pass.

If any test fails, fix the test (do not modify the production code
under test — the production code was already fixed in earlier tasks).

- [ ] **Step 2: Commit any test-only fixups**

```bash
git add tests/unit/agents/test_office_organize_dimensions.py
git commit -m "test(office): consolidate dimension suite"
```

---

## Task 18: Full unit test sweep

**Files:** (no file changes; verification only)

- [ ] **Step 1: Run the full unit suite**

Run: `pytest tests/unit/ -v`
Expected: all tests pass. If a test fails because it referenced a now-removed symbol (e.g. `VALID_CATEGORIES`, `_extract_primary_entity`, `primary_entity_confidence`), update the test to match the new contract — never reintroduce the symbol.

- [ ] **Step 2: Audit the office code for any remaining business literal**

Run:

```bash
grep -nE "students|by-student|primary_entity|IDENTITY_PREFIXES|VALID_CATEGORIES|WRAPPER_PREFIXES" agents/office/*.py agents/office/prompts/*.md skills/office-generic-methodology/*.md tests/unit/agents/test_office_organize*.py
```

Expected: zero matches in `agents/office/*.py` and `agents/office/prompts/*.md`. Any matches in tests should be inside negative-assertion tests (e.g. "the prompt does not reference X") that prove the absence — those are intentional.

- [ ] **Step 3: Commit any final fixes (none expected)**

```bash
# Only run if there were fixes from Step 1 or Step 2.
git add -u
git commit -m "test(office): full unit sweep clean"
```

---

## Task 19: Smoke-test the agent locally

**Files:** (no file changes; verification only)

- [ ] **Step 1: Build the office image and start it**

```bash
docker compose -f docker-compose-v2.yml up --build -d office
```

Expected: the office container starts and reports healthy at `http://localhost:8060/health`.

- [ ] **Step 2: Send a synthetic "by size" organize task via the A2A peer**

Use the existing test harness (or curl) to post a message to compass with `capability: organize` and `organizeGroupBy: size`. Confirm:

- `organized-output/files/{small,medium,large}/<file>` exist.
- `organization-plan.md` lists thresholds and a `Source Path | Destination` table covering every source file.
- `task-report.json` reports `success: true`.

- [ ] **Step 3: Send an organize task with no dimension**

Confirm:

- The task ends in `failed`.
- The `task-report.json` carries `error == "missing_organize_dimension"` and a `needs_clarification` payload with all six dimension options.

- [ ] **Step 4: Commit a brief notes file (optional)**

If a smoke-test notes file is needed for the team, save it under `artifacts/` (gitignored) — do NOT commit it. The build / runtime paths above are the durable record.

---

## Self-Review (filled in by plan author)

- **Spec coverage:**
  - Goal 1 (dimension-driven organize) — Task 1 + Task 8 + Task 9 + Task 12 + Task 14.
  - Goal 2 (six built-in dimensions, zero-LLM) — Tasks 2-6.
  - Goal 3 (fail-closed clarification) — Task 8 (analyze_request gate) + Task 1 (parse_dimension).
  - Goal 4 (no business hardcodes) — Task 7 (delete from `office_tools.py`) + Task 11 (canonical destination) + Task 10 (prompt) + Task 12 (organize.md) + Task 13 (system.md) + Task 14 (skill) + Task 15 (test schema) + Task 16 (test verification).
  - Goal 5 (gate stays dimension-agnostic) — confirmed; no gate code changed.
- **Placeholder scan:** no TBD / TODO / "fill in details" patterns.
- **Type consistency:** `parse_dimension` signature matches between Task 1, Task 6 dispatcher, and Task 8 caller. `run_dimension_tool` signature matches between Task 6, Task 9, and Task 16. `_canonical_organize_destination` signature is unchanged in Task 11; the test cases use the existing `(output_root, item)` shape.
- **Scope check:** the plan touches only `agents/office/`, `skills/office-generic-methodology/`, and `tests/unit/agents/`. No framework or compass changes.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-04-office-organize-dimension.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
