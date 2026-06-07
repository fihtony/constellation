import pytest, os, tempfile, json
from agents.office.office_tools import OrganizeFolderTool, WriteWorkspaceTool
from agents.office import nodes as office_nodes

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


def test_repair_missing_organize_plan_output_writes_plan_from_raw_output(tmp_path):
    """Organize delivery repair should persist a missing plan from raw model output."""
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    artifacts_dir = tmp_path / "workspace"
    artifacts_dir.mkdir()

    repaired = office_nodes._repair_missing_organize_plan_output(
        validated_paths=[str(source_dir)],
        output_mode="workspace",
        artifacts_dir=str(artifacts_dir),
        raw_output=(
            "Now let me write the organization plan documenting what was done.\n\n"
            "# Folder Organization Plan\n\n"
            "## Discovered Patterns\n"
            "- Grouped by student\n"
        ),
    )

    plan_path = artifacts_dir / "organization-plan.md"
    assert repaired == str(plan_path)
    assert plan_path.exists()
    plan_text = plan_path.read_text(encoding="utf-8")
    assert plan_text.startswith("# Folder Organization Plan")
    assert "Now let me write" not in plan_text


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
    # Business hardcode (primary_entity) was removed in Block 2; the
    # organise capability is now dimension-agnostic.
    assert "primary_entity" not in files[0]
    assert "The Most Important Discovery" in "\n".join(files[0]["prominent_headings"])


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


def test_organize_folder_tool_is_dimension_agnostic(tmp_path):
    """Block 2 removed explicit-identity inference; organize is dimension-agnostic."""
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
    # The organizer's job is to expose source metadata, not infer a
    # person/entity. Verify the business hardcode is gone but the
    # structural fields are still populated.
    assert "primary_entity" not in file_entry
    assert "primary_entity_confidence" not in file_entry
    assert file_entry["relative_path"] == "0131/1.txt"
    # The structural heading extractor is dimension-agnostic; verify
    # it still exposes content-derived headings when present.
    headings_blob = "\n".join(file_entry.get("prominent_headings") or [])
    assert "Formal Letter Writing" in headings_blob
