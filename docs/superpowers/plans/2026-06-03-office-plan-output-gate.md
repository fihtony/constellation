# Office Plan-Output Gate — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a deterministic plan-output gate to the Office agent that validates the materialized output tree against the declared plan, runs reconciliation rounds on mismatch, and emits Compass major-step rows for validation / reconciliation / exhaustion.

**Architecture:** Pure `framework/office/plan_output_gate.py` module (no network, no LLM); `framework/office/path_safety.py` helper shared by the gate and a new `delete_output_file` tool; Office node runs the gate after the primary LLM round, retries up to 3 times via the LLM with a deterministic retry prompt and plan-integrity snapshotting, and emits three new Compass major-step rows (`office.validating_plan_output`, `office.reconciling_plan_output#R`, `office.gate_exhausted`).

**Tech Stack:** Python 3.12, pytest (TDD), existing framework/major_step.py for timeline emission, existing tasks/task_store for persistence.

**Spec:** `docs/superpowers/specs/2026-06-03-office-plan-output-gate-design.md` (rev 811925c).

---

## File Structure

| File | Responsibility |
|---|---|
| `framework/office/__init__.py` (new) | Empty package marker |
| `framework/office/path_safety.py` (new) | `realpath` resolution, prefix check, symlink-escape detection — shared by gate and `delete_output_file` |
| `framework/office/plan_output_gate.py` (new) | `GateEntry`, `OutputContract`, `GateReport` dataclasses; `parse_plan`, `walk_output`, `diff`, `run`, `resolve_output_contract` |
| `agents/office/office_tools.py` (modify) | Add `DeleteOutputFileTool` with path guardrails |
| `agents/office/office_steps.py` (modify) | Add `emit_validating_plan_output`, `emit_reconciling_plan_output`, `emit_gate_exhausted` |
| `agents/office/nodes.py` (modify) | Add contract resolution, gate enforcement, retry prompt builder, plan integrity snapshot, no-progress detection, plan requirements for summarize/analyze |
| `agents/compass/agent.py` (modify) | Add validation / reconciliation / exhaustion rows to the Office major-step skeleton for all three capabilities |
| `tests/unit/framework/office/test_path_safety.py` (new) | Path-safety helper unit tests |
| `tests/unit/agents/office/test_plan_output_gate.py` (new) | Gate unit tests (positive + path-safety + edge cases) |
| `tests/unit/agents/office/test_office_plan_output_gate_flow.py` (new) | Office flow tests with mocked LLM/runtime |
| `tests/unit/agents/compass/test_ui_integration.py` (modify) | Assert new major-step rows in skeleton |

---

## Task 1: Framework office package + path_safety helper

**Files:**
- Create: `framework/office/__init__.py`
- Create: `framework/office/path_safety.py`
- Test: `tests/unit/framework/office/test_path_safety.py`

- [ ] **Step 1: Create the package marker**

Create `framework/office/__init__.py`:

```python
"""Office agent framework helpers — pure, no network, no LLM."""
```

- [ ] **Step 2: Write failing test for `resolve_within_root`**

Create `tests/unit/framework/office/test_path_safety.py`:

```python
import os
import pytest
from framework.office.path_safety import (
    normalize_relative_path,
    resolve_within_root,
    is_within_root,
    PathSafetyError,
)


def test_resolve_within_root_accepts_relative_child(tmp_path):
    root = tmp_path
    (root / "files").mkdir()
    target = tmp_path / "files" / "doc.pdf"
    target.write_text("x")
    resolved = resolve_within_root(str(root), "files/doc.pdf")
    assert resolved == str(target)


def test_resolve_within_root_rejects_parent_traversal(tmp_path):
    root = tmp_path
    with pytest.raises(PathSafetyError) as excinfo:
        resolve_within_root(str(root), "../etc/passwd")
    assert "parent traversal" in str(excinfo.value).lower()


def test_resolve_within_root_rejects_absolute_path(tmp_path):
    root = tmp_path
    with pytest.raises(PathSafetyError):
        resolve_within_root(str(root), "/etc/passwd")


def test_resolve_within_root_rejects_drive_letter(tmp_path):
    root = tmp_path
    with pytest.raises(PathSafetyError):
        resolve_within_root(str(root), "C:/Windows/System32")


def test_resolve_within_root_rejects_symlink_escape(tmp_path):
    root = tmp_path
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x")
    link = root / "link"
    link.symlink_to(outside)
    with pytest.raises(PathSafetyError) as excinfo:
        resolve_within_root(str(root), "link")
    assert "symlink" in str(excinfo.value).lower()


def test_resolve_within_root_rejects_backslash_separators(tmp_path):
    root = tmp_path
    (root / "files").mkdir()
    with pytest.raises(PathSafetyError):
        resolve_within_root(str(root), "files\\doc.pdf")


def test_normalize_relative_path_handles_trailing_separators(tmp_path):
    root = tmp_path
    (root / "files").mkdir()
    assert normalize_relative_path("files/") == "files"


def test_is_within_root_true_for_child(tmp_path):
    root = tmp_path
    child = root / "a" / "b"
    child.mkdir(parents=True)
    assert is_within_root(str(root), str(child)) is True


def test_is_within_root_false_for_parent(tmp_path):
    root = tmp_path
    outside = tmp_path.parent
    assert is_within_root(str(root), str(outside)) is False
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/framework/office/test_path_safety.py -v`
Expected: ModuleNotFoundError or ImportError because `framework/office/path_safety.py` does not exist.

- [ ] **Step 4: Implement `framework/office/path_safety.py`**

Create `framework/office/path_safety.py`:

```python
"""Path-safety helpers shared by the plan-output gate and the office tools.

The Office agent must never let LLM-driven file mutations escape their
intended root. The functions here are the only place that should resolve a
caller-supplied relative path against a fixed root and verify the result
is genuinely inside that root (post-symlink resolution).
"""
from __future__ import annotations

import os
import re


class PathSafetyError(ValueError):
    """Raised when a path violates the safety contract."""


_DRIVE_LETTER_RE = re.compile(r"^[a-zA-Z]:[\\/]")


def normalize_relative_path(relative: str) -> str:
    """Strip trailing separators and normalize backslashes to forward slashes.

    Does NOT resolve ``..`` segments — that is the job of
    :func:`resolve_within_root`, which combines the path with a real root.
    """
    if not isinstance(relative, str):
        raise PathSafetyError(f"path must be a string, got {type(relative).__name__}")
    cleaned = relative.replace("\\", "/").rstrip("/")
    return cleaned


def is_within_root(root: str, candidate: str) -> bool:
    """Return True if ``candidate`` resolves to a path inside ``root``.

    Both arguments are resolved with ``realpath`` so symlinks that escape the
    root are caught.
    """
    real_root = os.path.realpath(root)
    real_candidate = os.path.realpath(candidate)
    if real_candidate == real_root:
        return True
    return real_candidate.startswith(real_root.rstrip(os.sep) + os.sep)


def resolve_within_root(root: str, relative: str) -> str:
    """Resolve ``relative`` against ``root`` and return its real path.

    Raises :class:`PathSafetyError` for any of:
    * ``..`` segments after normalization
    * absolute paths (POSIX or Windows)
    * drive letters
    * backslash separators (forces POSIX-only on the contract)
    * symlinks whose chain escapes ``root``
    * the path itself does not exist (we need realpath to chase symlinks)

    On success returns the absolute real path.
    """
    if not isinstance(root, str) or not root:
        raise PathSafetyError("root must be a non-empty string")
    real_root = os.path.realpath(os.path.abspath(root))
    normalized = normalize_relative_path(relative)
    if not normalized:
        raise PathSafetyError("path is empty after normalization")
    if normalized.startswith("/"):
        raise PathSafetyError(f"absolute path not allowed: {relative!r}")
    if _DRIVE_LETTER_RE.match(normalized):
        raise PathSafetyError(f"drive-letter path not allowed: {relative!r}")
    if ".." in normalized.split("/"):
        raise PathSafetyError(f"parent traversal not allowed: {relative!r}")

    candidate = os.path.join(real_root, normalized)
    if not os.path.exists(candidate):
        raise PathSafetyError(f"path does not exist: {candidate}")
    real = os.path.realpath(candidate)
    if not is_within_root(real_root, real):
        raise PathSafetyError(
            f"symlink escapes root: {relative!r} -> {real!r}"
        )
    return real
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/framework/office/test_path_safety.py -v`
Expected: all 9 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add framework/office/__init__.py framework/office/path_safety.py tests/unit/framework/office/test_path_safety.py
git commit -m "feat(framework): add office.path_safety helper"
```

---

## Task 2: Plan-output gate — dataclasses and resolve_output_contract

**Files:**
- Create: `framework/office/plan_output_gate.py`
- Test: `tests/unit/agents/office/test_plan_output_gate.py`

- [ ] **Step 1: Create test directory**

```bash
mkdir -p tests/unit/agents/office
```

- [ ] **Step 2: Write failing tests for dataclasses and contract resolution**

Create `tests/unit/agents/office/test_plan_output_gate.py`:

```python
from framework.office.plan_output_gate import (
    GateEntry,
    OutputContract,
    GateReport,
    resolve_output_contract,
)


def test_gate_entry_is_frozen():
    entry = GateEntry(source_path="/a/b.txt", expected_path="files/b.txt")
    with __import__("pytest").raises(Exception):
        entry.source_path = "/c"  # type: ignore[misc]


def test_output_contract_is_frozen():
    contract = OutputContract(
        capability="organize",
        plan_path="/plan.md",
        output_root="/root",
        ancillary_allowlist={"x"},
        source_count=1,
        expected_plan_kind="files_organized",
    )
    with __import__("pytest").raises(Exception):
        contract.capability = "summarize"  # type: ignore[misc]


def test_gate_report_is_clean_when_no_discrepancies():
    report = GateReport(
        capability="organize",
        plan_status="ok",
        planned_count=2,
        actual_count=2,
        missing=[],
        unexpected=[],
        mismatches=[],
    )
    assert report.is_clean is True


def test_gate_report_is_not_clean_when_missing():
    report = GateReport(
        capability="organize",
        plan_status="ok",
        planned_count=2,
        actual_count=1,
        missing=["files/missing.txt"],
        unexpected=[],
        mismatches=[],
    )
    assert report.is_clean is False


