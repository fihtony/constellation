import pytest, os, tempfile, json

def test_organize_move_file_rejects_without_write_grant():
    """Test that organize_move_file requires OFFICE_ALLOW_INPLACE_WRITES=true."""
    from agents.office.office_tools import OrganizeMoveFileTool

    tool = OrganizeMoveFileTool()
    os.environ.pop("OFFICE_ALLOW_INPLACE_WRITES", None)  # Ensure not set
    result = tool.execute_sync(action="mkdir", dst="/tmp/test_dir")
    assert not result.success
    assert "inplace writes not enabled" in result.error

def test_organize_move_file_rejects_invalid_action():
    """Test that organize_move_file rejects actions not in whitelist."""
    from agents.office.office_tools import OrganizeMoveFileTool

    tool = OrganizeMoveFileTool()
    os.environ["OFFICE_ALLOW_INPLACE_WRITES"] = "true"
    # Mock source root to allow the path
    os.environ["OFFICE_SOURCE_ROOT"] = "/tmp"
    try:
        result = tool.execute_sync(action="delete_all", dst="/tmp/test")
        assert not result.success
        assert "not allowed" in result.error
    finally:
        os.environ.pop("OFFICE_ALLOW_INPLACE_WRITES", None)
        os.environ.pop("OFFICE_SOURCE_ROOT", None)

def test_organize_move_file_mkdir(tmp_path):
    """Test mkdir action creates directory."""
    from agents.office.office_tools import OrganizeMoveFileTool

    tool = OrganizeMoveFileTool()
    os.environ["OFFICE_ALLOW_INPLACE_WRITES"] = "true"
    os.environ["OFFICE_SOURCE_ROOT"] = str(tmp_path)
    os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)

    target_dir = tmp_path / "organized" / "subdir"
    result = tool.execute_sync(action="mkdir", dst=str(target_dir))

    assert result.success, f"mkdir failed: {result.error}"
    assert target_dir.exists()

    os.environ.pop("OFFICE_ALLOW_INPLACE_WRITES", None)
    os.environ.pop("OFFICE_SOURCE_ROOT", None)
    os.environ.pop("OFFICE_WORKSPACE_ROOT", None)

def test_organize_move_file_copy(tmp_path):
    """Test copy_file action copies file."""
    from agents.office.office_tools import OrganizeMoveFileTool

    # Create source file
    src_file = tmp_path / "source.txt"
    src_file.write_text("hello world")

    tool = OrganizeMoveFileTool()
    os.environ["OFFICE_ALLOW_INPLACE_WRITES"] = "true"
    os.environ["OFFICE_SOURCE_ROOT"] = str(tmp_path)
    os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)

    dst_file = tmp_path / "dest_copy.txt"
    result = tool.execute_sync(action="copy_file", src=str(src_file), dst=str(dst_file))

    assert result.success, f"copy_file failed: {result.error}"
    assert dst_file.exists()
    assert dst_file.read_text() == "hello world"

    os.environ.pop("OFFICE_ALLOW_INPLACE_WRITES", None)
    os.environ.pop("OFFICE_SOURCE_ROOT", None)
    os.environ.pop("OFFICE_WORKSPACE_ROOT", None)

def test_organize_move_file_write_text(tmp_path):
    """Test write_text action writes content."""
    from agents.office.office_tools import OrganizeMoveFileTool

    tool = OrganizeMoveFileTool()
    os.environ["OFFICE_ALLOW_INPLACE_WRITES"] = "true"
    os.environ["OFFICE_SOURCE_ROOT"] = str(tmp_path)
    os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)

    target = tmp_path / "output.txt"
    result = tool.execute_sync(
        action="write_text",
        dst=str(target),
        content="organized content\nline 2"
    )

    assert result.success, f"write_text failed: {result.error}"
    assert target.exists()
    assert target.read_text() == "organized content\nline 2"

    os.environ.pop("OFFICE_ALLOW_INPLACE_WRITES", None)
    os.environ.pop("OFFICE_SOURCE_ROOT", None)
    os.environ.pop("OFFICE_WORKSPACE_ROOT", None)