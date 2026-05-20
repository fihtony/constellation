import pytest, os, tempfile, json
from agents.office.office_tools import OrganizeFolderTool, WriteWorkspaceTool

def test_organize_folder_tool_lists_files(tmp_path):
    """OrganizeFolderTool lists directory contents."""
    tool = OrganizeFolderTool()
    # Create test files
    (tmp_path / "doc1.txt").write_text("hello")
    (tmp_path / "doc2.pdf").write_text("world")
    (tmp_path / "subdir").mkdir()
    (tmp_path / "subdir" / "doc3.txt").write_text("nested")
    result = tool.execute_sync(path=str(tmp_path))
    assert result.success, f"organize_folder failed: {result.error}"
    data = json.loads(result.output)
    groups = data.get("groups", {})
    total_items = sum(len(files) for files in groups.values())
    assert total_items >= 3, f"Expected at least 3 items across groups, got {total_items}"


def test_organize_folder_tool_validates_path(tmp_path):
    """OrganizeFolderTool rejects paths outside source root."""
    tool = OrganizeFolderTool()
    os.environ["OFFICE_SOURCE_ROOT"] = str(tmp_path)
    result = tool.execute_sync(path="/etc/passwd")
    assert not result.success
    assert "outside OFFICE_SOURCE_ROOT" in result.error
    del os.environ["OFFICE_SOURCE_ROOT"]

def test_write_workspace_in_organize_mode(tmp_path):
    """WriteWorkspaceTool can write organization plan."""
    tool = WriteWorkspaceTool()
    os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)
    result = tool.execute_sync(
        filename="organization-plan.md",
        content="# Organization Plan\n\n- Group 1: Documents"
    )
    assert result.success, f"write_workspace failed: {result.error}"
    assert (tmp_path / "organization-plan.md").exists()
    del os.environ["OFFICE_WORKSPACE_ROOT"]


def test_organize_folder_tool_empty_dir(tmp_path):
    """OrganizeFolderTool handles empty directories."""
    tool = OrganizeFolderTool()
    tool._source_root = str(tmp_path)
    result = tool.execute_sync(path=str(tmp_path))
    assert result.success
    data = json.loads(result.output)
    assert data.get("total_files") == 0
    assert data.get("total_dirs") == 0
    assert data.get("groups") == {}