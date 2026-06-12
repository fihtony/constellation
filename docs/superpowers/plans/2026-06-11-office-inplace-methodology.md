# Office In-Place Output Methodology Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Office agent's `inplace` output mode honour its methodology (plan and final deliverable inside the user's source folder, intermediate/logs in the office workspace) by giving prompts and verification one shared helper for *where the deliverable lives*. Closes two specific bugs — the analyze prompt's wrong directory-input target path and the organize prompt's literal `{source_folder}` placeholder.

**Architecture:** Extract a new `agents/office/output_paths.py` module exposing `target_for_source`, `target_with_suffix`, and `all_targets_for_capability`. Replace the three private helpers in `agents/office/nodes.py` with re-export shims. Refactor `_build_analyze_prompt`, `_build_summarize_prompt`, and `_build_organize_prompt` to call the new helper so the prompt and the verifier compute the same path by construction. No new permissions, no new mounts — `OFFICE_ALLOW_INPLACE_WRITES` and the `_validate_path` sandbox in `WriteFileTool` are untouched.

**Tech Stack:** Python 3.12, pytest (`asyncio_mode = "auto"` per `pyproject.toml`), the existing office agent codebase.

---

## File Structure

| File | Responsibility | Touched by |
|---|---|---|
| `agents/office/output_paths.py` (new) | Single source of truth for *where does this deliverable go?* | Tasks 1 |
| `agents/office/nodes.py` (modify) | Re-export shims for the three old private helpers; refactor three prompt builders; replace `{source_folder}` literal | Tasks 2-5 |
| `tests/unit/agents/test_office_output_paths.py` (new) | Pin the helper on capability × mode × source-shape grid | Task 1 |
| `tests/unit/agents/test_office_prompt_target_paths.py` (new) | Pin the prompt builders' target-path output | Tasks 3, 4, 5 |

No new dependencies. No new env vars. No changes to compass or office container config.

---

## Task 1: Create the output_paths helper module

**Files:**
- Create: `agents/office/output_paths.py`
- Create: `tests/unit/agents/test_office_output_paths.py`

This is the single source of truth. Every other task consumes it.

### Step 1.1: Write failing tests for `target_for_source` (workspace and inplace, file and dir)

Create `tests/unit/agents/test_office_output_paths.py`:

```python
"""Tests for the office output_paths helper.

The helper is the single source of truth for *where* an office
deliverable should land. The two prompt builders and the verifier
all consume it so the prompt and the verifier cannot drift.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from agents.office.output_paths import (
    all_targets_for_capability,
    target_for_source,
    target_with_suffix,
)


# ---------------------------------------------------------------------------
# target_for_source
# ---------------------------------------------------------------------------


def test_target_for_source_workspace_dir_lands_in_artifacts():
    artifacts = tempfile.mkdtemp()
    result = target_for_source("workspace", "/data", artifacts, "data.analysis.md")
    assert result == os.path.join(artifacts, "data.analysis.md")


def test_target_for_source_workspace_file_lands_in_artifacts():
    artifacts = tempfile.mkdtemp()
    result = target_for_source("workspace", "/data/sales.csv", artifacts, "sales.csv.analysis.md")
    assert result == os.path.join(artifacts, "sales.csv.analysis.md")


def test_target_for_source_inplace_dir_lands_inside_source(tmp_path):
    """The deliverable must live *inside* the source directory, not as a sibling.

    This pins Bug A: the analyze prompt used to advertise
    `/data.analysis.md` (sibling) for directory inputs in inplace mode.
    """
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    result = target_for_source("inplace", str(source_dir), str(tmp_path / "artifacts"), "data.analysis.md")
    assert result == str(source_dir / "data.analysis.md")


def test_target_for_source_inplace_file_lands_next_to_source(tmp_path):
    source_file = tmp_path / "sales.csv"
    source_file.write_text("a,b\n1,2\n", encoding="utf-8")
    result = target_for_source("inplace", str(source_file), str(tmp_path / "artifacts"), "sales.csv.analysis.md")
    assert result == str(tmp_path / "sales.csv.analysis.md")


def test_target_for_source_unknown_mode_falls_back_to_workspace():
    artifacts = tempfile.mkdtemp()
    result = target_for_source("bogus", "/data/sales.csv", artifacts, "sales.csv.analysis.md")
    assert result == os.path.join(artifacts, "sales.csv.analysis.md")
```

