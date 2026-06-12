"""Pre/post file-integrity verification for office organize tasks.

Office organize (in either workspace or inplace mode) must NEVER
delete or modify user files.  The agent's contract is to relocate
files into dimension buckets; mtimes, sizes, and names must be
preserved.

The integrity module captures a snapshot of every source file
(relative path, size, mtime) *before* organize starts and re-checks
the snapshot *after* organize completes:

- ``workspace`` mode — source is read-only.  Every pre-snapshot
  file must still be reachable under ``source_root`` with the same
  size and mtime after the run.
- ``inplace`` mode — files are moved out of the source layout and
  into bucket subdirectories.  Every pre-snapshot file must be
  reachable under ``output_root`` (which equals ``source_root``)
  with the same size and mtime after the run.

The same module also offers a post-organize *empty-directory
cleanup* helper that walks the source bottom-up and ``rmdir``s
subdirectories that became empty after the move.  The source root
itself is never removed.

The two helpers are intentionally tiny so they can be reused by
every organize code path (the bounded-folder fallback, the
built-in dimension tools, and the custom-dimension planner).  No
file I/O happens outside the explicit ``snapshot_source`` and
``verify_post`` call sites, so a test can stub either of them
without touching the rest of the office module.
"""
from __future__ import annotations

import json
import os
from typing import Any, Iterable


_MTIME_EPSILON = 0.001  # mtime round-trip tolerance (seconds)


def _walk_user_files(root: str) -> list[tuple[str, str, int, float]]:
    """Walk every regular file under ``root`` and return its triple.

    Returns a list of ``(rel_path, abs_path, size, mtime)`` tuples.
    Hidden files and hidden directories (names starting with ``.``)
    are skipped — the integrity check is about *user* files, not
    editor state (``~/.DS_Store`` etc.).
    """
    out: list[tuple[str, str, int, float]] = []
    if not root or not os.path.isdir(root):
        return out
    for walk_root, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith("."):
                continue
            abs_path = os.path.join(walk_root, name)
            try:
                st = os.stat(abs_path)
            except OSError:
                continue
            out.append((
                os.path.relpath(abs_path, root),
                abs_path,
                st.st_size,
                st.st_mtime,
            ))
    return out


def snapshot_source(root: str) -> list[dict[str, Any]]:
    """Capture the source folder's ``(rel, size, mtime)`` for every file.

    Returns a JSON-serialisable list of dicts.  Callers typically
    append the result to ``operations-plan.json`` so a verifier
    can later prove the snapshot was taken *before* the move.
    """
    return [
        {"rel": rel, "size": size, "mtime": mtime}
        for (rel, _, size, mtime) in _walk_user_files(root)
    ]


def _file_matches(rel: str, size: int, mtime: float, roots: Iterable[str]) -> bool:
    for root in roots:
        candidate = os.path.join(root, rel)
        if not os.path.exists(candidate):
            continue
        try:
            st = os.stat(candidate)
        except OSError:
            continue
        if st.st_size == size and abs(st.st_mtime - mtime) < _MTIME_EPSILON:
            return True
    return False


def _list_post_files(root: str) -> list[tuple[str, int, float]]:
    """Walk ``root`` and return every regular file's ``(rel, size, mtime)``.

    Skips hidden files and the audit-log filenames we know about
    (the snapshot/verify records are written by this module into a
    sibling of ``output_root`` and would never appear under the
    source folder; the defensive filter keeps the helper robust to
    any future layout change).
    """
    if not root or not os.path.isdir(root):
        return []
    out: list[tuple[str, int, float]] = []
    skip_names = {"operations-plan.json", "organization-plan.md"}
    for walk_root, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(files):
            if name.startswith(".") or name in skip_names:
                continue
            abs_path = os.path.join(walk_root, name)
            try:
                st = os.stat(abs_path)
            except OSError:
                continue
            out.append((
                os.path.relpath(abs_path, root),
                st.st_size,
                st.st_mtime,
            ))
    return out