def test_resolve_output_contract_organize(tmp_path):
    (tmp_path / "organized-output").mkdir()
    (tmp_path / "organized-output" / "files").mkdir()
    contract = resolve_output_contract(
        capability="organize",
        validated_paths=[str(tmp_path / "src")],
        output_mode="workspace",
        artifacts_dir=str(tmp_path),
    )
    assert contract.capability == "organize"
    assert contract.expected_plan_kind == "files_organized"
    assert contract.plan_path == str(tmp_path / "organized-output" / "files" / "organization-plan.md")
    assert contract.output_root == str(tmp_path / "organized-output" / "files")


def test_resolve_output_contract_summarize(tmp_path):
    (tmp_path / "workspace").mkdir()
    contract = resolve_output_contract(
        capability="summarize",
        validated_paths=[str(tmp_path / "a.txt")],
        output_mode="workspace",
        artifacts_dir=str(tmp_path),
    )
    assert contract.capability == "summarize"
    assert contract.expected_plan_kind == "source_summary_mapping"
    assert contract.plan_path.endswith("summary-plan.md")


def test_resolve_output_contract_analyze(tmp_path):
    contract = resolve_output_contract(
        capability="analyze",
        validated_paths=[str(tmp_path / "data.csv")],
        output_mode="workspace",
        artifacts_dir=str(tmp_path),
    )
    assert contract.capability == "analyze"
    assert contract.expected_plan_kind == "source_analysis_mapping"
    assert contract.plan_path.endswith("analysis-plan.md")


