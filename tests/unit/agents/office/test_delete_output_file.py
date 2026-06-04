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
    assert outside.exists()


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


def test_delete_output_file_refuses_when_no_target_env_set(tmp_path, monkeypatch):
    """Fails closed when neither OFFICE_RESOLVED_TARGET_DIR nor OFFICE_WORKSPACE_ROOT is set."""
    monkeypatch.delenv("OFFICE_RESOLVED_TARGET_DIR", raising=False)
    monkeypatch.delenv("OFFICE_WORKSPACE_ROOT", raising=False)
    from agents.office.office_tools import DeleteOutputFileTool
    tool = DeleteOutputFileTool()
    result = tool.execute_sync(filename="stale.txt")
    assert result.error != ""
    assert "OFFICE_RESOLVED_TARGET_DIR" in result.error or "OFFICE_WORKSPACE_ROOT" in result.error


def test_delete_output_file_refuses_missing_file(workspace):
    """Returns an error when the target file does not exist."""
    from agents.office.office_tools import DeleteOutputFileTool
    tool = DeleteOutputFileTool()
    result = tool.execute_sync(filename="never-existed.txt")
    assert result.error != ""
    assert "does not exist" in result.error.lower()


def test_delete_output_file_refuses_directory(workspace):
    """Refuses to delete a directory (non-regular file)."""
    subdir = workspace / "subdir"
    subdir.mkdir()
    from agents.office.office_tools import DeleteOutputFileTool
    tool = DeleteOutputFileTool()
    result = tool.execute_sync(filename="subdir")
    assert result.error != ""
    assert "non-regular" in result.error.lower() or "directory" in result.error.lower()
    assert subdir.exists()