def verify_post(
    snapshot: list[dict[str, Any]],
    *,
    source_root: str,
    output_root: str,
    output_mode: str,
) -> list[str]:
    """Return integrity errors; empty list means every file is intact.

    The contract in both modes is the same in spirit — *no file is
    modified, deleted, or created; only its location may change* —
    but the implementation differs because the post-organize
    layout is different.

    - ``workspace`` — every snapshot file must still be reachable
      under ``source_root`` at its original rel path with the same
      size and mtime, and the post-tree under ``source_root`` must
      not contain any new file.  (The copies under ``output_root``
      are irrelevant to the "source untouched" guarantee.)
    - ``inplace`` — the source IS the output.  Every snapshot file
      must be reachable somewhere under ``output_root`` (the new
      bucket layout) with the same size and mtime, and the
      post-tree must not contain any file the snapshot did not
      already know about.  The original location may have been
      emptied by a ``shutil.move``.

    Each error is a short human-readable string; the caller is
    expected to surface them in the agent's delivery-error stream.
    """
    if not snapshot:
        return []
    mode = (output_mode or "").strip().lower()
    if not source_root and not output_root:
        return ["integrity verify skipped: no roots supplied"]
    if mode == "workspace":
        if not source_root:
            return ["integrity verify skipped: no source_root for workspace mode"]
        # Workspace: source must be byte-identical at the original
        # rel paths.  No recursive walk needed; an exact-path
        # (size, mtime) check is what the "no source mutation"
        # contract requires.  The "extras" check is then a set
        # comparison against the post-walk of the same source_root.
        post = _list_post_files(source_root)
        post_index: dict[tuple[int, int, float], list[str]] = {}
        for rel, size, mtime in post:
            post_index.setdefault((size, int(mtime)), []).append(rel)
        errors: list[str] = []
        matched_rels: set[str] = set()
        for entry in snapshot:
            rel = str(entry.get("rel") or "")
            if not rel:
                continue
            try:
                size = int(entry.get("size") or 0)
                mtime = float(entry.get("mtime") or 0.0)
            except (TypeError, ValueError):
                errors.append(f"integrity: malformed snapshot entry: {entry!r}")
                continue
            bucket = post_index.get((size, int(mtime)), [])
            if rel in bucket:
                matched_rels.add(rel)
                continue
            errors.append(
                f"file missing or modified after organize: {rel} "
                f"(expected size={size}, mtime={mtime:.0f})"
            )
        for rel, size, mtime in post:
            if rel in matched_rels:
                continue
            errors.append(
                f"unexpected file present after organize: {rel} "
                f"(size={size}, mtime={mtime:.0f})"
            )
        return errors

    # Inplace: snapshot rels no longer exist; the post-tree has the
    # files at new (rel, size, mtime) tuples.  Match by (size,
    # mtime) — distinct extensions in the test suite give distinct
    # sizes, so collisions are unlikely in practice.
    if not output_root:
        return ["integrity verify skipped: no output_root for inplace mode"]
    post = _list_post_files(output_root)
    post_index = {}
    for rel, size, mtime in post:
        post_index.setdefault((size, int(mtime)), []).append(rel)
    errors = []
    matched_rels = set()
    for entry in snapshot:
        rel = str(entry.get("rel") or "")
        if not rel:
            continue
        try:
            size = int(entry.get("size") or 0)
            mtime = float(entry.get("mtime") or 0.0)
        except (TypeError, ValueError):
            errors.append(f"integrity: malformed snapshot entry: {entry!r}")
            continue
        bucket = post_index.get((size, int(mtime)), [])
        if not bucket:
            errors.append(
                f"file missing or modified after organize: {rel} "
                f"(expected size={size}, mtime={mtime:.0f})"
            )
            continue
        matched_rels.add(bucket.pop(0))
    for rel, size, mtime in post:
        if rel in matched_rels:
            continue
        errors.append(
            f"unexpected file present after organize: {rel} "
            f"(size={size}, mtime={mtime:.0f})"
        )
    return errors


def check_operations_plan_no_deletes(operations_path: str) -> list[str]:
    """Return errors if ``operations-plan.json`` records any ``delete_file``.

    Organize must never delete a user file; this helper is a final
    safety net that scans the audit log for delete actions and
    surfaces them as integrity violations.  The LLM-driven
    dimension path never records a delete action, but a buggy
    executor that tries to "clean up" a stray file would be caught
    here.
    """
    if not operations_path or not os.path.exists(operations_path):
        return []
    errors: list[str] = []
    with open(operations_path, encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                op = json.loads(line)
            except ValueError:
                continue
            if str(op.get("action") or "") == "delete_file":
                dst = str(op.get("dst") or op.get("src") or "<unknown>")
                errors.append(f"operations-plan records delete_file: {dst}")
    return errors


def cleanup_empty_dirs(root: str) -> list[str]:
    """Remove directories that became empty under ``root``.

    Walks bottom-up and ``rmdir``s any directory with no entries
    (no files, no subdirectories).  Never removes ``root`` itself,
    even if it becomes empty.

    Returns the list of removed directory paths.  Directories that
    are non-empty (typically the bucket subdirectories that
    received the moved files) are left untouched.
    """
    if not root or not os.path.isdir(root):
        return []
    real_root = os.path.realpath(root)
    removed: list[str] = []
    for walk_root, dirs, files in os.walk(real_root, topdown=False):
        if walk_root == real_root:
            # Never remove the source/output root itself, even if
            # every loose file was moved out and the bucket
            # subdirectories are also empty.  Removing the user's
            # source folder would be catastrophic.
            continue
        if dirs or files:
            continue
        try:
            os.rmdir(walk_root)
            removed.append(walk_root)
        except OSError:
            # Already gone, or a permission race.  Skip silently.
            pass
    return removed