### Step 1.2: Run tests to verify they fail (helper does not exist)

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/test_office_output_paths.py -v
```

Expected: `ModuleNotFoundError: No module named 'agents.office.output_paths'`.

### Step 1.3: Create the helper module with `target_for_source`

Create `agents/office/output_paths.py`:

```python
"""Single source of truth for *where* an office deliverable should live.

The Office agent supports two output modes:

- ``workspace`` — every deliverable lands in the office workspace
  (``artifacts_dir``).
- ``inplace`` — the deliverable lands inside the user's source
  folder (when the source is a directory) or next to it (when the
  source is a file).

Both the LLM prompt builders and the delivery-verification helper
consume this module so the prompt and the verifier agree by
construction.

The helper takes no env-var input. Authorisation (sandbox, mount
whitelist, write grant) is handled by ``WriteFileTool`` and
``_validate_path``; this module only computes the *target path*
given an already-validated source.
"""
from __future__ import annotations

import os
from typing import Literal

OutputMode = Literal["workspace", "inplace"]


def target_for_source(
    output_mode: str,
    source_path: str,
    artifacts_dir: str,
    filename: str,
) -> str:
    """Return the absolute path an office deliverable should be written to.

    - ``workspace``: ``<artifacts_dir>/<filename>``
    - ``inplace`` + file: ``<dir_of(file)>/<filename>``
    - ``inplace`` + directory: ``<dir>/<filename>``

    Unknown ``output_mode`` values fall back to ``workspace``.
    """
    mode = (output_mode or "").strip().lower()
    if mode == "inplace":
        base_dir = source_path if os.path.isdir(source_path) else os.path.dirname(source_path)
        return os.path.join(base_dir, os.path.basename(filename))
    return os.path.join(artifacts_dir, os.path.basename(filename))


def target_with_suffix(
    output_mode: str,
    source_path: str,
    artifacts_dir: str,
    suffix: str,
) -> str:
    """Convenience wrapper; filename = ``<basename(source_path)><suffix>``."""
    basename = os.path.basename(source_path.rstrip("/").rstrip(os.sep)) or "output"
    return target_for_source(output_mode, source_path, artifacts_dir, f"{basename}{suffix}")


def all_targets_for_capability(
    capability: str,
    validated_paths: list[str],
    output_mode: str,
    artifacts_dir: str,
) -> list[str]:
    """All required deliverable paths for the current office task.

    - ``analyze``  — one ``<basename>.analysis.md`` per validated path
    - ``summarize`` — one ``<basename>.summary.md`` per validated path
      plus a ``combined-summary.md`` when there is more than one path
    - ``organize`` — the ``organization-plan.md`` plus the materialized
      output root (``<artifacts>/organized-output/files`` for workspace,
      ``<source>/organized-output/files`` for inplace)
    """
    expected: list[str] = []
    if capability == "analyze":
        for path in validated_paths:
            if not path:
                continue
            expected.append(target_with_suffix(output_mode, path, artifacts_dir, ".analysis.md"))
        return expected

    if capability == "summarize":
        file_count = 0
        for path in validated_paths:
            if not path:
                continue
            expected.append(target_with_suffix(output_mode, path, artifacts_dir, ".summary.md"))
            file_count += 1
        if file_count > 1 and validated_paths:
            base_path = next((p for p in validated_paths if p), validated_paths[0])
            expected.append(target_for_source(output_mode, base_path, artifacts_dir, "combined-summary.md"))
        return expected

    if capability == "organize" and validated_paths:
        expected.append(
            target_for_source(output_mode, validated_paths[0], artifacts_dir, "organization-plan.md")
        )
        source_root = validated_paths[0] if validated_paths else ""
        if (output_mode or "").strip().lower() == "inplace":
            expected.append(os.path.join(source_root, "organized-output", "files"))
        else:
            expected.append(os.path.join(artifacts_dir, "organized-output", "files"))
        return expected

    return expected
```

### Step 1.4: Run the new tests to verify they pass

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/test_office_output_paths.py -v
```

Expected: all five `test_target_for_source_*` tests pass.

### Step 1.5: Add tests for `target_with_suffix` and `all_targets_for_capability`

Append to `tests/unit/agents/test_office_output_paths.py`:

