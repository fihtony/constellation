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
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from framework.office.path_safety import is_within_root, validate_relative_path_syntax


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
            and not self.tool_unavailable
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
        if not workspace_root:
            raise ValueError(
                "workspace mode requires artifacts_dir or OFFICE_WORKSPACE_ROOT"
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


# ---------------------------------------------------------------------------
# Plan parser
# ---------------------------------------------------------------------------

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
    """Return the body of a markdown section whose ``##`` heading matches ``header``.

    The heading must be an exact, case-insensitive match for ``header`` after
    stripping the leading ``##`` and surrounding whitespace — substring
    matching was too loose (e.g. a paragraph mentioning "files were
    organized" would previously match "files organized").
    """
    target = header.lower()
    lines = plan_text.splitlines()
    in_section = False
    body: list[str] = []
    for line in lines:
        if line.strip().startswith("##"):
            stripped = line.strip().lstrip("#").strip().lower()
            in_section = target == stripped
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
    """Infer which capability the plan's content was written for.

    Only the first ``##``-prefixed heading of the document is considered;
    later prose that happens to mention a section header is ignored. The
    heading is matched exactly (case-insensitive) against the
    capability-specific section header in :data:`_SECTION_HEADERS`.
    """
    for line in plan_text.splitlines():
        if not line.strip().startswith("##"):
            continue
        stripped = line.strip().lstrip("#").strip().lower()
        for capability, header in _SECTION_HEADERS.items():
            if stripped == header:
                return capability
        return None
    return None


def _plan_slot_capability(plan_path: str) -> str | None:
    """Infer which capability slot the plan file lives in, by filename."""
    basename = os.path.basename(plan_path)
    if basename == "organization-plan.md":
        return "organize"
    if basename == "summary-plan.md":
        return "summarize"
    if basename == "analysis-plan.md":
        return "analyze"
    return None


def _is_path_safety_violation(relative: str) -> str | None:
    return validate_relative_path_syntax(relative)


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


def _split_first_two_cells(cells: list[str]) -> tuple[str, str]:
    return (cells[0], cells[1]) if len(cells) >= 2 else ("", "")


def _parse_plan_rows(capability: str, plan_text: str) -> tuple[list[GateEntry], dict[str, Any]]:
    section = _extract_section(plan_text, _SECTION_HEADERS[capability])
    rows = _parse_table_rows(section)
    entries: list[GateEntry] = []
    data_rows = [r for r in rows if r and r[0].lower() != "source"]
    for cells in data_rows:
        source, target = _split_first_two_cells(cells)
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
    status, _invalid_entries, entries, _committed, _error = parse_plan_with_status(
        capability, plan_path
    )
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
    slot_capability = _plan_slot_capability(plan_path)

    # Wrong slot: filename implies one capability, content is for another.
    # This catches the case where (e.g.) summary content lands in
    # organization-plan.md.
    if (
        plan_capability
        and slot_capability
        and plan_capability != slot_capability
    ):
        section_header = _SECTION_HEADERS.get(plan_capability, plan_capability)
        return (
            "invalid",
            [],
            [],
            {},
            (
                f"plan section '{section_header}' is for capability "
                f"{plan_capability} but the plan slot is for capability {slot_capability}"
            ),
        )

    # Wrong content: content is for a different capability than requested.
    if plan_capability and plan_capability != capability:
        section_header = _SECTION_HEADERS.get(plan_capability, plan_capability)
        return (
            "invalid",
            [],
            [],
            {},
            (
                f"plan section '{section_header}' is for capability "
                f"{plan_capability} but the plan slot is for capability {capability}"
            ),
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
        expanded_set = set(expanded_file_list)
        for entry in valid_entries:
            if (
                entry.source_path
                and entry.source_path not in expanded_set
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


# ---------------------------------------------------------------------------
# walk_output / diff / run
# ---------------------------------------------------------------------------

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
                rel = os.path.relpath(full, output_root).replace(os.sep, "/")
            except ValueError:
                rel = name
            if not is_within_root(output_root, full):
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


def run(
    contract: OutputContract,
    *,
    expanded_file_list: Iterable[str] | None = None,
    validated_source_roots: Iterable[str] | None = None,
) -> GateReport:
    """Run the full gate: parse the plan, walk the output, diff."""
    status, invalid_entries, entries, committed, error = parse_plan_with_status(
        contract.capability,
        contract.plan_path,
        validated_source_roots=validated_source_roots,
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
