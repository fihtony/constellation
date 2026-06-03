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
    (root / "files" / "doc.pdf").write_text("x", encoding="utf-8")
    with pytest.raises(PathSafetyError) as excinfo:
        resolve_within_root(str(root), "files\\doc.pdf")
    assert "backslash" in str(excinfo.value).lower()


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