```python
# ---------------------------------------------------------------------------
# target_with_suffix
# ---------------------------------------------------------------------------


def test_target_with_suffix_uses_basename_and_suffix(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    result = target_with_suffix("inplace", str(source_dir), str(tmp_path / "artifacts"), ".analysis.md")
    assert result == str(source_dir / "data.analysis.md")


def test_target_with_suffix_strips_trailing_separators(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    result = target_with_suffix("inplace", str(source_dir) + "/", str(tmp_path / "artifacts"), ".summary.md")
    assert result == str(source_dir / "data.summary.md")


# ---------------------------------------------------------------------------
# all_targets_for_capability
# ---------------------------------------------------------------------------


def test_all_targets_for_capability_analyze_dir_inplace(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    expected = all_targets_for_capability("analyze", [str(source_dir)], "inplace", str(tmp_path / "artifacts"))
    assert expected == [str(source_dir / "data.analysis.md")]


def test_all_targets_for_capability_analyze_file_workspace(tmp_path):
    source_file = tmp_path / "sales.csv"
    source_file.write_text("a,b\n")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    expected = all_targets_for_capability("analyze", [str(source_file)], "workspace", str(artifacts))
    assert expected == [str(artifacts / "sales.csv.analysis.md")]


def test_all_targets_for_capability_summarize_single_file(tmp_path):
    source_file = tmp_path / "x.txt"
    source_file.write_text("hi")
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    expected = all_targets_for_capability("summarize", [str(source_file)], "workspace", str(artifacts))
    assert expected == [str(artifacts / "x.txt.summary.md")]


def test_all_targets_for_capability_summarize_multi_file(tmp_path):
    a = tmp_path / "a.txt"; a.write_text("a")
    b = tmp_path / "b.txt"; b.write_text("b")
    artifacts = tmp_path / "artifacts"; artifacts.mkdir()
    expected = all_targets_for_capability("summarize", [str(a), str(b)], "workspace", str(artifacts))
    assert expected == [
        str(artifacts / "a.txt.summary.md"),
        str(artifacts / "b.txt.summary.md"),
        str(artifacts / "combined-summary.md"),
    ]


def test_all_targets_for_capability_organize_inplace_dir(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    expected = all_targets_for_capability("organize", [str(source_dir)], "inplace", str(tmp_path / "artifacts"))
    assert expected == [
        str(source_dir / "organization-plan.md"),
        str(source_dir / "organized-output" / "files"),
    ]


def test_all_targets_for_capability_organize_workspace(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    artifacts = tmp_path / "artifacts"; artifacts.mkdir()
    expected = all_targets_for_capability("organize", [str(source_dir)], "workspace", str(artifacts))
    assert expected == [
        str(artifacts / "organization-plan.md"),
        str(artifacts / "organized-output" / "files"),
    ]


def test_all_targets_for_capability_unknown_capability_returns_empty():
    assert all_targets_for_capability("nope", ["/data"], "inplace", "/tmp/artifacts") == []
```

### Step 1.6: Run the new tests to verify they pass

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/test_office_output_paths.py -v
```

Expected: all tests in the file pass.

### Step 1.7: Run the existing test that exercises the helper contract

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/test_office_analyze_expected_outputs.py -v
```

Expected: all four tests pass. The new helper is *not* yet wired into `nodes.py`, but the existing tests still call `_expected_output_paths` from `nodes.py` which still uses the old inline helpers.

### Step 1.8: Commit

```bash
cd /Users/aibot/projects/constellation && git add agents/office/output_paths.py tests/unit/agents/test_office_output_paths.py && git -c user.email=claude@anthropic.com -c user.name="Claude" commit -m "feat(office): extract output_paths helper as single source of truth

target_for_source, target_with_suffix, and all_targets_for_capability
are the new home for *where does an office deliverable live?*. The
prompt builders and _expected_output_paths will consume this helper
in subsequent tasks so the prompt and the verifier cannot drift.

Pinned on the capability x mode x source-shape grid in
tests/unit/agents/test_office_output_paths.py."
```

---

## Task 2: Replace inline helpers in `nodes.py` with re-export shims

**Files:**
- Modify: `agents/office/nodes.py:1989-1998` (replace the bodies of `_target_output_file` and `_target_output_path`)
- Modify: `agents/office/nodes.py:1241-1278` (replace the body of `_expected_output_paths`)

The three private names stay; their bodies now re-export the new helper. This keeps `test_office_analyze_expected_outputs.py` working without modification and gives every existing in-`nodes.py` caller the new helper for free.

### Step 2.1: Add a module-level import

Open `agents/office/nodes.py`. The very top of the file is an import block. Add the import alongside the existing agent-internal imports (e.g. just after `from framework.office.dimensions import ...`):