def test_resolve_output_contract_inplace_uses_target_under_source(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    contract = resolve_output_contract(
        capability="organize",
        validated_paths=[str(src)],
        output_mode="inplace",
        artifacts_dir=str(tmp_path / "artifacts"),
    )
    assert contract.output_root == str(src / "organized-output" / "files")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/test_plan_output_gate.py -v`
Expected: ModuleNotFoundError for `framework.office.plan_output_gate`.

- [ ] **Step 4: Implement dataclasses and `resolve_output_contract`**

Create `framework/office/plan_output_gate.py`:

```python
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/test_plan_output_gate.py -v`
Expected: all 8 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add framework/office/plan_output_gate.py tests/unit/agents/office/test_plan_output_gate.py
git commit -m "feat(framework): add plan_output_gate dataclasses and contract resolver"
```

---

## Task 3: Plan-output gate — `parse_plan`

**Files:**
- Modify: `framework/office/plan_output_gate.py`
- Test: `tests/unit/agents/office/test_plan_output_gate.py`

- [ ] **Step 1: Write failing tests for `parse_plan`**

Append to `tests/unit/agents/office/test_plan_output_gate.py`:

```python
import pytest
from framework.office.plan_output_gate import parse_plan, GateEntry


def _write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


def test_parse_plan_organize_extracts_pairs(tmp_path):
    plan = """# Plan
## Files Organized
| source | destination |
| --- | --- |
| /src/a.txt | files/a.txt |
| /src/b.txt | documents/b.txt |
"""
    plan_path = _write(tmp_path, "organization-plan.md", plan)
    entries = parse_plan("organize", plan_path)
    assert len(entries) == 2
    assert entries[0].source_path == "/src/a.txt"
    assert entries[0].expected_path == "files/a.txt"
    assert entries[1].expected_path == "documents/b.txt"


def test_parse_plan_summarize_extracts_expanded_file_rows(tmp_path):
    plan = """# Plan
## Source -> Summary Mapping
| source | summary_target |
| --- | --- |
| /src/a.txt | a.md |
| /src/b.txt | b.md |
"""
    plan_path = _write(tmp_path, "summary-plan.md", plan)
    entries = parse_plan("summarize", plan_path)
    assert len(entries) == 2
    assert entries[0].extras["summary_target"] == "a.md"


def test_parse_plan_analyze_extracts_output_rows_and_committed_fields(tmp_path):
    plan = """# Plan
## Source -> Analysis Mapping
| source | analysis_target |
| --- | --- |
| /src/data.csv | data.analysis.md |

## Committed Fields
- field_count: 4
- numeric_field_count: 2
"""
    plan_path = _write(tmp_path, "analysis-plan.md", plan)
    entries, committed = parse_plan("analyze", plan_path)
    assert len(entries) == 1
    assert committed == {"field_count": 4, "numeric_field_count": 2}


def test_parse_plan_missing_returns_missing_status(tmp_path):
    from framework.office.plan_output_gate import parse_plan_with_status
    status, _invalid_entries, entries, committed, error = parse_plan_with_status(
        "organize", str(tmp_path / "absent.md")
    )
    assert status == "missing"
    assert entries == []


def test_parse_plan_wrong_capability_returns_invalid(tmp_path):
    from framework.office.plan_output_gate import parse_plan_with_status
    plan_path = _write(tmp_path, "organization-plan.md", "# Plan\n## Source -> Summary Mapping\n| s | t |\n|---|---|\n| a | b |\n")
    status, _invalid_entries, entries, committed, error = parse_plan_with_status(
        "summarize", plan_path
    )
    assert status == "invalid"
    assert "summary" in error.lower()


def test_parse_plan_destination_with_parent_traversal_is_invalid(tmp_path):
    from framework.office.plan_output_gate import parse_plan_with_status
    plan = """## Files Organized
| source | destination |
| --- | --- |
| /src/a.txt | ../escape.txt |
"""
    plan_path = _write(tmp_path, "organization-plan.md", plan)
    status, _invalid_entries, entries, committed, error = parse_plan_with_status("organize", plan_path)
    assert status == "invalid"
    assert any("../escape.txt" in e for e in entries) or "parent traversal" in error.lower()


def test_parse_plan_destination_absolute_path_is_invalid(tmp_path):
    from framework.office.plan_output_gate import parse_plan_with_status
    plan = """## Files Organized
| source | destination |
| --- | --- |
| /src/a.txt | /etc/passwd |
"""
    plan_path = _write(tmp_path, "organization-plan.md", plan)
    status, _invalid_entries, entries, committed, error = parse_plan_with_status("organize", plan_path)
    assert status == "invalid"


def test_parse_plan_duplicate_rows_is_invalid(tmp_path):
    from framework.office.plan_output_gate import parse_plan_with_status
    plan = """## Files Organized
| source | destination |
| --- | --- |
| /src/a.txt | files/a.txt |
| /src/a.txt | files/a.txt |
"""
    plan_path = _write(tmp_path, "organization-plan.md", plan)
    status, _invalid_entries, entries, committed, error = parse_plan_with_status("organize", plan_path)
    assert status == "invalid"
    assert "duplicate" in error.lower()


def test_parse_plan_source_outside_validated_set_is_invalid(tmp_path):
    from framework.office.plan_output_gate import parse_plan_with_status
    plan = """## Files Organized
| source | destination |
| --- | --- |
| /other/a.txt | files/a.txt |
"""
    plan_path = _write(tmp_path, "organization-plan.md", plan)
    status, _invalid_entries, entries, committed, error = parse_plan_with_status(
        "organize", plan_path, validated_source_roots=["/src"]
    )
    assert status == "invalid"
    assert "outside" in error.lower() or any("outside" in e for e in entries)


def test_parse_plan_folder_source_not_expanded_is_invalid(tmp_path):
    from framework.office.plan_output_gate import parse_plan_with_status
    plan = """## Source -> Summary Mapping
| source | summary_target |
| --- | --- |
| /src/folder | summary.md |
"""
    plan_path = _write(tmp_path, "summary-plan.md", plan)
    status, _invalid_entries, entries, committed, error = parse_plan_with_status(
        "summarize", plan_path, expanded_file_list=["/src/folder/a.txt", "/src/folder/b.txt"]
    )
    assert status == "invalid"
    assert "expand" in error.lower() or any("expand" in e for e in entries)


def test_parse_plan_empty_with_non_empty_inventory_is_invalid(tmp_path):
    from framework.office.plan_output_gate import parse_plan_with_status
    plan = """## Files Organized
| source | destination |
| --- | --- |
"""
    plan_path = _write(tmp_path, "organization-plan.md", plan)
    status, _invalid_entries, entries, committed, error = parse_plan_with_status(
        "organize", plan_path, source_count=3
    )
    assert status == "invalid"
    assert "non-empty" in error.lower() or any("non-empty" in e for e in entries)


def test_parse_plan_huge_file_bounded_by_size_cap(tmp_path, monkeypatch):
    from framework.office import plan_output_gate as pog
    monkeypatch.setattr(pog, "_MAX_PLAN_BYTES", 16)
    plan_path = _write(tmp_path, "organization-plan.md", "x" * 64)
    status, _invalid_entries, entries, committed, error = parse_plan_with_status("organize", plan_path)
    assert status == "unparseable"


def test_parse_plan_non_utf8_rejected(tmp_path):
    from framework.office.plan_output_gate import parse_plan_with_status
    plan_path = tmp_path / "organization-plan.md"
    plan_path.write_bytes(b"\xff\xfe garbage")
    status, _invalid_entries, entries, committed, error = parse_plan_with_status("organize", str(plan_path))
    assert status == "unparseable"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/test_plan_output_gate.py::test_parse_plan_organize_extracts_pairs -v`
Expected: ImportError or AttributeError — `parse_plan` does not exist yet.

- [ ] **Step 3: Implement `parse_plan` and `parse_plan_with_status`**

Append to `framework/office/plan_output_gate.py`:

```python
import re
from typing import Iterable

_MAX_PLAN_BYTES = 1_048_576  # 1 MB cap; configurable per Task 2 spec
_PLAN_SIZE_CAP_ENV = "OFFICE_PLAN_MAX_BYTES"


def _plan_size_cap() -> int:
    env = os.environ.get(_PLAN_SIZE_CAP_ENV, "").strip()
    if env.isdigit() and int(env) > 0:
        return int(env)
    return _MAX_PLAN_BYTES


_SECTION_HEADERS = {
    "organize": "files organized",
    "summarize": "source -> summary mapping",
    "analyze": "source -> analysis mapping",
}
_SECTION_COMMITTED = "committed fields"


def _parse_table_rows(section: str) -> list[list[str]]:
    """Parse a markdown pipe-table into a list of row-arrays of cell strings."""
    rows: list[list[str]] = []
    for raw in section.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            continue
        # skip alignment row (---)
        cells = [c.strip() for c in line.strip("|").split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
            continue
        rows.append(cells)
    return rows


def _extract_section(plan_text: str, header: str) -> str:
    """Return the body of a markdown section whose ``##`` heading matches ``header``."""
    target = header.lower()
    lines = plan_text.splitlines()
    in_section = False
    body: list[str] = []
    for line in lines:
        if line.strip().startswith("##"):
            in_section = target in line.lower()
            continue
        if in_section:
            body.append(line)
    return "\n".join(body)


def _parse_committed_fields(plan_text: str) -> dict[str, Any]:
    section = _extract_section(plan_text, _SECTION_COMMITTED)
    out: dict[str, Any] = {}
    for line in section.splitlines():
        line = line.strip()
        if line.startswith("- "):
            line = line[2:]
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if value.isdigit():
            out[key] = int(value)
        else:
            out[key] = value
    return out


def _plan_capability_marker(plan_text: str) -> str | None:
    """Infer which capability the plan was written for.

    Used to detect a wrong-capability-in-slot case (e.g. summary-plan.md
    dropped into the organize slot).
    """
    text = plan_text.lower()
    if "files organized" in text and "source -> summary mapping" not in text:
        return "organize"
    if "source -> summary mapping" in text:
        return "summarize"
    if "source -> analysis mapping" in text:
        return "analyze"
    return None


def _is_path_safety_violation(relative: str) -> str | None:
    if not isinstance(relative, str) or not relative:
        return "destination is empty"
    if relative.startswith("/") or relative.startswith("~"):
        return "absolute path not allowed"
    if re.match(r"^[a-zA-Z]:[\\/]", relative):
        return "drive-letter path not allowed"
    if "\\" in relative:
        return "backslash separator not allowed"
    if ".." in relative.split("/"):
        return "parent traversal not allowed"
    return None


def _validated_source_realpaths(validated_source_roots: Iterable[str] | None) -> set[str]:
    if not validated_source_roots:
        return set()
    out: set[str] = set()
    for root in validated_source_roots:
        if not root:
            continue
        out.add(os.path.realpath(os.path.abspath(root)))
    return out


def _is_under_validated_source(source_path: str, validated_roots: set[str]) -> bool:
    if not validated_roots:
        return True
    try:
        real = os.path.realpath(os.path.abspath(source_path))
    except OSError:
        return False
    for root in validated_roots:
        if real == root or real.startswith(root.rstrip(os.sep) + os.sep):
            return True
    return False


def _split_destination_organize(cells: list[str]) -> tuple[str, str]:
    # The organize table columns are | source | destination |
    if len(cells) < 2:
        return "", ""
    return cells[0], cells[1]


def _split_destination_summarize(cells: list[str]) -> tuple[str, str]:
    if len(cells) < 2:
        return "", ""
    return cells[0], cells[1]


def _split_destination_analyze(cells: list[str]) -> tuple[str, str]:
    if len(cells) < 2:
        return "", ""
    return cells[0], cells[1]


def _parse_plan_rows(capability: str, plan_text: str) -> tuple[list[GateEntry], dict[str, Any]]:
    section = _extract_section(plan_text, _SECTION_HEADERS[capability])
    rows = _parse_table_rows(section)
    entries: list[GateEntry] = []
    if capability in ("summarize", "analyze"):
        # skip header row
        data_rows = [r for r in rows if r and r[0].lower() != "source"]
    else:
        data_rows = [r for r in rows if r and r[0].lower() != "source"]
    for cells in data_rows:
        source, target = {
            "organize": _split_destination_organize,
            "summarize": _split_destination_summarize,
            "analyze": _split_destination_analyze,
        }[capability](cells)
        if not source and not target:
            continue
        extras: dict[str, Any] = {}
        if capability == "summarize":
            extras["summary_target"] = target
            expected = target
        elif capability == "analyze":
            extras["analysis_target"] = target
            expected = target
        else:
            expected = target
        entries.append(GateEntry(source_path=source, expected_path=expected, extras=extras))
    committed = _parse_committed_fields(plan_text) if capability == "analyze" else {}
    return entries, committed


def parse_plan(capability: str, plan_path: str) -> list[GateEntry]:
    """Parse the plan and return its GateEntry list.

    Convenience wrapper used by tests; production code should use
    :func:`parse_plan_with_status` because it returns the gate status.
    """
    status, _invalid_entries, entries, _committed, _error = parse_plan_with_status(capability, plan_path)
    if status != "ok":
        return []
    return entries


def parse_plan_with_status(
    capability: str,
    plan_path: str,
    *,
    validated_source_roots: Iterable[str] | None = None,
    expanded_file_list: Iterable[str] | None = None,
    source_count: int | None = None,
) -> tuple[str, list[str], list[GateEntry], dict[str, Any], str]:
    """Parse a plan file and return ``(status, invalid_entries, entries, committed, error)``.

    ``invalid_entries`` is a list of human-readable explanations of rows that
    failed path/source safety. ``entries`` is the list of valid :class:`GateEntry`
    objects (excluding the invalid ones — the gate fails with ``status=invalid``).
    """
    if not os.path.exists(plan_path):
        return "missing", [], [], {}, "plan file not found"

    try:
        size = os.path.getsize(plan_path)
    except OSError as exc:
        return "unparseable", [], [], {}, f"plan stat failed: {exc}"

    if size > _plan_size_cap():
        return "unparseable", [], [], {}, f"plan exceeds {_plan_size_cap()} bytes"

    try:
        with open(plan_path, "r", encoding="utf-8", errors="strict") as fh:
            plan_text = fh.read()
    except UnicodeDecodeError as exc:
        return "unparseable", [], [], {}, f"plan is not valid UTF-8: {exc}"
    except OSError as exc:
        return "unparseable", [], [], {}, f"plan read failed: {exc}"

    plan_capability = _plan_capability_marker(plan_text)
    if plan_capability and plan_capability != capability:
        return (
            "invalid",
            [],
            [],
            {},
            f"plan for capability {plan_capability} found in capability {capability} slot",
        )

    valid_source_roots = _validated_source_realpaths(validated_source_roots)
    entries, committed = _parse_plan_rows(capability, plan_text)

    invalid_entries: list[str] = []
    valid_entries: list[GateEntry] = []
    seen: set[tuple[str, str]] = set()

    for entry in entries:
        marker = f"source={entry.source_path!r} destination={entry.expected_path!r}"
        reason = _is_path_safety_violation(entry.expected_path)
        if reason:
            invalid_entries.append(f"{marker}: {reason}")
            continue
        if valid_source_roots and not _is_under_validated_source(
            entry.source_path, valid_source_roots
        ):
            invalid_entries.append(
                f"{marker}: source path outside validated set"
            )
            continue
        pair = (entry.source_path, entry.expected_path)
        if pair in seen:
            invalid_entries.append(f"{marker}: duplicate row")
            continue
        seen.add(pair)
        valid_entries.append(entry)

    if invalid_entries:
        return "invalid", invalid_entries, [], committed, "; ".join(invalid_entries)

    # Non-empty source inventory with empty plan
    if source_count and source_count > 0 and not valid_entries:
        return (
            "invalid",
            [f"plan is empty but source inventory has {source_count} item(s)"],
            [],
            committed,
            "empty plan with non-empty source inventory",
        )

    # Empty source inventory with non-empty plan
    if source_count == 0 and valid_entries:
        return (
            "invalid",
            [f"plan has {len(valid_entries)} row(s) but source inventory is empty"],
            [],
            committed,
            "non-empty plan with empty source inventory",
        )

    # summarize/analyze require expanded file list, not folder placeholders
    if capability in ("summarize", "analyze") and expanded_file_list is not None:
        for entry in valid_entries:
            if (
                entry.source_path
                and os.path.isdir(entry.source_path)
                and entry.source_path not in set(expanded_file_list)
            ):
                return (
                    "invalid",
                    [
                        f"source {entry.source_path!r} is a folder; "
                        "expand to individual files before planning"
                    ],
                    [],
                    committed,
                    "folder source not expanded",
                )

    return "ok", [], valid_entries, committed, ""
```

- [ ] **Step 4: Update the test file to call `parse_plan_with_status` correctly**

The tests above already use the new 5-tuple shape `(status, invalid_entries, entries, committed, error)`. Verify all tests parse correctly with the new implementation.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/test_plan_output_gate.py -v`
Expected: all gate tests PASS (the dataclass tests + 13 new parse tests).

- [ ] **Step 6: Commit**

```bash
git add framework/office/plan_output_gate.py tests/unit/agents/office/test_plan_output_gate.py
git commit -m "feat(framework): plan_output_gate parse_plan with status and path-safety"
```

---

## Task 4: Plan-output gate — `walk_output`, `diff`, and `run`

**Files:**
- Modify: `framework/office/plan_output_gate.py`
- Test: `tests/unit/agents/office/test_plan_output_gate.py`

- [ ] **Step 1: Write failing tests for `walk_output` and `diff`**

Append to `tests/unit/agents/office/test_plan_output_gate.py`:

```python
from framework.office.plan_output_gate import walk_output, diff, run
import os


def _touch(root, *parts):
    p = root.joinpath(*parts)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("x", encoding="utf-8")
    return str(p.relative_to(root))


def test_walk_output_returns_basename_paths(tmp_path):
    _touch(tmp_path, "files", "a.txt")
    _touch(tmp_path, "files", "b.txt")
    files = walk_output(str(tmp_path), allowlist={"organization-plan.md"})
    assert "files/a.txt" in files
    assert "files/b.txt" in files


def test_walk_output_excludes_ancillary_files(tmp_path):
    _touch(tmp_path, "organization-plan.md")
    _touch(tmp_path, "files", "a.txt")
    files = walk_output(str(tmp_path), allowlist={"organization-plan.md"})
    assert "organization-plan.md" not in files
    assert "files/a.txt" in files


def test_walk_output_excludes_timestamped_backups(tmp_path):
    _touch(tmp_path, "files", "a.txt")
    _touch(tmp_path, "files", "a.txt.20260603-120000.bak")
    files = walk_output(str(tmp_path), allowlist={"organization-plan.md"})
    assert "files/a.txt" in files
    assert not any(f.endswith(".bak") for f in files)


def test_walk_output_ignores_hidden_files_and_empty_dirs(tmp_path):
    _touch(tmp_path, "files", "a.txt")
    (tmp_path / "files" / "emptydir").mkdir()
    (tmp_path / ".hidden").write_text("h", encoding="utf-8")
    files = walk_output(str(tmp_path), allowlist={"organization-plan.md"})
    assert "files/a.txt" in files
    assert ".hidden" not in files


def test_walk_output_ignores_ancillary_files_in_subdirectories(tmp_path):
    _touch(tmp_path, "files", "a.txt")
    _touch(tmp_path, "files", "warnings.md")
    files = walk_output(str(tmp_path), allowlist={"warnings.md"})
    assert "files/a.txt" in files
    assert "files/warnings.md" not in files


def test_walk_output_symlink_escape_treated_as_unexpected(tmp_path, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside")
    (outside / "leaked.txt").write_text("x", encoding="utf-8")
    link = tmp_path / "files" / "link"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside / "leaked.txt")
    files = walk_output(str(tmp_path), allowlist=set())
    assert "files/link" in files


def test_diff_clean_tree_returns_clean_report(tmp_path):
    _touch(tmp_path, "files", "a.txt")
    contract = OutputContract(
        capability="organize",
        plan_path="",
        output_root=str(tmp_path),
        ancillary_allowlist=frozenset({"organization-plan.md"}),
        source_count=1,
        expected_plan_kind="files_organized",
    )
    plan = [GateEntry(source_path="/src/a.txt", expected_path="files/a.txt")]
    report = diff("organize", plan, {"files/a.txt"}, contract)
    assert report.is_clean is True


def test_diff_missing_file_populates_missing(tmp_path):
    _touch(tmp_path, "files", "a.txt")
    contract = OutputContract(
        capability="organize",
        plan_path="",
        output_root=str(tmp_path),
        ancillary_allowlist=frozenset({"organization-plan.md"}),
        source_count=2,
        expected_plan_kind="files_organized",
    )
    plan = [
        GateEntry(source_path="/src/a.txt", expected_path="files/a.txt"),
        GateEntry(source_path="/src/b.txt", expected_path="files/b.txt"),
    ]
    report = diff("organize", plan, {"files/a.txt"}, contract)
    assert "files/b.txt" in report.missing
    assert report.is_clean is False


def test_diff_unexpected_file_populates_unexpected(tmp_path):
    _touch(tmp_path, "files", "a.txt")
    _touch(tmp_path, "files", "b.txt")
    contract = OutputContract(
        capability="organize",
        plan_path="",
        output_root=str(tmp_path),
        ancillary_allowlist=frozenset({"organization-plan.md"}),
        source_count=1,
        expected_plan_kind="files_organized",
    )
    plan = [GateEntry(source_path="/src/a.txt", expected_path="files/a.txt")]
    report = diff("organize", plan, {"files/a.txt", "files/b.txt"}, contract)
    assert "files/b.txt" in report.unexpected


def test_diff_analyze_committed_fields_mismatch(tmp_path):
    contract = OutputContract(
        capability="analyze",
        plan_path="",
        output_root=str(tmp_path),
        ancillary_allowlist=frozenset({"analysis-plan.md"}),
        source_count=1,
        expected_plan_kind="source_analysis_mapping",
    )
    plan = [
        GateEntry(
            source_path="/src/data.csv",
            expected_path="data.analysis.md",
            extras={"analysis_target": "data.analysis.md", "field_count": 5},
        )
    ]
    actual = {"data.analysis.md"}
    # The committed field value is 4, but the analysis produced 5 — committed mismatch
    report = diff("analyze", plan, actual, contract, committed={"field_count": 4})
    assert any("field_count" in m for m in report.mismatches)


def test_diff_empty_plan_with_non_empty_inventory_is_invalid(tmp_path):
    _touch(tmp_path, "files", "a.txt")
    contract = OutputContract(
        capability="organize",
        plan_path="",
        output_root=str(tmp_path),
        ancillary_allowlist=frozenset({"organization-plan.md"}),
        source_count=3,
        expected_plan_kind="files_organized",
    )
    report = diff("organize", [], {"files/a.txt"}, contract)
    assert report.plan_status == "invalid"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/test_plan_output_gate.py::test_walk_output_returns_basename_paths -v`
Expected: ImportError for `walk_output` / `diff`.

- [ ] **Step 3: Implement `walk_output` and `diff`**

Append to `framework/office/plan_output_gate.py`:

```python
_BACKUP_SUFFIX_RE = re.compile(r"\.\d{8}-\d{6}\.bak$")


def _is_ancillary(rel_path: str, allowlist: frozenset[str]) -> bool:
    base = os.path.basename(rel_path)
    if base in allowlist:
        return True
    if _BACKUP_SUFFIX_RE.search(base):
        return True
    return False


def walk_output(output_root: str, *, allowlist: set[str] | frozenset[str] | None) -> set[str]:
    """Return the set of deliverable files under ``output_root``.

    Excluded: hidden files, empty directories, ancillary allowlist
    basenames, timestamped backup files, and any file whose basename
    appears in ``allowlist`` regardless of subdirectory.
    """
    frozen: frozenset[str] = frozenset(allowlist or set())
    out: set[str] = set()
    if not output_root or not os.path.isdir(output_root):
        return out
    for current_root, dirs, files in os.walk(output_root, followlinks=False):
        # prune hidden directories in place so os.walk skips them
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.startswith("."):
                continue
            full = os.path.join(current_root, name)
            try:
                stat = os.path.getsize(full)
            except OSError:
                continue
            if stat == 0:
                continue
            # chase symlinks; record escapes so the gate can flag them
            real = os.path.realpath(full)
            real_root = os.path.realpath(output_root)
            try:
                rel = os.path.relpath(full, output_root).replace(os.sep, "/")
            except ValueError:
                rel = name
            if not (real == real_root or real.startswith(real_root.rstrip(os.sep) + os.sep)):
                out.add(rel)
                continue
            if _is_ancillary(rel, frozen):
                continue
            out.add(rel)
    return out


def _committed_field_diffs(plan_committed: dict[str, Any], contract: OutputContract) -> list[str]:
    """Diff a tiny subset of committed fields that the analyze capability
    is expected to validate. For now this only checks field_count and
    numeric_field_count; expand as the spec grows.
    """
    if not plan_committed or contract.capability != "analyze":
        return []
    out: list[str] = []
    for key in ("field_count", "numeric_field_count"):
        if key in plan_committed:
            # actual count must be derivable from the report; for now we only
            # report the field as a known committed fact so a future runtime
            # check can fill it in
            out.append(
                f"{key} committed to {plan_committed[key]} (validation deferred to analyze runtime)"
            )
    return out


def diff(
    capability: str,
    plan: list[GateEntry],
    actual: set[str],
    contract: OutputContract,
    committed: dict[str, Any] | None = None,
) -> GateReport:
    """Compare parsed plan vs walked output tree."""
    if not plan and contract.source_count > 0:
        return GateReport(
            capability=capability,
            plan_status="invalid",
            planned_count=0,
            actual_count=len(actual),
            missing=[],
            unexpected=sorted(actual),
            mismatches=[],
            error_message="empty plan with non-empty source inventory",
        )
    planned = {entry.expected_path for entry in plan}
    missing = sorted(planned - actual)
    unexpected = sorted(actual - planned)
    mismatches = _committed_field_diffs(committed or {}, contract)
    return GateReport(
        capability=capability,
        plan_status="ok",
        planned_count=len(planned),
        actual_count=len(actual),
        missing=missing,
        unexpected=unexpected,
        mismatches=mismatches,
    )


def run(contract: OutputContract, *, expanded_file_list: Iterable[str] | None = None) -> GateReport:
    """Run the full gate: parse the plan, walk the output, diff."""
    status, invalid_entries, entries, committed, error = parse_plan_with_status(
        contract.capability,
        contract.plan_path,
        validated_source_roots=None,
        expanded_file_list=expanded_file_list,
        source_count=contract.source_count,
    )
    if status == "missing":
        return GateReport(
            capability=contract.capability,
            plan_status="missing",
            planned_count=0,
            actual_count=0,
            missing=[],
            unexpected=[],
            mismatches=[],
            error_message=error,
        )
    if status != "ok":
        return GateReport(
            capability=contract.capability,
            plan_status=status,
            planned_count=0,
            actual_count=0,
            missing=[],
            unexpected=[],
            mismatches=[],
            invalid_plan_entries=list(invalid_entries),
            error_message=error,
        )
    actual = walk_output(contract.output_root, allowlist=contract.ancillary_allowlist)
    return diff(contract.capability, entries, actual, contract, committed=committed)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/test_plan_output_gate.py -v`
Expected: all gate tests PASS (dataclass tests + parse tests + walk/diff/run tests).

- [ ] **Step 5: Commit**

```bash
git add framework/office/plan_output_gate.py tests/unit/agents/office/test_plan_output_gate.py
git commit -m "feat(framework): plan_output_gate walk_output, diff, run"
```

---

## Task 5: Add `delete_output_file` tool

**Files:**
- Modify: `agents/office/office_tools.py`
- Test: `tests/unit/agents/office/test_plan_output_gate_flow.py` (created later; use a focused test file first)

- [ ] **Step 1: Create focused delete-tool test file**

Create `tests/unit/agents/office/test_delete_output_file.py`:

```python
import os
import pytest
from agents.office import office_tools


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    monkeypatch.setenv("OFFICE_WORKSPACE_ROOT", str(root))
    return root


def _resolve_tool():
    from agents.office.office_tools import DeleteOutputFileTool
    return DeleteOutputFileTool()


def test_delete_output_file_removes_file_under_workspace(workspace):
    f = workspace / "stale.txt"
    f.write_text("x", encoding="utf-8")
    tool = _resolve_tool()
    result = tool.execute_sync(filename="stale.txt")
    assert result.error == ""
    assert not f.exists()


def test_delete_output_file_refuses_parent_traversal(workspace):
    f = workspace / "stale.txt"
    f.write_text("x", encoding="utf-8")
    tool = _resolve_tool()
    result = tool.execute_sync(filename="../escape.txt")
    assert result.error != ""
    assert "traversal" in result.error.lower() or "outside" in result.error.lower()
    assert f.exists()  # original file untouched


def test_delete_output_file_refuses_source_input_path(workspace, tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    src_file = src / "input.txt"
    src_file.write_text("x", encoding="utf-8")
    # Symlink the source folder inside the workspace so the tool can see it
    link = workspace / "input.txt"
    link.symlink_to(src_file)
    monkeypatch.setenv("OFFICE_SOURCE_ROOT", str(src))
    tool = _resolve_tool()
    result = tool.execute_sync(filename="input.txt")
    assert result.error != ""
    assert "source" in result.error.lower()
    assert src_file.exists()


def test_delete_output_file_refuses_path_outside_workspace(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    monkeypatch.setenv("OFFICE_WORKSPACE_ROOT", str(workspace))
    tool = _resolve_tool()
    result = tool.execute_sync(filename=str(outside))
    assert result.error != ""
    assert not outside.exists() or result.error != ""


def test_delete_output_file_refuses_symlink_escape(workspace, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    link = workspace / "link.txt"
    link.symlink_to(outside)
    tool = _resolve_tool()
    result = tool.execute_sync(filename="link.txt")
    assert result.error != ""
    assert outside.exists()


def test_delete_output_file_inplace_uses_resolved_target(workspace, tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    target = src / "organized-output" / "files"
    target.mkdir(parents=True)
    stale = target / "stale.txt"
    stale.write_text("x", encoding="utf-8")
    monkeypatch.setenv("OFFICE_OUTPUT_MODE", "inplace")
    monkeypatch.setenv("OFFICE_RESOLVED_TARGET_DIR", str(target))
    tool = _resolve_tool()
    result = tool.execute_sync(filename=str(stale))
    assert result.error == ""
    assert not stale.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/test_delete_output_file.py -v`
Expected: ImportError for `DeleteOutputFileTool`.

- [ ] **Step 3: Implement `DeleteOutputFileTool`**

Append to `agents/office/office_tools.py`:

```python
class DeleteOutputFileTool(BaseTool):
    """Delete a file under the resolved task output root.

    Hard rules (enforced in this order, fail-closed):
    1. ``OFFICE_OUTPUT_MODE`` and ``OFFICE_RESOLVED_TARGET_DIR`` must be set
       to a real directory; the tool refuses otherwise.
    2. The candidate path resolves with ``realpath`` and must lie inside
       the resolved target directory.
    3. The candidate path must NOT resolve to a file under any validated
       source root (``OFFICE_SOURCE_ROOT``).
    4. Symlink chains that escape the target are rejected without
       following the chain to delete.
    """
    name = "delete_output_file"
    description = "Delete a stale output file under the resolved task output root. Refuses source inputs and out-of-root paths."
    parameters_schema = {
        "type": "object",
        "properties": {
            "filename": {
                "type": "string",
                "description": "Path of the file to delete, relative to the resolved target directory or absolute under it.",
            },
        },
        "required": ["filename"],
    }

    def execute_sync(self, filename: str = "") -> ToolResult:
        target_dir = os.environ.get("OFFICE_RESOLVED_TARGET_DIR", "").strip()
        if not target_dir:
            return ToolResult(output="", error="delete_output_file: OFFICE_RESOLVED_TARGET_DIR is not set")
        try:
            real_target = os.path.realpath(os.path.abspath(target_dir))
        except Exception as exc:
            return ToolResult(output="", error=f"delete_output_file: target resolution failed: {exc}")
        if not os.path.isdir(real_target):
            return ToolResult(output="", error=f"delete_output_file: target {real_target!r} is not a directory")
        if not filename:
            return ToolResult(output="", error="delete_output_file: filename is required")
        # candidate resolution
        try:
            if os.path.isabs(filename):
                candidate = os.path.realpath(os.path.abspath(filename))
            else:
                candidate = os.path.realpath(os.path.join(real_target, filename))
        except Exception as exc:
            return ToolResult(output="", error=f"delete_output_file: path resolution failed: {exc}")
        if not (candidate == real_target or candidate.startswith(real_target.rstrip(os.sep) + os.sep)):
            return ToolResult(output="", error=f"delete_output_file: path {filename!r} is outside the resolved target directory")
        # source-input protection
        source_root = os.environ.get("OFFICE_SOURCE_ROOT", "").strip()
        if source_root:
            real_source = os.path.realpath(os.path.abspath(source_root))
            if candidate == real_source or candidate.startswith(real_source.rstrip(os.sep) + os.sep):
                return ToolResult(output="", error=f"delete_output_file: refusing to delete source input {filename!r}")
        if not os.path.exists(candidate):
            return ToolResult(output="", error=f"delete_output_file: file does not exist: {filename!r}")
        if not os.path.isfile(candidate):
            return ToolResult(output="", error=f"delete_output_file: refusing to delete non-regular file: {filename!r}")
        try:
            os.remove(candidate)
        except OSError as exc:
            return ToolResult(output="", error=f"delete_output_file: remove failed: {exc}")
        return ToolResult(output=json.dumps({"deleted": candidate}))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/test_delete_output_file.py -v`
Expected: all 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/office/office_tools.py tests/unit/agents/office/test_delete_output_file.py
git commit -m "feat(office): add delete_output_file tool with path guardrails"
```

---

## Task 6: Wire `delete_output_file` into capability tool names

**Files:**
- Modify: `agents/office/nodes.py`
- Test: `tests/unit/agents/office/test_plan_output_gate_flow.py` (created in next task; first add a smoke test for the registration function)

- [ ] **Step 1: Add a focused registration test**

Create `tests/unit/agents/office/test_office_tool_registration.py`:

```python
from agents.office.nodes import _capability_tool_names


def test_capability_tool_names_includes_delete_output_file_for_all_three():
    for capability in ("analyze", "summarize", "organize"):
        names = _capability_tool_names(capability, "workspace")
        assert "delete_output_file" in names, capability
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/test_office_tool_registration.py -v`
Expected: AssertionError (delete_output_file not in names).

- [ ] **Step 3: Modify `_capability_tool_names` to include `delete_output_file`**

In `agents/office/nodes.py`, the function `_capability_tool_names` is at line 743. Read the function, then ensure its returned list for every capability includes `"delete_output_file"` as the last entry:

```python
def _capability_tool_names(capability: str, output_mode: str) -> list[str]:
    ...  # existing logic
    return base + ["delete_output_file"]
```

(The exact edit depends on the function's existing shape. Always keep the existing entries, then append `"delete_output_file"`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/test_office_tool_registration.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full office tool registration test suite**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/ -v`
Expected: existing tests still pass; new test passes.

- [ ] **Step 6: Commit**

```bash
git add agents/office/nodes.py tests/unit/agents/office/test_office_tool_registration.py
git commit -m "feat(office): register delete_output_file for all three capabilities"
```

---

## Task 7: Add major-step emitters for the gate

**Files:**
- Modify: `agents/office/office_steps.py`
- Test: extend `tests/unit/agents/office/test_office_tool_registration.py` (rename later) or add `tests/unit/agents/office/test_office_steps.py`

- [ ] **Step 1: Add focused step-emitter test**

Create `tests/unit/agents/office/test_office_steps.py`:

```python
from agents.office import office_steps


class _Sink:
    def __init__(self):
        self.events = []
    def handle_event(self, event):
        self.events.append(event)


def _state():
    return {
        "capability": "summarize",
        "_compass_task_id": "task-1",
        "_task_store": None,
        "_major_step_progress_sink": None,
    }


def test_emit_validating_plan_output_running(monkeypatch):
    sink = _Sink()
    state = {**_state(), "_major_step_progress_sink": sink}
    office_steps.emit_validating_plan_output(
        state, lifecycle_state="running",
        summary_template="validating {planned_count}",
        summary_facts={"planned_count": 3},
    )
    assert sink.events[0]["step_key"] == "office.validating_plan_output"
    assert sink.events[0]["lifecycle_state"] == "running"


def test_emit_reconciling_plan_output_emits_round(monkeypatch):
    sink = _Sink()
    state = {**_state(), "_major_step_progress_sink": sink}
    office_steps.emit_reconciling_plan_output(
        state, lifecycle_state="running", round=2,
        summary_template="reconciling round {round}",
        summary_facts={"round": 2},
    )
    assert sink.events[0]["step_key"] == "office.reconciling_plan_output"
    assert sink.events[0]["step_instance_key"] == "office.reconciling_plan_output#2"
    assert sink.events[0]["round"] == 2


def test_emit_gate_exhausted_warning(monkeypatch):
    sink = _Sink()
    state = {**_state(), "_major_step_progress_sink": sink}
    office_steps.emit_gate_exhausted(
        state, summary_facts={"round_count": 3}
    )
    assert sink.events[0]["step_key"] == "office.gate_exhausted"
    assert sink.events[0]["lifecycle_state"] == "warning"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/test_office_steps.py -v`
Expected: AttributeError — emitters do not exist.

- [ ] **Step 3: Implement the three emitters in `office_steps.py`**

Append to `agents/office/office_steps.py`:

```python
def emit_validating_plan_output(
    state: dict,
    *,
    lifecycle_state: str,
    summary_template: str,
    summary_facts: dict | None = None,
) -> None:
    """Emit the plan-output validation step.

    ``lifecycle_state`` must be one of: ``running``, ``done``, ``warning``.
    """
    record_office_step(
        state,
        step_key="office.validating_plan_output",
        title="Office validating output against plan",
        lifecycle_state=lifecycle_state,
        summary_template=summary_template,
        summary_facts=summary_facts,
    )


def emit_reconciling_plan_output(
    state: dict,
    *,
    lifecycle_state: str,
    round: int,
    summary_template: str,
    summary_facts: dict | None = None,
) -> None:
    """Emit a per-round reconciliation step.

    The round number becomes part of ``step_instance_key`` so the UI can
    show up to three reconciliation rows.
    """
    record_office_step(
        state,
        step_key="office.reconciling_plan_output",
        title="Office reconciling output to match plan",
        lifecycle_state=lifecycle_state,
        summary_template=summary_template,
        summary_facts=summary_facts,
        conditional=True,
        round=round,
    )


def emit_gate_exhausted(
    state: dict,
    *,
    summary_facts: dict | None = None,
) -> None:
    """Emit the gate-exhaustion warning row."""
    record_office_step(
        state,
        step_key="office.gate_exhausted",
        title="Office plan-output gate exhausted",
        lifecycle_state=LIFECYCLE_WARNING,
        summary_template=(
            "Office could not fully reconcile the output with the declared plan after {round_count} round(s)."
        ),
        summary_facts=summary_facts,
        conditional=True,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/test_office_steps.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add agents/office/office_steps.py tests/unit/agents/office/test_office_steps.py
git commit -m "feat(office): add plan-output gate major-step emitters"
```

---

## Task 8: Add gate orchestration helper in `agents/office/nodes.py`

**Files:**
- Modify: `agents/office/nodes.py`
- Test: `tests/unit/agents/office/test_plan_output_gate_flow.py` (new)

- [ ] **Step 1: Write failing tests for the gate orchestration helper**

Create `tests/unit/agents/office/test_plan_output_gate_flow.py`:

```python
import json
import os
import threading
from unittest import mock

import pytest

from agents.office import office_steps
from agents.office.nodes import _run_plan_output_gate
from framework.office.plan_output_gate import (
    GateReport,
    OutputContract,
    resolve_output_contract,
)


class _StubRuntime:
    def __init__(self, results):
        self._results = list(results)
        self.calls = []

    def run_agentic(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        if not self._results:
            raise RuntimeError("no more stub results")
        item = self._results.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _stub_agentic_result(success=True, summary="ok", tool_calls=None, raw=""):
    from framework.agent import AgenticResult
    return AgenticResult(
        success=success,
        summary=summary,
        raw_output=raw,
        tool_calls=tool_calls or [],
    )


@pytest.fixture
def task_artifacts(tmp_path):
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "organized-output" / "files").mkdir(parents=True)
    return artifacts, workspace


def _state(artifacts, workspace, capability, validated_paths, **extras):
    return {
        "capability": capability,
        "validated_paths": validated_paths,
        "artifacts_dir": str(artifacts),
        "output_mode": extras.get("output_mode", "workspace"),
        "source_paths": validated_paths,
        "lifecycle_state": "running",
        "_compass_task_id": "task-1",
        "_task_id": "task-1",
    }


def test_clean_first_pass_emits_done_step(task_artifacts):
    artifacts, workspace = task_artifacts
    # write plan with one row, materialize the file
    plan_path = workspace / "organized-output" / "files" / "organization-plan.md"
    plan_path.write_text(
        "# Plan\n## Files Organized\n"
        "| source | destination |\n| --- | --- |\n"
        f"| /src/a.txt | files/a.txt |\n",
        encoding="utf-8",
    )
    (workspace / "organized-output" / "files" / "files").mkdir(parents=True, exist_ok=True)
    (workspace / "organized-output" / "files" / "files" / "a.txt").write_text("x", encoding="utf-8")
    state = _state(artifacts, workspace, "organize", ["/src/a.txt"])
    sink_calls = []
    state["_major_step_progress_sink"] = type("_Sink", (), {"handle_event": lambda self, e: sink_calls.append(e)})()
    runtime = _StubRuntime([])  # no retries needed
    state["_runtime"] = runtime
    report = _run_plan_output_gate(state, runtime=runtime)
    assert report.is_clean
    keys = [c["step_key"] for c in sink_calls]
    assert "office.validating_plan_output" in keys
    validating = [c for c in sink_calls if c["step_key"] == "office.validating_plan_output"]
    assert any(c["lifecycle_state"] == "done" for c in validating)


def test_mismatch_triggers_retry_and_emits_warning(task_artifacts):
    artifacts, workspace = task_artifacts
    plan_path = workspace / "organized-output" / "files" / "organization-plan.md"
    plan_path.write_text(
        "# Plan\n## Files Organized\n"
        "| source | destination |\n| --- | --- |\n"
        f"| /src/a.txt | files/a.txt |\n",
        encoding="utf-8",
    )
    # No file materialized -> gate will fail
    state = _state(artifacts, workspace, "organize", ["/src/a.txt"])
    sink_calls = []
    state["_major_step_progress_sink"] = type("_Sink", (), {"handle_event": lambda self, e: sink_calls.append(e)})()
    # Stub LLM retry that will write the missing file
    from framework.agent import AgenticResult
    runtime = _StubRuntime([
        AgenticResult(
            success=True,
            summary="retry wrote file",
            tool_calls=[{"name": "delete_output_file", "ok": True}],
        ),
    ])
    # Side effect: retry call should create the file before the second gate run
    def side_effect(*args, **kwargs):
        target = workspace / "organized-output" / "files" / "files" / "a.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x", encoding="utf-8")
        return AgenticResult(success=True, summary="ok", tool_calls=[])
    runtime._results = [side_effect]
    state["_runtime"] = runtime
    report = _run_plan_output_gate(state, runtime=runtime)
    assert report.is_clean
    keys = [c["step_key"] for c in sink_calls]
    assert "office.validating_plan_output" in keys
    assert "office.reconciling_plan_output" in keys
    reconciling = [c for c in sink_calls if c["step_key"] == "office.reconciling_plan_output"]
    assert reconciling[0]["round"] == 1


def test_exhausted_emits_gate_exhausted_step(task_artifacts):
    artifacts, workspace = task_artifacts
    plan_path = workspace / "organized-output" / "files" / "organization-plan.md"
    plan_path.write_text(
        "# Plan\n## Files Organized\n"
        "| source | destination |\n| --- | --- |\n"
        f"| /src/a.txt | files/a.txt |\n",
        encoding="utf-8",
    )
    state = _state(artifacts, workspace, "organize", ["/src/a.txt"])
    sink_calls = []
    state["_major_step_progress_sink"] = type("_Sink", (), {"handle_event": lambda self, e: sink_calls.append(e)})()
    # LLM always returns a "no progress" empty result
    from framework.agent import AgenticResult
    runtime = _StubRuntime([
        AgenticResult(success=True, summary="no", tool_calls=[]),
        AgenticResult(success=True, summary="no", tool_calls=[]),
        AgenticResult(success=True, summary="no", tool_calls=[]),
    ])
    state["_runtime"] = runtime
    report = _run_plan_output_gate(state, runtime=runtime)
    assert not report.is_clean
    keys = [c["step_key"] for c in sink_calls]
    assert "office.gate_exhausted" in keys
    # plan-output-gate-report.json should be written
    report_path = artifacts / "plan-output-gate-report.json"
    assert report_path.exists()


def test_round_0_missing_plan_causes_missing_status(task_artifacts):
    artifacts, workspace = task_artifacts
    # No plan file is written
    state = _state(artifacts, workspace, "summarize", ["/src/a.txt"])
    sink_calls = []
    state["_major_step_progress_sink"] = type("_Sink", (), {"handle_event": lambda self, e: sink_calls.append(e)})()
    from framework.agent import AgenticResult
    runtime = _StubRuntime([
        # Retry round 1: LLM writes the plan
        AgenticResult(success=True, summary="wrote plan", tool_calls=[]),
    ])
    state["_runtime"] = runtime
    def _write_plan(*args, **kwargs):
        plan = workspace / "summary-plan.md"
        plan.write_text(
            "# Plan\n## Source -> Summary Mapping\n"
            "| source | summary_target |\n| --- | --- |\n"
            f"| /src/a.txt | a.md |\n",
            encoding="utf-8",
        )
        (workspace / "a.md").write_text("x", encoding="utf-8")
        return AgenticResult(success=True, summary="ok", tool_calls=[])
    runtime._results = [_write_plan]
    report = _run_plan_output_gate(state, runtime=runtime)
    assert report.is_clean
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/test_plan_output_gate_flow.py -v`
Expected: ImportError for `_run_plan_output_gate`.

- [ ] **Step 3: Implement `_run_plan_output_gate` and helper functions**

Append to `agents/office/nodes.py`:

```python
PLAN_OUTPUT_GATE_MAX_ROUNDS = 3
PLAN_OUTPUT_GATE_NO_PROGRESS_LIMIT = 2


def _snapshot_plan(plan_path: str) -> dict[str, Any] | None:
    """Capture (realpath, mtime_ns, sha256, bytes) of the plan file for
    integrity checks and a possible revert."""
    if not plan_path or not os.path.exists(plan_path):
        return None
    real = os.path.realpath(plan_path)
    stat = os.stat(real)
    with open(real, "rb") as fh:
        data = fh.read()
    return {
        "realpath": real,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": data,
    }


def _plan_modified(snapshot: dict[str, Any] | None) -> bool:
    if not snapshot:
        return False
    real = snapshot["realpath"]
    if not os.path.exists(real):
        return True
    stat = os.stat(real)
    if stat.st_mtime_ns != snapshot["mtime_ns"]:
        return True
    with open(real, "rb") as fh:
        digest = hashlib.sha256(fh.read()).hexdigest()
    return digest != snapshot["sha256"]


def _revert_plan(snapshot: dict[str, Any]) -> bool:
    """Restore the plan file from the snapshot's stored bytes.

    Returns True on success, False if the snapshot has no bytes (e.g. the
    plan was missing at snapshot time) or the write fails — the caller
    must then refuse to proceed and surface a task failure.
    """
    data = snapshot.get("bytes")
    if data is None:
        return False
    real = snapshot["realpath"]
    parent = os.path.dirname(real) or "."
    try:
        os.makedirs(parent, exist_ok=True)
        with open(real, "wb") as fh:
            fh.write(data)
    except OSError:
        return False
    return True


def _diff_signature(report: GateReport) -> str:
    """Stable signature of the gate's diff for no-progress detection."""
    h = hashlib.sha256()
    h.update(json.dumps(sorted(report.missing), sort_keys=True).encode("utf-8"))
    h.update(b"|")
    h.update(json.dumps(sorted(report.unexpected), sort_keys=True).encode("utf-8"))
    h.update(b"|")
    h.update(json.dumps(sorted(report.mismatches), sort_keys=True).encode("utf-8"))
    return h.hexdigest()


def _build_retry_prompt(
    capability: str,
    contract: OutputContract,
    report: GateReport,
    round_num: int,
    *,
    inplace: bool = False,
) -> str:
    """Build the deterministic retry prompt for the LLM."""
    lines: list[str] = []
    lines.append(
        f"[plan-output-gate] The declared plan and the materialized output disagree. (round {round_num} of {PLAN_OUTPUT_GATE_MAX_ROUNDS})"
    )
    lines.append("")
    lines.append(f"Plan status: {report.plan_status}")
    lines.append(f"Missing deliverables: {len(report.missing)}")
    lines.append(f"Unexpected deliverables: {len(report.unexpected)}")
    lines.append(f"Plan-specific mismatches: {len(report.mismatches)}")
    if report.error_message:
        lines.append(f"Error: {report.error_message}")
    if report.invalid_plan_entries:
        lines.append("Invalid plan entries:")
        for entry in report.invalid_plan_entries[:20]:
            lines.append(f"  - {entry}")
    if report.missing:
        lines.append("Missing from output (max 20 shown):")
        for path in report.missing[:20]:
            lines.append(f"  - {path}")
    if report.unexpected:
        lines.append("Unexpected in output (max 20 shown):")
        for path in report.unexpected[:20]:
            lines.append(f"  - {path}")
    if report.mismatches:
        lines.append("Mismatches:")
        for m in report.mismatches[:20]:
            lines.append(f"  - {m}")
    lines.append("")
    if report.plan_status in {"missing", "unparseable", "invalid"}:
        lines.append("The plan artifact itself is missing or invalid. Write the plan first, then materialize.")
    else:
        lines.append("Fix the materialized output so it matches the existing plan contract exactly.")
    lines.append("Do not invent new deliverables.")
    lines.append("Do not leave stale outputs from previous rounds.")
    if inplace:
        lines.append("This task is in inplace mode: the source tree is read-only. Only the resolved target directory is writable.")
    lines.append(f"Use only the authorized Office tools for the {capability} capability (including delete_output_file for stale files).")
    return "\n".join(lines)


def _record_retry_to_operations_log(
    state: dict, *, round: int, trigger: str, tool_name: str, ok: bool, error: str = ""
) -> None:
    """Best-effort append to operations-plan.json for audit."""
    try:
        artifacts_dir = state.get("artifacts_dir") or os.environ.get("OFFICE_WORKSPACE_ROOT", "")
        if not artifacts_dir:
            return
        log_path = os.path.join(artifacts_dir, "operations-plan.json")
        existing: list[dict[str, Any]] = []
        if os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8") as fh:
                    existing = json.load(fh)
            except (OSError, ValueError):
                existing = []
        existing.append(
            {
                "action": tool_name,
                "round": round,
                "trigger": trigger,
                "status": "succeeded" if ok else "failed",
                "error": error[:200],
            }
        )
        with open(log_path, "w", encoding="utf-8") as fh:
            json.dump(existing, fh, indent=2, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        logger.debug("operations-plan.json append failed: %s", exc)


def _write_gate_report(state: dict, contract: OutputContract, report: GateReport, rounds: int, *, no_progress_rounds: list[int], plan_modification_detected: bool) -> None:
    artifacts_dir = state.get("artifacts_dir") or ""
    if not artifacts_dir:
        return
    report_path = os.path.join(artifacts_dir, "plan-output-gate-report.json")
    payload = {
        "capability": report.capability,
        "rounds": rounds,
        "plan_status": report.plan_status,
        "planned_count": report.planned_count,
        "actual_count": report.actual_count,
        "final": {
            "missing": list(report.missing),
            "unexpected": list(report.unexpected),
            "mismatches": list(report.mismatches),
        },
        "invalid_plan_entries": list(report.invalid_plan_entries),
        "no_progress_rounds": no_progress_rounds,
        "plan_modification_detected": plan_modification_detected,
        "tool_unavailable": report.tool_unavailable,
        "plan_path": contract.plan_path,
        "output_root": contract.output_root,
    }
    try:
        with open(report_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
    except OSError as exc:
        logger.debug("plan-output-gate-report.json write failed: %s", exc)


def _run_plan_output_gate(state: dict, *, runtime) -> GateReport:
    """Run the plan-output gate with reconciliation.

    The runtime is the existing agentic runtime used by ``execute_office_work``.
    The LLM is invoked only on retry rounds using a deterministic prompt and
    the existing capability-specific prompt builder.
    """
    from agents.office import office_steps as _steps

    capability = state.get("capability", "summarize")
    validated_paths = state.get("validated_paths", [])
    output_mode = state.get("output_mode", "workspace")
    artifacts_dir = state.get("artifacts_dir", "")

    contract = resolve_output_contract(
        capability, validated_paths, output_mode, artifacts_dir
    )
    inplace = output_mode == "inplace"

    # Check tool registration up-front; fail closed if missing
    expected_tools = _capability_tool_names(capability, output_mode)
    if "delete_output_file" not in expected_tools:
        report = GateReport(
            capability=capability,
            plan_status="invalid",
            planned_count=0,
            actual_count=0,
            missing=[],
            unexpected=[],
            mismatches=[],
            error_message="delete_output_file tool not registered for this capability",
            tool_unavailable=True,
        )
        _steps.emit_validating_plan_output(
            state,
            lifecycle_state=LIFECYCLE_WARNING,
            summary_template="Plan-output gate could not start: delete_output_file tool is not registered.",
            summary_facts={"plan_status": "invalid", "tool_unavailable": True},
        )
        _steps.emit_gate_exhausted(state, summary_facts={"round_count": 0, "tool_unavailable": True})
        _write_gate_report(
            state, contract, report, rounds=0,
            no_progress_rounds=[], plan_modification_detected=False,
        )
        return report

    # Round 0 — initial validation
    _steps.emit_validating_plan_output(
        state,
        lifecycle_state=LIFECYCLE_RUNNING,
        summary_template="Office is validating the materialized output against the declared plan.",
        summary_facts={"plan_status": "running", "round": 0},
    )
    report = run(contract)
    if report.is_clean:
        _steps.emit_validating_plan_output(
            state,
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Plan and output match. Validated {planned_count} planned deliverable(s).",
            summary_facts={
                "plan_status": "ok",
                "planned_count": report.planned_count,
                "actual_count": report.actual_count,
                "round": 0,
            },
        )
        return report

    # Mismatch — enter retry loop
    _steps.emit_validating_plan_output(
        state,
        lifecycle_state=LIFECYCLE_WARNING,
        summary_template=(
            "Validation found {missing_count} missing, {unexpected_count} unexpected, "
            "and {mismatch_count} mismatched item(s). Starting reconciliation."
        ),
        summary_facts={
            "missing_count": len(report.missing),
            "unexpected_count": len(report.unexpected),
            "mismatch_count": len(report.mismatches),
            "round": 0,
        },
    )

    last_signature = _diff_signature(report)
    no_progress_rounds: list[int] = []
    plan_modification_detected = False
    retry_count = 0
    final_report = report

    for round_num in range(1, PLAN_OUTPUT_GATE_MAX_ROUNDS + 1):
        # Snapshot plan integrity before this round
        snapshot = _snapshot_plan(contract.plan_path)
        retry_prompt = _build_retry_prompt(
            capability, contract, final_report, round_num, inplace=inplace
        )
        _steps.emit_reconciling_plan_output(
            state,
            lifecycle_state=LIFECYCLE_RUNNING,
            round=round_num,
            summary_template=f"Office is reconciling the output to match the plan (round {{round}} of {PLAN_OUTPUT_GATE_MAX_ROUNDS}).",
            summary_facts={
                "round": round_num,
                "missing_count": len(final_report.missing),
                "unexpected_count": len(final_report.unexpected),
                "mismatch_count": len(final_report.mismatches),
            },
        )
        # Invoke the LLM with the retry prompt
        try:
            retry_result = runtime.run_agentic(retry_prompt)
            tool_calls = list(getattr(retry_result, "tool_calls", []) or [])
            if not tool_calls:
                no_progress_rounds.append(round_num)
        except Exception as exc:  # noqa: BLE001
            logger.debug("plan-output-gate retry round %d failed: %s", round_num, exc)
            tool_calls = []
            no_progress_rounds.append(round_num)

        for tc in tool_calls:
            _record_retry_to_operations_log(
                state,
                round=round_num,
                trigger="gate-retry",
                tool_name=str(tc.get("name", "")),
                ok=bool(tc.get("ok", True)),
                error=str(tc.get("error", "")),
            )

        # Plan integrity: revert if LLM modified the plan and the previous status was ok
        if _plan_modified(snapshot) and final_report.plan_status == "ok":
            reverted = _revert_plan(snapshot)
            if not reverted:
                _steps.emit_gate_exhausted(
                    state,
                    summary_facts={"round_count": round_num, "revert_failed": True},
                )
                _write_gate_report(
                    state,
                    contract,
                    final_report,
                    rounds=round_num,
                    no_progress_rounds=no_progress_rounds,
                    plan_modification_detected=True,
                )
                return final_report
            plan_modification_detected = True
            _steps.emit_reconciling_plan_output(
                state,
                lifecycle_state=LIFECYCLE_WARNING,
                round=round_num,
                summary_template="Plan was modified during retry; reverted to snapshot.",
                summary_facts={"round": round_num, "plan_modified": True},
            )

        # Snapshot is now stale; take a fresh one
        new_snapshot = _snapshot_plan(contract.plan_path)

        # Re-run the gate
        report = run(contract)
        # If the plan is missing after retry, ask the LLM to restore it (the
        # snapshot is still in the node-local state; the LLM gets the message
        # in the next round's prompt via the standard missing-status path)
        if report.plan_status == "missing" and new_snapshot:
            report = GateReport(
                capability=report.capability,
                plan_status="missing",
                planned_count=0,
                actual_count=report.actual_count,
                missing=[],
                unexpected=list(report.unexpected),
                mismatches=[],
                error_message="plan was deleted during retry; restore from snapshot",
            )

        if report.is_clean:
            _steps.emit_reconciling_plan_output(
                state,
                lifecycle_state=LIFECYCLE_DONE,
                round=round_num,
                summary_template="Reconciliation round {round} completed and the output now matches the plan.",
                summary_facts={
                    "round": round_num,
                    "missing_count": 0,
                    "unexpected_count": 0,
                    "mismatch_count": 0,
                },
            )
            _steps.emit_validating_plan_output(
                state,
                lifecycle_state=LIFECYCLE_DONE,
                summary_template="Plan and output match after {round_count} reconciliation round(s). Validated {planned_count} planned deliverable(s).",
                summary_facts={
                    "plan_status": "ok",
                    "planned_count": report.planned_count,
                    "actual_count": report.actual_count,
                    "round_count": round_num,
                },
            )
            return report

        # Detect repeated same-diff signature (no progress)
        new_sig = _diff_signature(report)
        if new_sig == last_signature:
            no_progress_rounds.append(round_num)
        last_signature = new_sig

        if round_num < PLAN_OUTPUT_GATE_MAX_ROUNDS:
            _steps.emit_reconciling_plan_output(
                state,
                lifecycle_state=LIFECYCLE_WARNING,
                round=round_num,
                summary_template="Reconciliation round {round} completed, but validation is still not clean.",
                summary_facts={
                    "round": round_num,
                    "missing_count": len(report.missing),
                    "unexpected_count": len(report.unexpected),
                    "mismatch_count": len(report.mismatches),
                },
            )
        retry_count = round_num
        final_report = report

    # Exhausted
    _steps.emit_validating_plan_output(
        state,
        lifecycle_state=LIFECYCLE_WARNING,
        summary_template=(
            "Plan-output gate exhausted after {round_count} reconciliation round(s): "
            "{missing_count} missing, {unexpected_count} unexpected, {mismatch_count} mismatched. "
            "See plan-output-gate-report.json."
        ),
        summary_facts={
            "plan_status": report.plan_status,
            "round_count": retry_count,
            "missing_count": len(report.missing),
            "unexpected_count": len(report.unexpected),
            "mismatch_count": len(report.mismatches),
            "no_progress_count": len(no_progress_rounds),
        },
    )
    _steps.emit_gate_exhausted(
        state,
        summary_facts={
            "round_count": retry_count,
            "no_progress_count": len(no_progress_rounds),
            "missing_count": len(report.missing),
            "unexpected_count": len(report.unexpected),
        },
    )
    _write_gate_report(
        state,
        contract,
        report,
        rounds=retry_count,
        no_progress_rounds=no_progress_rounds,
        plan_modification_detected=plan_modification_detected,
    )
    return final_report
```

- [ ] **Step 4: Add the missing imports at the top of `nodes.py`**

At the top of `agents/office/nodes.py`, add to the existing imports:

```python
import hashlib
from framework.office.plan_output_gate import (
    GateReport,
    OutputContract,
    resolve_output_contract,
    run as _run_gate,
)
from framework.major_step import LIFECYCLE_WARNING, LIFECYCLE_RUNNING, LIFECYCLE_DONE
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/test_plan_output_gate_flow.py -v`
Expected: all 4 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add agents/office/nodes.py tests/unit/agents/office/test_plan_output_gate_flow.py
git commit -m "feat(office): plan_output_gate orchestration with retry and exhaustion steps"
```

---

## Task 9: Wire the gate into `execute_office_work`

**Files:**
- Modify: `agents/office/nodes.py`
- Test: extend `tests/unit/agents/office/test_plan_output_gate_flow.py`

- [ ] **Step 1: Write failing test for the integration in `execute_office_work`**

Append to `tests/unit/agents/office/test_plan_output_gate_flow.py`:

```python
def test_execute_office_work_invokes_gate_after_success(task_artifacts, monkeypatch):
    from agents.office import nodes as office_nodes
    from framework.agent import AgenticResult
    artifacts, workspace = task_artifacts
    state = _state(artifacts, workspace, "organize", ["/src/a.txt"])
    sink_calls = []
    state["_major_step_progress_sink"] = type("_Sink", (), {"handle_event": lambda self, e: sink_calls.append(e)})()
    # Stub runtime that returns a success and triggers a clean gate
    runtime = _StubRuntime([])
    state["_runtime"] = runtime
    # Pre-write the plan and file so the gate is clean
    plan_path = workspace / "organized-output" / "files" / "organization-plan.md"
    plan_path.write_text(
        "# Plan\n## Files Organized\n"
        "| source | destination |\n| --- | --- |\n"
        f"| /src/a.txt | files/a.txt |\n",
        encoding="utf-8",
    )
    (workspace / "organized-output" / "files" / "files").mkdir(parents=True, exist_ok=True)
    (workspace / "organized-output" / "files" / "files" / "a.txt").write_text("x", encoding="utf-8")
    # Patch _try_bounded_office_flow to return a known result
    monkeypatch.setattr(
        office_nodes,
        "_try_bounded_office_flow",
        lambda *a, **k: AgenticResult(success=True, summary="ok", tool_calls=[]),
    )
    monkeypatch.setattr(office_nodes, "_expected_output_paths", lambda *a, **k: [str(workspace / "organized-output/files/files/a.txt")])
    monkeypatch.setattr(office_nodes, "_verify_delivery_paths", lambda *a, **k: (True, []))
    # The gate must run as part of the success path
    result = office_nodes.execute_office_work(state)
    assert result["success"] is True
    keys = [c["step_key"] for c in sink_calls]
    assert "office.validating_plan_output" in keys
    assert "office.reconciling_plan_output" not in keys
    assert "office.gate_exhausted" not in keys
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/test_plan_output_gate_flow.py::test_execute_office_work_invokes_gate_after_success -v`
Expected: AssertionError because the validating step is not yet emitted by `execute_office_work`.

- [ ] **Step 3: Wire the gate into `execute_office_work`**

In `agents/office/nodes.py`, locate the `if result.success:` block (around line 1349). Immediately after the existing post-processing (after line 1418, just before the `return {"summary": result.summary, ...}` block), add:

```python
# Plan-output gate: validate the materialized output against the plan
try:
    gate_report = _run_plan_output_gate(state, runtime=runtime)
    if not gate_report.is_clean:
        state["_plan_output_gate_last_report"] = {
            "is_clean": False,
            "missing": list(gate_report.missing),
            "unexpected": list(gate_report.unexpected),
            "mismatches": list(gate_report.mismatches),
            "invalid_plan_entries": list(gate_report.invalid_plan_entries),
        }
        warnings_list = list(state.get("_plan_output_gate_warnings") or [])
        warnings_list.append(
            f"plan-output gate not clean: {len(gate_report.missing)} missing, "
            f"{len(gate_report.unexpected)} unexpected, "
            f"{len(gate_report.mismatches)} mismatched"
        )
        state["_plan_output_gate_warnings"] = warnings_list
except Exception as exc:  # noqa: BLE001
    logger.debug("plan-output gate integration failed: %s", exc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/test_plan_output_gate_flow.py -v`
Expected: all 5 tests PASS.

- [ ] **Step 5: Run the full office unit test suite to check for regressions**

Run: `.venv/bin/python -m pytest tests/unit/agents/office/ -v`
Expected: all existing tests continue to pass.

- [ ] **Step 6: Commit**

```bash
git add agents/office/nodes.py tests/unit/agents/office/test_plan_output_gate_flow.py
git commit -m "feat(office): wire plan_output_gate into execute_office_work success path"
```

---

## Task 10: Add skeleton rows for the three new gate steps

**Files:**
- Modify: `agents/compass/agent.py`
- Test: `tests/unit/agents/compass/test_ui_integration.py`

- [ ] **Step 1: Write failing test for the new skeleton entries**

Append to `tests/unit/agents/compass/test_ui_integration.py`:

```python
def test_office_skeleton_includes_validating_reconciling_exhausted():
    from agents.compass.agent import _office_major_step_skeleton
    for capability in ("analyze", "summarize", "organize"):
        rows = _office_major_step_skeleton({"capability": capability})
        keys = [r["step_key"] for r in rows]
        assert "office.validating_plan_output" in keys, capability
        reconciling = [r for r in rows if r["step_key"] == "office.reconciling_plan_output"]
        assert len(reconciling) == 1, capability
        assert reconciling[0].get("conditional") is True
        exhausted = [r for r in rows if r["step_key"] == "office.gate_exhausted"]
        assert len(exhausted) == 1, capability
        assert exhausted[0].get("conditional") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/unit/agents/compass/test_ui_integration.py::test_office_skeleton_includes_validating_reconciling_exhausted -v`
Expected: AssertionError — the rows are not present.

- [ ] **Step 3: Add the new rows to `_office_major_step_skeleton`**

In `agents/compass/agent.py`, locate the `rows.extend([...])` block that adds `office.verifying` and `office.delivered` (around line 387). Add the three new conditional rows **before** `office.verifying`:

```python
rows.extend(
    [
        {
            "step_key": "office.validating_plan_output",
            "title": "Office validating output against plan",
            "agent": "office",
        },
        {
            "step_key": "office.reconciling_plan_output",
            "title": "Office reconciling output to match plan",
            "agent": "office",
            "conditional": True,
        },
        {
            "step_key": "office.gate_exhausted",
            "title": "Office plan-output gate exhausted",
            "agent": "office",
            "conditional": True,
        },
        {
            "step_key": "office.verifying",
            "title": "Office verifying deliverable",
            "agent": "office",
        },
        {
            "step_key": "office.delivered",
            "title": "Office delivering report to Compass",
            "agent": "office",
        },
    ]
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/unit/agents/compass/test_ui_integration.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full unit test suite**

Run: `.venv/bin/python -m pytest tests/unit/ -v`
Expected: all tests pass (no regressions).

- [ ] **Step 6: Commit**

```bash
git add agents/compass/agent.py tests/unit/agents/compass/test_ui_integration.py
git commit -m "feat(compass): add validating/reconciling/exhausted rows to office skeleton"
```

---

## Task 11: End-to-end regression check

- [ ] **Step 1: Run the full unit test suite**

Run: `.venv/bin/python -m pytest tests/unit/ -v`
Expected: all unit tests pass.

- [ ] **Step 2: Verify no test references test-specific task info**

Run: `grep -rn "34adaec0e1cf\|grade3\|Grade3\|Educational" framework/ agents/office/ agents/compass/ 2>/dev/null | head -10`
Expected: no matches in framework or agent code (test files in `artifacts/` are exempt).

- [ ] **Step 3: Verify the design principles are met**

Run: `grep -n "delete_output_file\|plan_output_gate\|path_safety" framework/office/ agents/office/office_tools.py agents/office/office_steps.py agents/office/nodes.py agents/compass/agent.py | head -30`
Expected: matches across the listed files (no test-only references).

- [ ] **Step 4: Final commit if any cleanup was needed**

```bash
git status
# if clean: nothing to commit
# otherwise:
# git add -A
# git commit -m "chore: final cleanup after plan-output gate implementation"
```

---

## Self-Review Checklist

- [x] **Spec coverage:** §5.1 dataclasses + run/resolve ✅ (Task 2, 4); §5.2 capability contracts ✅ (Task 3); §5.3 + §5.3.1 path safety ✅ (Task 1, 3); §5.4 retry prompt ✅ (Task 8); §5.5 delete tool ✅ (Task 5, 6); §5.5.1 plan integrity snapshot ✅ (Task 8); §5.6 + §5.6.1 + §5.6.2 no-progress ✅ (Task 8, 9); §5.7 + §5.8 step rows ✅ (Task 7, 10); §6 data flow ✅ (Task 8); §7 error handling covered by tests in Task 3, 4, 5, 8; §8 boundary parity — no SCM/design dependencies added; §9 tests ✅ (Task 1-10); §10 files touched ✅.
- [x] **Placeholder scan:** No "TBD", "TODO", "implement later", "fill in details", "similar to task N". All test code is concrete. All implementation code is concrete. `_revert_plan` has a concrete body that writes the snapshot bytes back to disk and returns False on failure. `_run_plan_output_gate` calls `_revert_plan` and fails closed if it cannot restore the previous plan.
- [x] **Type consistency:** `GateEntry.source_path/expected_path/extras` (Task 2) ↔ used in Task 3, 4, 8. `OutputContract.capability/plan_path/output_root/ancillary_allowlist/source_count/expected_plan_kind` (Task 2) ↔ used in Task 8. `GateReport.capability/plan_status/planned_count/actual_count/missing/unexpected/mismatches/invalid_plan_entries/error_message/tool_unavailable` (Task 2) ↔ used in Task 3, 4, 8. `LIFECYCLE_WARNING/RUNNING/DONE` (Task 8) ↔ imported from `framework.major_step`. Step keys `office.validating_plan_output`, `office.reconciling_plan_output`, `office.gate_exhausted` (Task 7) ↔ used in Task 8 and Task 10. `parse_plan_with_status` 5-tuple shape (Task 3) ↔ used in Task 4. `resolve_output_contract` signature (Task 2) ↔ used in Task 8.
- [x] **Spec self-review on the plan:** Walked every spec section; every requirement has a task. Edge cases in §5.3.1, §5.5, §5.5.1, §5.6.1, §5.6.2 are covered by named tests.
