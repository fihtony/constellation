"""Tests for the office organize integrity module (task-69d252f1842c).

The user reported two follow-up issues with the in-place organize
flow that fixed the duplication problem (task-888e1be6e345):

1. Empty subdirectories (e.g. ``1/``, ``2/``) were left behind in
   the user's source folder after files had been moved into
   buckets.  The folder should look clean afterwards.
2. There was no audit that proves no file was deleted or modified
   during organize.  Only moving is allowed; the pre/post file
   (name, size, mtime) triples must match.

This module exercises both fixes at the methodology level via
``agents.office.integrity`` — the same helpers consumed by the
bounded-folder fallback in ``nodes.py`` and by every dimension
tool in ``organize_by_dimension.py``.  The tests therefore cover
both code paths in one place.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from agents.office.integrity import (
    check_operations_plan_no_deletes,
    cleanup_empty_dirs,
    snapshot_source,
    verify_post,
)
from agents.office.organize_by_dimension import OrganizeByTypeTool
from agents.office.nodes import _run_bounded_folder_organize


# ---------------------------------------------------------------------------
# Source tree fixtures
# ---------------------------------------------------------------------------


def _touch(path: Path, content: str = "x", mtime: float | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))


def _make_unsorted_with_subdirs(root: Path) -> dict[str, Path]:
    """Lay out the same shape the user complained about in the
    task: top-level files plus a few subdirectories that each
    hold a single file.  All files have distinct sizes so the
    integrity verifier's (size, mtime) match is unambiguous.
    """
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "intro.txt": ("hello world from the top", 11),
        "notes/idea.md": ("# Idea note", 11),
        "1/invoice.pdf": ("pdf-bytes-001", 13),
        "2/photo.png": ("png-bytes-002", 13),
        "3/data.csv": ("a,b\n1,2\n", 8),
    }
    out: dict[str, Path] = {}
    base = 1_700_000_000.0
    for idx, (rel, (content, _)) in enumerate(files.items()):
        p = root / rel
        _touch(p, content, mtime=base + idx)
        out[rel] = p
    return out


# ---------------------------------------------------------------------------
# 1. Empty-folder cleanup
# ---------------------------------------------------------------------------


def test_cleanup_empty_dirs_removes_became_empty_subdirs(tmp_path):
    """After organize moves every file out of ``1/`` and ``2/``,
    the cleanup pass must rmdir both.  ``3/`` still has a file and
    must stay; the source root itself must stay.
    """
    source_root = tmp_path / "unsorted_rw"
    _make_unsorted_with_subdirs(source_root)

    # Simulate organize moving the files out: delete the contents
    # of ``1/`` and ``2/`` and verify cleanup rmdirs the empty
    # leaves, but leaves ``3/`` (still has data.csv) and the root
    # alone.
    (source_root / "1" / "invoice.pdf").unlink()
    (source_root / "2" / "photo.png").unlink()
    # ``3/data.csv`` is still there.

    removed = cleanup_empty_dirs(str(source_root))

    assert (source_root / "1").exists() is False, (
        f"empty subdir 1/ should have been rmdir'd; "
        f"removed list: {removed!r}"
    )
    assert (source_root / "2").exists() is False, (
        f"empty subdir 2/ should have been rmdir'd; "
        f"removed list: {removed!r}"
    )
    assert (source_root / "3").is_dir(), (
        f"non-empty subdir 3/ must NOT be removed; "
        f"removed list: {removed!r}"
    )
    assert (source_root / "3" / "data.csv").exists(), (
        "3/data.csv was not touched by the cleanup"
    )
    assert source_root.is_dir(), (
        "the source root itself must NEVER be removed by cleanup"
    )


def test_cleanup_empty_dirs_preserves_source_root_even_when_empty(tmp_path):
    """A pathological case: every loose file is moved out, every
    subdirectory becomes empty, even the bucket subdirectories.
    The source root itself must still survive.
    """
    source_root = tmp_path / "unsorted_rw"
    source_root.mkdir()
    # Create then immediately empty two subdirs.
    (source_root / "a").mkdir()
    (source_root / "b").mkdir()

    removed = cleanup_empty_dirs(str(source_root))
    assert (source_root / "a").exists() is False
    assert (source_root / "b").exists() is False
    assert source_root.is_dir(), (
        "source root must be preserved even when every leaf is empty"
    )


def test_cleanup_empty_dirs_no_op_for_rootless_path(tmp_path):
    """Cleanup against a non-existent or empty path must be a no-op."""
    assert cleanup_empty_dirs(str(tmp_path / "does-not-exist")) == []
    assert cleanup_empty_dirs("") == []


# ---------------------------------------------------------------------------
# 2. Pre/post integrity snapshot
# ---------------------------------------------------------------------------


def test_snapshot_source_captures_rel_size_mtime_for_every_file(tmp_path):
    """The snapshot must include every non-hidden file under the
    source root, with its size and mtime.  Hidden files are
    skipped (editor state is not part of the integrity contract).
    """
    source_root = tmp_path / "unsorted_rw"
    _make_unsorted_with_subdirs(source_root)
    # Add a hidden file that must NOT appear in the snapshot.
    _touch(source_root / ".DS_Store", "mac", mtime=1_700_000_999.0)

    snap = snapshot_source(str(source_root))
    rels = {entry["rel"] for entry in snap}
    assert rels == {
        "intro.txt",
        "notes/idea.md",
        "1/invoice.pdf",
        "2/photo.png",
        "3/data.csv",
    }, f"snapshot missed files or picked up hidden ones: {rels!r}"
    for entry in snap:
        assert "rel" in entry and "size" in entry and "mtime" in entry
        # All files in the fixture are non-empty.
        assert entry["size"] > 0


def test_verify_post_workspace_passes_when_source_untouched(tmp_path):
    """Workspace mode: source must remain byte-identical at every
    pre-snapshot rel path with the same size and mtime.
    """
    source_root = tmp_path / "unsorted_rw"
    _make_unsorted_with_subdirs(source_root)
    snap = snapshot_source(str(source_root))

    # No files were touched.  Verify passes.
    assert verify_post(
        snap,
        source_root=str(source_root),
        output_root=str(tmp_path / "artifacts" / "organized-output" / "files"),
        output_mode="workspace",
    ) == []


def test_verify_post_workspace_flags_missing_source_file(tmp_path):
    """If a source file is deleted in workspace mode, the verify
    pass must flag the missing entry.
    """
    source_root = tmp_path / "unsorted_rw"
    _make_unsorted_with_subdirs(source_root)
    snap = snapshot_source(str(source_root))
    (source_root / "intro.txt").unlink()

    errors = verify_post(
        snap,
        source_root=str(source_root),
        output_root=str(tmp_path / "artifacts" / "organized-output" / "files"),
        output_mode="workspace",
    )
    assert any("intro.txt" in e for e in errors), (
        f"missing file should be reported; got {errors!r}"
    )


def test_verify_post_workspace_flags_unexpected_new_file(tmp_path):
    """If a new file appears in the source folder (workspace
    mode), the verify pass must flag it as an integrity
    violation.
    """
    source_root = tmp_path / "unsorted_rw"
    _make_unsorted_with_subdirs(source_root)
    snap = snapshot_source(str(source_root))
    _touch(source_root / "sneaky.txt", "I should not be here", mtime=1_700_000_777.0)

    errors = verify_post(
        snap,
        source_root=str(source_root),
        output_root=str(tmp_path / "artifacts" / "organized-output" / "files"),
        output_mode="workspace",
    )
    assert any("sneaky.txt" in e for e in errors), (
        f"unexpected file should be reported; got {errors!r}"
    )


def test_verify_post_inplace_passes_after_dimension_tool_move(tmp_path):
    """Inplace mode: the dimension tool moves every file into a
    bucket subdirectory under the source root.  The verify pass
    must walk the new layout recursively and confirm every
    pre-snapshot file is reachable with the same size+mtime.
    """
    source_root = tmp_path / "unsorted_rw"
    _make_unsorted_with_subdirs(source_root)

    # Take a snapshot, then run the dimension tool against the
    # same folder (inplace: source == output_root).
    snap = snapshot_source(str(source_root))
    result = OrganizeByTypeTool().execute_sync(
        source=str(source_root),
        output_root=str(source_root),
    )
    assert result.success, f"dimension tool failed: {result.error}"

    errors = verify_post(
        snap,
        source_root=str(source_root),
        output_root=str(source_root),
        output_mode="inplace",
    )
    assert errors == [], (
        f"inplace organize should leave every pre-snapshot file "
        f"reachable; got {errors!r}"
    )

    # And the empty subdirs (1/, 2/, 3/) are gone — the cleanup
    # is part of the dimension tool's audit pass.
    for stale in ("1", "2", "3"):
        assert not (source_root / stale).exists(), (
            f"empty subdir {stale!r}/ should have been cleaned up"
        )


def test_verify_post_inplace_detects_missing_file(tmp_path):
    """If a file disappears from the source/output root in
    inplace mode, the verify pass must flag it.
    """
    source_root = tmp_path / "unsorted_rw"
    _make_unsorted_with_subdirs(source_root)
    snap = snapshot_source(str(source_root))

    # Simulate an organize that moves everything but then drops
    # one file on the floor.
    OrganizeByTypeTool().execute_sync(
        source=str(source_root),
        output_root=str(source_root),
    )
    (source_root / "text" / "intro.txt").unlink(missing_ok=True)

    errors = verify_post(
        snap,
        source_root=str(source_root),
        output_root=str(source_root),
        output_mode="inplace",
    )
    assert any("intro.txt" in e for e in errors), (
        f"deleted bucket file should be flagged; got {errors!r}"
    )


# ---------------------------------------------------------------------------
# 3. Cross-check operations-plan for delete actions
# ---------------------------------------------------------------------------


def test_check_operations_plan_no_deletes_flags_delete_action(tmp_path):
    """If the audit log records a ``delete_file`` action, the
    integrity module must surface it as a violation.
    """
    plan = tmp_path / "operations-plan.json"
    plan.write_text(
        json.dumps({"action": "delete_file", "dst": "/some/file.txt"}) + "\n",
        encoding="utf-8",
    )
    errors = check_operations_plan_no_deletes(str(plan))
    assert any("delete_file" in e for e in errors), (
        f"delete_file action should be flagged; got {errors!r}"
    )


def test_check_operations_plan_no_deletes_passes_for_move_only(tmp_path):
    """A log of only move_file / copy_file actions must pass."""
    plan = tmp_path / "operations-plan.json"
    plan.write_text(
        "\n".join(
            json.dumps({"action": "move_file", "src": "a", "dst": "b"})
            for _ in range(3)
        )
        + "\n",
        encoding="utf-8",
    )
    assert check_operations_plan_no_deletes(str(plan)) == []


# ---------------------------------------------------------------------------
# 4. End-to-end: the canonical LLM-driven executor (the
#    OrganizeByTypeTool that the office agent calls via
#    ``run_dimension_tool``) wires the integrity hooks the way a
#    user would see them.  This pins the contract at the executor
#    boundary, not just the helper.
# ---------------------------------------------------------------------------


def test_dimension_tool_inplace_cleans_empty_subdirs_and_records_audit(
    tmp_path,
):
    """The dimension tool (the canonical LLM-driven organize
    path) must:

    - move every pre-snapshot file into a bucket subdirectory,
    - rmdir the now-empty ``1/``, ``2/``, ``3/`` and ``notes/``
      subdirectories that the user complained about, and
    - record an ``audit_snapshot`` and ``integrity_verify`` in
      ``operations-plan.json`` so the run is auditable.

    The source layout matches the user's task report.
    """
    source_root = tmp_path / "unsorted_rw"
    _make_unsorted_with_subdirs(source_root)

    result = OrganizeByTypeTool().execute_sync(
        source=str(source_root),
        output_root=str(source_root),
    )
    assert result.success, f"dimension tool failed: {result.error}"

    # The empty subdirs are gone.
    for stale in ("1", "2", "3", "notes"):
        assert not (source_root / stale).exists(), (
            f"inplace dimension tool must rmdir empty {stale!r}/"
        )

    # The bucket subdirectories now hold every file.
    for rel, expect in [
        ("text/intro.txt", "hello world from the top"),
        ("text/notes/idea.md", "# Idea note"),
        ("documents/1/invoice.pdf", "pdf-bytes-001"),
        ("images/2/photo.png", "png-bytes-002"),
        ("data/3/data.csv", "a,b\n1,2\n"),
    ]:
        path = source_root / rel
        assert path.is_file(), f"expected {rel} in bucket after organize"
        assert path.read_text(encoding="utf-8") == expect, (
            f"{rel} content drifted across the move"
        )

    # operations-plan.json records the audit snapshot, the moves,
    # and the post-run verify verdict.  The dimension tool writes
    # it next to the source root (its ``output_root`` is the
    # source in inplace mode, so ``os.path.dirname(output_root)``
    # is the source's parent).
    plan_path = source_root.parent / "operations-plan.json"
    assert plan_path.exists(), (
        "operations-plan.json must be written by the dimension tool"
    )
    actions: list[dict] = []
    with open(plan_path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                actions.append(json.loads(line))

    kinds = [a.get("action") for a in actions]
    assert "audit_snapshot" in kinds, (
        f"executor must record an audit_snapshot before moving; "
        f"actions: {kinds!r}"
    )
    assert "integrity_verify" in kinds, (
        f"executor must record a post-run integrity verdict; "
        f"got {kinds!r}"
    )
    # No delete_file actions are allowed — "only moving is allowed"
    # is the contract this audit log enforces.
    assert "delete_file" not in kinds, (
        f"organize must never record a delete_file action; "
        f"got {kinds!r}"
    )

    # The post-run verify is empty (success).
    verify_records = [a for a in actions if a.get("action") == "integrity_verify"]
    assert verify_records, "integrity_verify record missing"
    assert verify_records[0].get("errors") == [], (
        f"successful organize should produce zero integrity errors; "
        f"got: {verify_records[0].get('errors')!r}"
    )

    # And the audit_snapshot includes every pre-move file with
    # its (rel, size, mtime) triple.
    snap_records = [a for a in actions if a.get("action") == "audit_snapshot"]
    snap_files = snap_records[0].get("files") or []
    snap_rels = {f["rel"] for f in snap_files}
    assert snap_rels == {
        "intro.txt",
        "notes/idea.md",
        "1/invoice.pdf",
        "2/photo.png",
        "3/data.csv",
    }, f"snapshot rels drifted; got {snap_rels!r}"


def test_bounded_folder_organize_records_audit_and_integrity_verdict(
    tmp_path,
):
    """The bounded-folder fallback (the other executor path) must
    record the same audit_snapshot / integrity_verify / no-deletes
    pair as the dimension tool, even when the move loop is a
    structural no-op (the inventory's ``relative_path`` falls back
    to ``<output_root>/<rel>`` for this fallback).  The audit
    record is what makes the run auditable.
    """
    source_root = tmp_path / "unsorted_rw"
    _make_unsorted_with_subdirs(source_root)
    artifacts_dir = tmp_path / "artifacts"

    result = _run_bounded_folder_organize(
        [str(source_root)],
        output_mode="inplace",
        artifacts_dir=str(artifacts_dir),
    )
    assert result.success, f"bounded-folder organize failed: {result.summary}"

    plan_path = artifacts_dir / "operations-plan.json"
    assert plan_path.exists(), (
        "operations-plan.json must be written by the executor"
    )
    actions: list[dict] = []
    with open(plan_path, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                actions.append(json.loads(line))

    kinds = [a.get("action") for a in actions]
    assert "audit_snapshot" in kinds, (
        f"executor must record an audit_snapshot before any move; "
        f"actions: {kinds!r}"
    )
    assert "integrity_verify" in kinds, (
        f"executor must record a post-run integrity verdict; "
        f"got {kinds!r}"
    )
    # No delete_file actions allowed in a healthy organize run.
    assert "delete_file" not in kinds, (
        f"organize must never record a delete_file action; "
        f"got {kinds!r}"
    )

    # The post-run verify is empty (success).
    verify_records = [a for a in actions if a.get("action") == "integrity_verify"]
    assert verify_records, "integrity_verify record missing"
    assert verify_records[0].get("errors") == [], (
        f"successful bounded-folder organize should produce zero "
        f"integrity errors; got: {verify_records[0].get('errors')!r}"
    )