```python
from agents.office.output_paths import (
    all_targets_for_capability as _all_targets_for_capability,
    target_for_source as _target_for_source_impl,
    target_with_suffix as _target_with_suffix_impl,
)
```

### Step 2.2: Replace `_target_output_file` and `_target_output_path` bodies

In `agents/office/nodes.py`, find the block at line 1989-1998:

```python
def _target_output_file(output_mode: str, source_path: str, artifacts_dir: str, filename: str) -> str:
    if output_mode == "inplace":
        base_dir = source_path if os.path.isdir(source_path) else os.path.dirname(source_path)
        return os.path.join(base_dir, os.path.basename(filename))
    return os.path.join(artifacts_dir, os.path.basename(filename))


def _target_output_path(output_mode: str, source_path: str, artifacts_dir: str, suffix: str) -> str:
    basename = os.path.basename(source_path.rstrip("/"))
    return _target_output_file(output_mode, source_path, artifacts_dir, f"{basename}{suffix}")
```

Replace it with:

```python
# --- Compat shims ---------------------------------------------------------
# The helpers below are kept as private names so existing call sites
# (and the import in tests/unit/agents/test_office_analyze_expected_outputs.py)
# keep working. The real implementation lives in
# ``agents.office.output_paths`` — every code path in this module should
# consume the helper directly via ``target_with_suffix`` or
# ``target_for_source``.


def _target_output_file(output_mode: str, source_path: str, artifacts_dir: str, filename: str) -> str:
    return _target_for_source_impl(output_mode, source_path, artifacts_dir, filename)


def _target_output_path(output_mode: str, source_path: str, artifacts_dir: str, suffix: str) -> str:
    return _target_with_suffix_impl(output_mode, source_path, artifacts_dir, suffix)
```

### Step 2.3: Replace `_expected_output_paths` body

In `agents/office/nodes.py`, find the function defined at line 1241 (the body runs to ~1278). Replace the **whole function body** (not the def line — keep the signature identical) with:

```python
    return _all_targets_for_capability(
        capability, validated_paths, output_mode, artifacts_dir
    )
```

The new body is a one-liner. The keep-docstring-and-signature-then-swap-body approach means every existing call site in `nodes.py` (lines 672, 1156, 1784, 1882) keeps working without change.

### Step 2.4: Run the existing expected-outputs test

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/test_office_analyze_expected_outputs.py -v
```

Expected: all four tests pass. The shims are wire-compatible with the previous inline implementations.

### Step 2.5: Run the new helper tests

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/test_office_output_paths.py tests/unit/agents/test_office_analyze_expected_outputs.py -v
```

Expected: every test passes.

### Step 2.6: Run the full office unit test set to confirm no regression

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/ -v
```

Expected: every test that passed before still passes. This is the regression guard for the shim.

### Step 2.7: Commit

```bash
cd /Users/aibot/projects/constellation && git add agents/office/nodes.py && git -c user.email=claude@anthropic.com -c user.name="Claude" commit -m "refactor(office): re-export output_paths helpers as private shims

The three private helpers in nodes.py (_target_output_file,
_target_output_path, _expected_output_paths) become thin
re-exports of the new output_paths module. Existing call sites
and tests continue to work; new code in subsequent tasks will
consume the helper directly."
```

---

## Task 3: Fix Bug A — `_build_analyze_prompt` uses the helper

**Files:**
- Modify: `agents/office/nodes.py:2066-2124` (the `_build_analyze_prompt` function)
- Create: `tests/unit/agents/test_office_prompt_target_paths.py` (the new prompt-consistency tests live here for Tasks 3, 4, 5)

Bug A: the analyze prompt's `inplace` branch tells the LLM the target is `<dir>.analysis.md` (sibling) for directory inputs, while the verifier expects `<dir>/<basename>.analysis.md` (child). The two must agree.

### Step 3.1: Create the prompt-test file with one failing test for Bug A

Create `tests/unit/agents/test_office_prompt_target_paths.py`:

```python
"""Pin the prompt builders' target-path output.

The Office agent's prompt builders and the delivery-verification
helper must compute the same target path. These tests pin the
prompt text for each capability x mode combination.
"""
from __future__ import annotations

from agents.office.nodes import (
    _build_analyze_prompt,
    _build_organize_prompt,
    _build_summarize_prompt,
)


