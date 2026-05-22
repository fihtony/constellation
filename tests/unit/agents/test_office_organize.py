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


def test_organize_folder_tool_returns_recursive_file_metadata(tmp_path):
    """OrganizeFolderTool returns recursive per-file metadata for nested content."""
    tool = OrganizeFolderTool()
    source_root = tmp_path / "2026"
    essay_dir = source_root / "0103"
    essay_dir.mkdir(parents=True)
    essay_path = essay_dir / "1.txt"
    essay_path.write_text(
        "\n\n>>> Student Yan\n\nThe Most Important Discovery\n",
        encoding="utf-8",
    )

    result = tool.execute_sync(path=str(source_root))

    assert result.success, f"organize_folder failed: {result.error}"
    data = json.loads(result.output)
    files = data.get("files", [])
    assert len(files) == 1
    assert files[0]["relative_path"] == "0103/1.txt"
    assert files[0]["suggested_reader_tool"] == "read_txt"
    assert files[0]["inferred_date_bucket"] == "2026-01"
    assert files[0]["primary_entity"] == "Yan"
    assert "Student Yan" in "\n".join(files[0]["prominent_headings"])


def test_organize_folder_tool_counts_all_nested_files(tmp_path):
    """OrganizeFolderTool recurses through the full folder tree."""
    tool = OrganizeFolderTool()
    (tmp_path / "0103").mkdir()
    (tmp_path / "0207").mkdir()
    (tmp_path / "0103" / "1.txt").write_text(">>> Student Ethan", encoding="utf-8")
    (tmp_path / "0207" / "2.txt").write_text(">>> Student Liam", encoding="utf-8")

    result = tool.execute_sync(path=str(tmp_path))

    assert result.success, f"organize_folder failed: {result.error}"
    data = json.loads(result.output)
    assert data.get("total_files") == 2
    relative_paths = {item["relative_path"] for item in data.get("files", [])}
    assert relative_paths == {"0103/1.txt", "0207/2.txt"}


def test_organize_folder_tool_uses_explicit_identity_not_assignment_title(tmp_path):
    """OrganizeFolderTool should prefer explicit identity markers over document titles."""
    tool = OrganizeFolderTool()
    essay_dir = tmp_path / "0131"
    essay_dir.mkdir(parents=True)
    (essay_dir / "1.txt").write_text(
        "Formal Letter Writing\n\n>>> Student Ethan\n\nFebruary 1, 2026\n",
        encoding="utf-8",
    )

    result = tool.execute_sync(path=str(tmp_path))

    assert result.success, f"organize_folder failed: {result.error}"
    data = json.loads(result.output)
    file_entry = data["files"][0]
    assert file_entry["primary_entity"] == "Ethan"
    assert file_entry["primary_entity_confidence"] == "high"
