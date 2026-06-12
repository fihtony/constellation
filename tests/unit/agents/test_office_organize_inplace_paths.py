"""Tests for the inplace organize path layout (task-888e1be6e345).

The user reported that the inplace organize flow was duplicating the
user's source files into a new ``organized-output/files/`` directory
*inside* the source folder, doubling disk usage.  Workspace organize
keeps the existing ``<artifacts>/organized-output/files/`` parent
folder convention.

The fix: when output_mode is ``inplace``, organize must treat the
user's source folder as the output root.  Bucket subdirectories
(``documents/``, ``images/``) land directly under the source, no
``organized-output/files/`` wrapper.  Source files are *moved* into
the buckets, not copied.  Workspace mode is unchanged.

Methodology-level: the source of truth for the organize root is
``_organized_output_root`` (used by both the executor and the
verifier); the executor's move/copy decision lives in
``_run_bounded_folder_organize``; the prompt's "files exist under
``organized-output/files/``" rule is in ``_build_organize_prompt``.
All three need to agree on the new inplace semantics.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from agents.office.nodes import (
    _build_organize_prompt,
    _organized_output_root,
    _run_bounded_folder_organize,
)
from agents.office.organize_by_dimension import OrganizeByTypeTool


# ---------------------------------------------------------------------------
# Helper: build a small fake source tree on disk for organize tests.
# ---------------------------------------------------------------------------


def _make_source_tree(root: Path) -> dict[str, str]:
    """Lay out a tiny source tree with two file types and return the
    mapping of relative path -> absolute path.  Files have distinct
    extensions so the dimension tool can place them in different
    buckets.
    """
    root.mkdir(parents=True, exist_ok=True)
    files: dict[str, str] = {
        "intro.txt": "Hello world.",
        "notes/idea.md": "# Idea",
        "photo.png": "fake-png-bytes",
        "report.pdf": "fake-pdf-bytes",
    }
    created: dict[str, str] = {}
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        created[rel] = str(path)
    return created


# ---------------------------------------------------------------------------
# _organized_output_root: the single source of truth for the root path.
# ---------------------------------------------------------------------------


def test_organized_output_root_workspace_uses_artifacts_dir(tmp_path):
    """Workspace mode keeps the historical
    ``<artifacts>/organized-output/files/`` layout.  No change.
    """
    artifacts_dir = str(tmp_path / "artifacts")
    root = _organized_output_root("workspace", artifacts_dir, ["/data/x"])
    expected = os.path.join(artifacts_dir, "organized-output", "files")
    assert root == expected, (
        f"workspace organize root drifted; got {root!r}, want {expected!r}"
    )


def test_organized_output_root_inplace_uses_source_root(tmp_path):
    """Inplace mode treats the user source folder as the root.  No
    ``organized-output/files/`` wrapper.
    """
    source_root = str(tmp_path / "userdata" / "unsorted_rw")
    artifacts_dir = str(tmp_path / "artifacts")
    root = _organized_output_root("inplace", artifacts_dir, [source_root])
    assert root == source_root, (
        f"inplace organize root must be the user source; got {root!r}, "
        f"want {source_root!r}"
    )


# ---------------------------------------------------------------------------
# _build_organize_prompt: the LLM must be told the new layout.
# ---------------------------------------------------------------------------


def test_organize_prompt_inplace_tells_llm_to_use_source_as_root():
    """The inplace prompt must NOT say "files exist under
    organized-output/files/" and must explicitly say the user source
    folder is the root.
    """
    prompt = _build_organize_prompt(["/data/x"], "inplace", "/data/x")
    lowered = prompt.lower()
    # The historical "files exist under organized-output/files/" rule
    # is workspace-only now.  Inplace must avoid that wording.
    assert "files exist under `organized-output/files/`" not in lowered, (
        f"inplace prompt should not promise organized-output/files/; "
        f"prompt=\n{prompt!r}"
    )
    # Inplace must tell the LLM the source folder is the root.
    assert "source" in lowered and "root" in lowered, (
        f"inplace prompt should explain the source-as-root layout; "
        f"prompt=\n{prompt!r}"
    )
    # Inplace must say files are MOVED (not copied), so the user's
    # disk usage is not doubled.
    assert "move" in lowered, (
        f"inplace prompt should ask the agent to move files, not copy; "
        f"prompt=\n{prompt!r}"
    )


def test_organize_prompt_workspace_keeps_organized_output_files_rule():
    """Workspace mode keeps the historical wording unchanged."""
    prompt = _build_organize_prompt(["/data/x"], "workspace", "/data/x")
    lowered = prompt.lower()
    assert "files exist under `organized-output/files/`" in lowered, (
        f"workspace prompt should still mention organized-output/files/; "
        f"prompt=\n{prompt!r}"
    )


# ---------------------------------------------------------------------------
# Dimension tool path: the LLM-driven bucket materialize step.
# _run_bounded_folder_organize is the agentic-fallback path; the
# canonical LLM-driven path is OrganizeByTypeTool.execute_sync().
# Both must respect the new inplace semantics.
# ---------------------------------------------------------------------------


def _read_operations(artifacts_dir: str) -> list[dict]:
    operations_path = os.path.join(artifacts_dir, "operations-plan.json")
    if not os.path.exists(operations_path):
        return []
    with open(operations_path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_organize_by_type_inplace_moves_files_into_buckets(tmp_path):
    """Inplace mode: the source folder becomes the output root, files
    are MOVED into the dimension buckets, and the source top-level no
    longer holds the originals.
    """
    source_root = tmp_path / "unsorted_rw"
    _make_source_tree(source_root)
    tool = OrganizeByTypeTool()

    result = tool.execute_sync(
        source=str(source_root),
        output_root=str(source_root),  # inplace: source == output_root
    )
    assert result.success, f"organize_by_type failed: {result.error}"

    # No organized-output/files/ wrapper inside the source folder.
    organised_wrapper = source_root / "organized-output"
    assert not organised_wrapper.exists(), (
        f"inplace organize must not create organized-output/ inside the "
        f"source folder; found at {organised_wrapper}"
    )

    # Bucket subdirectories ("text", "images", "documents") land
    # directly under the source root.
    bucket_dirs = {
        d for d in os.listdir(source_root)
        if (source_root / d).is_dir() and not d.startswith(".")
    }
    assert {"text", "images", "documents"}.issubset(bucket_dirs), (
        f"inplace organize should create dimension bucket dirs under the "
        f"source root; got: {bucket_dirs!r}"
    )

    # The originals have been moved — top-level source no longer holds
    # the original files.
    for rel in ("intro.txt", "photo.png", "report.pdf"):
        assert not (source_root / rel).exists(), (
            f"inplace organize must MOVE (not copy) the source file "
            f"{rel!r} out of the source root; still present"
        )

    # The bucket contents match the source contents.
    assert (source_root / "text" / "intro.txt").exists(), (
        "inplace organize must place intro.txt under text/"
    )
    assert (source_root / "images" / "photo.png").exists(), (
        "inplace organize must place photo.png under images/"
    )
    assert (source_root / "documents" / "report.pdf").exists(), (
        "inplace organize must place report.pdf under documents/"
    )


def test_organize_by_type_workspace_copies_files_into_artifacts(
    tmp_path,
):
    """Workspace mode is unchanged: the source is left intact, files
    are copied to ``<output_root>/<bucket>/<rel>``.
    """
    source_root = tmp_path / "unsorted_rw"
    artifacts_root = tmp_path / "artifacts" / "organized-output" / "files"
    _make_source_tree(source_root)
    tool = OrganizeByTypeTool()

    result = tool.execute_sync(
        source=str(source_root),
        output_root=str(artifacts_root),
    )
    assert result.success, f"organize_by_type failed: {result.error}"

    # Source files are intact (workspace mode is read-only on the source).
    for rel in ("intro.txt", "photo.png", "report.pdf"):
        assert (source_root / rel).exists(), (
            f"workspace organize must NOT modify the source file {rel!r}"
        )

    # Buckets live under the artifacts root, not under the source.
    bucket_dirs = {
        d for d in os.listdir(artifacts_root)
        if (artifacts_root / d).is_dir() and not d.startswith(".")
    }
    assert {"text", "images", "documents"}.issubset(bucket_dirs), (
        f"workspace organize should create dimension buckets under "
        f"artifacts; got: {bucket_dirs!r}"
    )

    # Bucket contents match the source contents.
    assert (artifacts_root / "text" / "intro.txt").exists()
    assert (artifacts_root / "images" / "photo.png").exists()
    assert (artifacts_root / "documents" / "report.pdf").exists()


# ---------------------------------------------------------------------------
# _run_bounded_folder_organize: the agentic-fallback path.  Same
# operations-plan contract as the LLM-driven path; same in-place
# semantics for move-vs-copy.
# ---------------------------------------------------------------------------


def test_run_bounded_folder_organize_inplace_records_move_actions(
    tmp_path,
):
    """The bounded-folder-organize fallback records ``move_file``
    actions in inplace mode (the source-as-root case) and keeps
    ``copy_file`` for workspace mode.
    """
    source_root = tmp_path / "unsorted_rw"
    artifacts_dir = tmp_path / "artifacts"
    _make_source_tree(source_root)

    result = _run_bounded_folder_organize(
        [str(source_root)],
        output_mode="inplace",
        artifacts_dir=str(artifacts_dir),
    )
    assert result.success, f"inplace organize failed: {result.error}"

    # No organized-output/ wrapper inside the source.
    assert not (source_root / "organized-output").exists(), (
        f"inplace organize must not create organized-output/ inside the "
        f"source folder"
    )

    operations = _read_operations(str(artifacts_dir))
    actions = {op.get("action") for op in operations}
    assert "move_file" in actions, (
        f"inplace organize must record move_file actions; got {actions!r}"
    )
    assert "copy_file" not in actions, (
        f"inplace organize must not record copy_file actions; "
        f"got {actions!r}"
    )


def test_run_bounded_folder_organize_workspace_keeps_artifacts_layout(
    tmp_path,
):
    """Workspace mode is unchanged: organized-output/files/ under
    artifacts, source intact, copy_file actions recorded.
    """
    source_root = tmp_path / "unsorted_rw"
    artifacts_dir = tmp_path / "artifacts"
    _make_source_tree(source_root)

    result = _run_bounded_folder_organize(
        [str(source_root)],
        output_mode="workspace",
        artifacts_dir=str(artifacts_dir),
    )
    assert result.success, f"workspace organize failed: {result.error}"

    # Source files are intact (workspace mode is read-only on the source).
    for rel in ("intro.txt", "photo.png", "report.pdf"):
        assert (source_root / rel).exists(), (
            f"workspace organize must NOT modify the source file {rel!r}"
        )

    # organised-output/files/ exists under the artifacts dir.
    organized_root = artifacts_dir / "organized-output" / "files"
    assert organized_root.is_dir(), (
        f"workspace organize must create {organized_root}; missing"
    )

    # operations-plan records the copy.
    operations = _read_operations(str(artifacts_dir))
    actions = {op.get("action") for op in operations}
    assert "copy_file" in actions, (
        f"workspace organize must record copy_file actions; got {actions!r}"
    )