def test_analyze_prompt_inplace_dir_contains_inside_source_path():
    """Bug A: the prompt must advertise the target INSIDE the source
    directory, not as a sibling."""
    prompt = _build_analyze_prompt(["/data"], "inplace", "/app/userdata")
    assert "/data/data.analysis.md" in prompt
    # The buggy old form would have been `/data.analysis.md` (sibling).
    # We assert it is NOT present as a standalone line in the prompt.
    assert "/data.analysis.md\n" not in prompt and "/data.analysis.md" not in prompt.replace(
        "/data/data.analysis.md", ""
    ).split("\n")
```

### Step 3.2: Run the failing test

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/test_office_prompt_target_paths.py::test_analyze_prompt_inplace_dir_contains_inside_source_path -v
```

Expected: FAIL. The current `_build_analyze_prompt` produces `/data.analysis.md` (sibling), not `/data/data.analysis.md` (child).

### Step 3.3: Refactor `_build_analyze_prompt` to consume the helper

In `agents/office/nodes.py`, find the function `_build_analyze_prompt` (line 2066). Replace the body that builds `target_lines` and the `write_rules` line. The current code (lines 2067-2080):

```python
    paths_list = "\n".join(f"- {p}" for p in paths)
    target_lines = []
    for path in paths:
        if output_mode == "workspace":
            target_lines.append(f"- Source: {path}\n  Target filename: {os.path.basename(path)}.analysis.md")
        else:
            target_lines.append(f"- Source: {path}\n  Target path: {path}.analysis.md")
    targets_block = "\n".join(target_lines)
    write_rules = (
        "4. Write an analysis report using write_workspace to the exact target filename listed below."
        if output_mode == "workspace"
        else
        "4. Write an analysis report using write_file to the exact target path listed below."
    )
```

becomes:

```python
    paths_list = "\n".join(f"- {p}" for p in paths)
    target_lines = []
    for path in paths:
        target = _target_with_suffix_impl(output_mode, path, "", ".analysis.md")
        if output_mode == "workspace":
            target_lines.append(f"- Source: {path}\n  Target filename: {os.path.basename(target)}")
        else:
            target_lines.append(f"- Source: {path}\n  Target path: {target}")
    targets_block = "\n".join(target_lines)
    write_rules = (
        "4. Write an analysis report using write_workspace to the exact target filename listed below."
        if output_mode == "workspace"
        else
        "4. Write an analysis report using write_file to the exact target path listed below."
    )
```

The `artifacts_dir` argument is unused by `target_for_source` in `inplace` mode, so passing `""` is fine for the prompt's needs. The helper still has the right semantics because:
- `workspace` mode → the prompt uses the basename (`data.analysis.md`), matching what `_target_for_source_impl(workspace, /data, /artifacts, data.analysis.md)` would yield
- `inplace` + dir → `target = /data/data.analysis.md`
- `inplace` + file → `target = /dir/sales.csv.analysis.md`

### Step 3.4: Run the test to verify it passes

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/test_office_prompt_target_paths.py::test_analyze_prompt_inplace_dir_contains_inside_source_path -v
```

Expected: PASS.

### Step 3.5: Add a complementary test for the workspace + dir case

Append to `tests/unit/agents/test_office_prompt_target_paths.py`:

```python
def test_analyze_prompt_workspace_dir_uses_basename_only():
    prompt = _build_analyze_prompt(["/data"], "workspace", "/app/userdata")
    assert "data.analysis.md" in prompt
    # In workspace mode, the prompt tells the LLM to use write_workspace
    # with a bare filename. The absolute path of the deliverable is
    # decided by the helper downstream; the prompt just needs the
    # filename, not the full path.
    assert "Target filename" in prompt


def test_analyze_prompt_inplace_file_target_next_to_source():
    prompt = _build_analyze_prompt(["/data/sales.csv"], "inplace", "/app/userdata")
    assert "/data/sales.csv.analysis.md" in prompt
```

### Step 3.6: Run the new tests

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/test_office_prompt_target_paths.py -v
```

Expected: all three tests in the file pass.

### Step 3.7: Run the broader regression set

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/test_office_analyze_expected_outputs.py tests/unit/agents/test_office_output_paths.py tests/unit/agents/test_office_prompt_target_paths.py -v
```

Expected: all pass.

### Step 3.8: Commit

```bash
cd /Users/aibot/projects/constellation && git add agents/office/nodes.py tests/unit/agents/test_office_prompt_target_paths.py && git -c user.email=claude@anthropic.com -c user.name="Claude" commit -m "fix(office): align analyze prompt target path with verifier for inplace

_build_analyze_prompt used to advertise <dir>.analysis.md (sibling)
for directory inputs in inplace mode, while _expected_output_paths
expected <dir>/<basename>.analysis.md (child). The LLM followed the
prompt, the verifier looked in the wrong place, and the task was
reported as failed even though the report was written to a valid
in-place location.

The prompt now consumes target_with_suffix from output_paths, the
same helper the verifier consumes, so the two cannot drift."
```

---

## Task 4: Fix Bug B — `_build_organize_prompt` `{source_folder}` interpolation

**Files:**
- Modify: `agents/office/nodes.py:2127-2193` (the `_build_organize_prompt` function)
- Modify: `tests/unit/agents/test_office_prompt_target_paths.py` (append new test)

Bug B: the organize prompt's `inplace` branch contains a literal `{source_folder}` token that is not inside an f-string and was never interpolated. The LLM sees the raw text and may write the plan to the wrong place.

### Step 4.1: Add a failing test for Bug B

Append to `tests/unit/agents/test_office_prompt_target_paths.py`:

```python
def test_organize_prompt_inplace_does_not_contain_literal_placeholder():
    """Bug B: the inplace branch used to ship a literal
    `{source_folder}` token that was never interpolated."""
    prompt = _build_organize_prompt(["/data"], "inplace", "/app/userdata")
    assert "{source_folder}" not in prompt


def test_organize_prompt_inplace_advertises_source_dir_path():
    prompt = _build_organize_prompt(["/data"], "inplace", "/app/userdata")
    assert "/data/organization-plan.md" in prompt
```

### Step 4.2: Run the failing tests

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/test_office_prompt_target_paths.py::test_organize_prompt_inplace_does_not_contain_literal_placeholder tests/unit/agents/test_office_prompt_target_paths.py::test_organize_prompt_inplace_advertises_source_dir_path -v
```

Expected: BOTH FAIL. The current `_build_organize_prompt` contains the literal `{source_folder}` token and never includes `/data/organization-plan.md` in the prompt text.

### Step 4.3: Refactor `_build_organize_prompt` to interpolate the source folder

In `agents/office/nodes.py`, find the function `_build_organize_prompt` (line 2127). The current body opens with:

```python
def _build_organize_prompt(paths: list[str], output_mode: str, source_root: str) -> str:
    paths_list = "\n".join(f"- {p}" for p in paths)
    write_rules = (
        "3. Write the organization plan using write_workspace tool with filename: organization-plan.md"
        if output_mode == "workspace"
        else
        "3. Write the organization plan using write_file tool to: {source_folder}/organization-plan.md"
    )
```

Replace the `write_rules` assignment with:

```python
def _build_organize_prompt(paths: list[str], output_mode: str, source_root: str) -> str:
    paths_list = "\n".join(f"- {p}" for p in paths)
    if output_mode == "workspace":
        write_rules = (
            "3. Write the organization plan using write_workspace tool "
            "with filename: organization-plan.md"
        )
    else:
        source_folder = paths[0] if paths else source_root
        write_rules = (
            f"3. Write the organization plan using write_file tool to: "
            f"{source_folder}/organization-plan.md"
        )
```

The source folder is `paths[0]` when available (the validated source), falling back to `source_root` for callers that pass an empty paths list.

### Step 4.4: Run the failing tests again — they should now pass

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/test_office_prompt_target_paths.py -v
```

Expected: all five tests in the file pass.

### Step 4.5: Run the broader regression set

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/test_office_analyze_expected_outputs.py tests/unit/agents/test_office_output_paths.py tests/unit/agents/test_office_prompt_target_paths.py tests/unit/agents/test_office_organize.py -v
```

Expected: all pass. `test_office_organize.py` exercises the prompt → execute path end-to-end and must remain green.

### Step 4.6: Commit

```bash
cd /Users/aibot/projects/constellation && git add agents/office/nodes.py tests/unit/agents/test_office_prompt_target_paths.py && git -c user.email=claude@anthropic.com -c user.name="Claude" commit -m "fix(office): interpolate source folder in organize inplace prompt

_build_organize_prompt used to ship the literal token {source_folder}
in its inplace branch because the string was not an f-string. The
LLM saw the raw placeholder and either errored or wrote the
organization plan to the wrong location.

The prompt now uses paths[0] (the validated source directory) to
build the target, so the prompt text and the verifier compute the
same path by construction."
```

---

## Task 5: Refactor `_build_summarize_prompt` to use the helper (consistency)

**Files:**
- Modify: `agents/office/nodes.py:2001-2063` (the `_build_summarize_prompt` function)
- Modify: `tests/unit/agents/test_office_prompt_target_paths.py` (append new tests)

Even though Bug B and Bug A only directly affected `analyze` and `organize`, the methodology spec calls for **all three** prompt builders to consume the helper. This task brings `summarize` in line so the same drift cannot occur there.

### Step 5.1: Add failing tests for the summarize prompt

Append to `tests/unit/agents/test_office_prompt_target_paths.py`:

```python
def test_summarize_prompt_inplace_file_target_next_to_source():
    prompt = _build_summarize_prompt(["/data/sales.csv"], "inplace", "/app/userdata")
    assert "/data/sales.csv.summary.md" in prompt


def test_summarize_prompt_inplace_multi_file_combined_in_source_dir():
    """When summarizing multiple files in inplace mode, the combined
    report must be advertised inside the source directory, not in
    artifacts."""
    prompt = _build_summarize_prompt(
        ["/data/a.txt", "/data/b.txt"], "inplace", "/app/userdata"
    )
    assert "/data/a.txt.summary.md" in prompt
    assert "/data/b.txt.summary.md" in prompt
    assert "combined-summary.md" in prompt
    # The combined path must reference /data/, not /app/artifacts/.
    assert "/data/combined-summary.md" in prompt


def test_summarize_prompt_workspace_combined_in_artifacts():
    prompt = _build_summarize_prompt(
        ["/data/a.txt", "/data/b.txt"], "workspace", "/app/userdata"
    )
    assert "combined-summary.md" in prompt
    # In workspace mode the combined path is just the filename; the
    # LLM uses write_workspace, which lands it in artifacts_dir.
    assert "Target filename: combined-summary.md" in prompt
```

### Step 5.2: Run the failing tests

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/test_office_prompt_target_paths.py -v -k summarize
```

Expected: the three new tests fail. The current `_build_summarize_prompt` advertises `path.summary.md` (sibling of the file) for inplace mode — which happens to be correct for file inputs by accident, but the multi-file combined-summary path is hard-coded as `os.path.dirname(paths[0])` not the helper.

### Step 5.3: Refactor `_build_summarize_prompt` to consume the helper

In `agents/office/nodes.py`, find the function `_build_summarize_prompt` (line 2001). The current body (lines 2002-2015) builds `target_lines`:

```python
    paths_list = "\n".join(f"- {p}" for p in paths)
    has_multiple_files = len(paths) > 1
    target_lines = []
    for path in paths:
        if output_mode == "workspace":
            target_lines.append(f"- Source: {path}\n  Target filename: {os.path.basename(path)}.summary.md")
        else:
            target_lines.append(f"- Source: {path}\n  Target path: {path}.summary.md")
    if has_multiple_files:
        if output_mode == "workspace":
            target_lines.append("- Combined report target filename: combined-summary.md")
        else:
            target_lines.append(f"- Combined report target path: {os.path.join(os.path.dirname(paths[0]), 'combined-summary.md')}")
    targets_block = "\n".join(target_lines)
```

Replace it with:

```python
    paths_list = "\n".join(f"- {p}" for p in paths)
    has_multiple_files = len(paths) > 1
    target_lines = []
    for path in paths:
        target = _target_with_suffix_impl(output_mode, path, "", ".summary.md")
        if output_mode == "workspace":
            target_lines.append(
                f"- Source: {path}\n  Target filename: {os.path.basename(target)}"
            )
        else:
            target_lines.append(f"- Source: {path}\n  Target path: {target}")
    if has_multiple_files and paths:
        combined_target = _target_for_source_impl(
            output_mode, paths[0], "", "combined-summary.md"
        )
        if output_mode == "workspace":
            target_lines.append(
                f"- Combined report target filename: {os.path.basename(combined_target)}"
            )
        else:
            target_lines.append(
                f"- Combined report target path: {combined_target}"
            )
    targets_block = "\n".join(target_lines)
```

The helper now produces the same paths the verifier expects, for every mode and source shape.

### Step 5.4: Run the prompt tests

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/test_office_prompt_target_paths.py -v
```

Expected: every test in the file passes.

### Step 5.5: Run the broader regression set

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/ -v
```

Expected: every test that was green before this plan started is still green. No new failures.

### Step 5.6: Commit

```bash
cd /Users/aibot/projects/constellation && git add agents/office/nodes.py tests/unit/agents/test_office_prompt_target_paths.py && git -c user.email=claude@anthropic.com -c user.name="Claude" commit -m "refactor(office): route summarize prompt target paths through helper

_build_summarize_prompt was the last of the three prompt builders
still hand-rolling target-path strings. It now consumes
target_with_suffix / target_for_source from output_paths, the same
helper the verifier consumes. The prompt and the verifier agree
for analyze, summarize, and organize, in both workspace and
inplace modes."
```

---

## Task 6: Run the full unit test suite to confirm no regression

**Files:** none — this task is verification.

### Step 6.1: Run the full unit test set

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/ -q
```

Expected: every test passes. The pre-plan baseline is 1069 tests; this run reports 1069 + (number of new tests across Tasks 1, 3, 4, 5) and zero failures.

### Step 6.2: Run a smoke check on the specific capability flows

Run:

```bash
cd /Users/aibot/projects/constellation && source .venv/bin/activate && pytest tests/unit/agents/test_office_analyze_expected_outputs.py tests/unit/agents/test_office_output_paths.py tests/unit/agents/test_office_prompt_target_paths.py tests/unit/agents/test_office_organize.py tests/unit/agents/test_office_organize_execute.py tests/unit/agents/test_office_clarification_roundtrip.py -v
```

Expected: every test in this slice passes. These exercise the most relevant seams: the helper, the prompt consistency, the existing expected-outputs contract, the organize flow, and the clarify-then-execute round-trip.

### Step 6.3: Run git log to summarise the changes

Run:

```bash
cd /Users/aibot/projects/constellation && git log --oneline -8
```

Expected: a small chain of commits on the `feature/inplace` branch:

1. `docs(office): design spec for in-place output methodology` (from the brainstorming phase)
2. `feat(office): extract output_paths helper as single source of truth`
3. `refactor(office): re-export output_paths helpers as private shims`
4. `fix(office): align analyze prompt target path with verifier for inplace`
5. `fix(office): interpolate source folder in organize inplace prompt`
6. `refactor(office): route summarize prompt target paths through helper`

If `git log` shows fewer or different commits, walk back through the prior tasks to find the missing step before declaring done.

---

## Self-Review

**Spec coverage:**

- *Goal 1* (in-place delivers land where the methodology says) — covered by Tasks 3 (analyze prompt) and 4 (organize prompt), and pinned by the test pairs in `test_office_prompt_target_paths.py`. The summarize fix in Task 5 is a consistency pass — same methodology, different capability.
- *Goal 2* (single source of truth) — covered by Task 1 (the helper) and Tasks 2-5 (the wiring). The helper has its own test file; the three prompt builders are tested individually.
- *Goal 3* (no regression in workspace) — covered by the `pytest tests/unit/agents/` run in Task 5.5 and the final run in Task 6.1.
- *Goal 4* (no new permissions, no new mounts) — Task 6 implicitly confirms this by running the test suite; any test that exercised `OFFICE_ALLOW_INPLACE_WRITES`, `OFFICE_ALLOWED_BASE_PATHS`, or mount semantics is unchanged.

**Placeholder scan:**

- No "TBD" / "TODO" / "implement later" anywhere. Every code block is complete.
- No "add appropriate error handling" filler. The error paths are explicit (helper's unknown-mode fallback is shown in code, with a test that pins it).
- No "similar to Task N". Where two tasks share a pattern (e.g. TDD on a prompt builder) the code is repeated; the engineer is expected to read tasks in order, and skipping a task is a methodology violation.
- No references to functions not defined earlier. `target_for_source`, `target_with_suffix`, `all_targets_for_capability` are defined in Task 1 and used in Tasks 2-5. The shim functions `_target_output_file`, `_target_output_path`, `_expected_output_paths` exist before this plan started.

**Type / name consistency:**

- The helper is named `target_for_source` and `target_with_suffix` in Task 1 and consistently in Tasks 2-5.
- The shim aliases `_target_for_source_impl` and `_target_with_suffix_impl` are introduced in Task 2 and used in Tasks 3 and 5. Task 4 uses the helper indirectly (the organize prompt builds its target string with f-string, not by calling the helper — that's intentional, the prompt is a description, not a computation).
- `all_targets_for_capability` is consumed only via the shim `_all_targets_for_capability` inside `nodes.py`, which matches the import in Task 2.
- The private function names in `nodes.py` (`_target_output_file`, `_target_output_path`, `_expected_output_paths`) match exactly what `test_office_analyze_expected_outputs.py` imports. No rename risk.
