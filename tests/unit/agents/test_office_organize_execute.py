import pytest, os, tempfile, json

def test_organize_move_file_rejects_without_write_grant():
    """Test that organize_move_file requires OFFICE_ALLOW_INPLACE_WRITES=true."""
    from agents.office.office_tools import OrganizeMoveFileTool

    tool = OrganizeMoveFileTool()
    os.environ.pop("OFFICE_ALLOW_INPLACE_WRITES", None)  # Ensure not set
    os.environ["OFFICE_OUTPUT_MODE"] = "inplace"
    os.environ["OFFICE_SOURCE_ROOT"] = "/tmp"
    result = tool.execute_sync(action="mkdir", dst="/tmp/test_dir")
    assert not result.success
    assert "inplace writes not enabled" in result.error
    os.environ.pop("OFFICE_OUTPUT_MODE", None)
    os.environ.pop("OFFICE_SOURCE_ROOT", None)

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
    os.environ["OFFICE_OUTPUT_MODE"] = "inplace"
    os.environ["OFFICE_SOURCE_ROOT"] = str(tmp_path)
    os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)

    target_dir = tmp_path / "organized-output" / "files" / "bucket_a" / "asset_1"
    result = tool.execute_sync(action="mkdir", dst=str(target_dir))

    assert result.success, f"mkdir failed: {result.error}"
    assert target_dir.exists()

    os.environ.pop("OFFICE_ALLOW_INPLACE_WRITES", None)
    os.environ.pop("OFFICE_OUTPUT_MODE", None)
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
    os.environ["OFFICE_OUTPUT_MODE"] = "inplace"
    os.environ["OFFICE_SOURCE_ROOT"] = str(tmp_path)
    os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)

    dst_file = tmp_path / "organized-output" / "files" / "bucket_a" / "asset_1" / "source.txt"
    result = tool.execute_sync(action="copy_file", src=str(src_file), dst=str(dst_file))

    assert result.success, f"copy_file failed: {result.error}"
    assert dst_file.exists()
    assert dst_file.read_text() == "hello world"

    os.environ.pop("OFFICE_ALLOW_INPLACE_WRITES", None)
    os.environ.pop("OFFICE_OUTPUT_MODE", None)
    os.environ.pop("OFFICE_SOURCE_ROOT", None)
    os.environ.pop("OFFICE_WORKSPACE_ROOT", None)

def test_organize_move_file_write_text(tmp_path):
    """Test write_text action writes content."""
    from agents.office.office_tools import OrganizeMoveFileTool

    tool = OrganizeMoveFileTool()
    os.environ["OFFICE_ALLOW_INPLACE_WRITES"] = "true"
    os.environ["OFFICE_OUTPUT_MODE"] = "inplace"
    os.environ["OFFICE_SOURCE_ROOT"] = str(tmp_path)
    os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)

    target = tmp_path / "organized-output" / "files" / "documents" / "output.txt"
    result = tool.execute_sync(
        action="write_text",
        dst=str(target),
        content="organized content\nline 2"
    )

    assert result.success, f"write_text failed: {result.error}"
    assert target.exists()
    assert target.read_text() == "organized content\nline 2"

    os.environ.pop("OFFICE_ALLOW_INPLACE_WRITES", None)
    os.environ.pop("OFFICE_OUTPUT_MODE", None)
    os.environ.pop("OFFICE_SOURCE_ROOT", None)
    os.environ.pop("OFFICE_WORKSPACE_ROOT", None)


def test_organize_move_file_no_longer_blocks_on_identity_metadata(tmp_path):
    """Block 2 dropped the high-confidence identity/date destination check.

    The destination contract is now enforced by the plan-output gate
    and the dimension tool. This test pins the new contract: an
    arbitrary organized-output destination succeeds.
    """
    from agents.office.office_tools import OrganizeMoveFileTool

    source_dir = tmp_path / "2026" / "0103"
    source_dir.mkdir(parents=True)
    src_file = source_dir / "1.txt"
    src_file.write_text(">>> Student Yan\n", encoding="utf-8")

    tool = OrganizeMoveFileTool()
    os.environ["OFFICE_ALLOW_INPLACE_WRITES"] = "true"
    os.environ["OFFICE_OUTPUT_MODE"] = "workspace"
    os.environ["OFFICE_SOURCE_ROOT"] = str(tmp_path / "2026")
    os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path / "workspace")

    result = tool.execute_sync(action="copy_file", src=str(src_file), dst="Ethan/2026-01/0103-1.txt")

    # The destination gets the organized-output prefix prepended and is
    # accepted regardless of the source's primary entity / date metadata.
    assert result.success, result.error

    os.environ.pop("OFFICE_ALLOW_INPLACE_WRITES", None)
    os.environ.pop("OFFICE_OUTPUT_MODE", None)
    os.environ.pop("OFFICE_SOURCE_ROOT", None)
    os.environ.pop("OFFICE_WORKSPACE_ROOT", None)


def test_organize_move_file_rejects_duplicate_successful_copy(tmp_path):
    """A source file should only be copied once per organize task."""
    from agents.office.office_tools import OrganizeMoveFileTool

    source_dir = tmp_path / "2026" / "0214"
    source_dir.mkdir(parents=True)
    src_file = source_dir / "2.txt"
    src_file.write_text(">>> Student Liam\n", encoding="utf-8")

    tool = OrganizeMoveFileTool()
    os.environ["OFFICE_ALLOW_INPLACE_WRITES"] = "true"
    os.environ["OFFICE_OUTPUT_MODE"] = "workspace"
    os.environ["OFFICE_SOURCE_ROOT"] = str(tmp_path / "2026")
    os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path / "workspace")

    first = tool.execute_sync(action="copy_file", src=str(src_file), dst="Liam/2026-02/0214-2.txt")
    second = tool.execute_sync(action="copy_file", src=str(src_file), dst="Liam/2026-02/0214-2.txt")

    assert first.success, first.error
    assert not second.success
    assert "already copied successfully" in second.error

    os.environ.pop("OFFICE_ALLOW_INPLACE_WRITES", None)
    os.environ.pop("OFFICE_OUTPUT_MODE", None)
    os.environ.pop("OFFICE_SOURCE_ROOT", None)
    os.environ.pop("OFFICE_WORKSPACE_ROOT", None)


def test_organize_move_file_uses_allowed_base_for_expected_filename(tmp_path):
    """High-confidence filename checks should use the narrowest authorized base path."""
    from agents.office.office_tools import OrganizeMoveFileTool

    source_dir = tmp_path / "mount-root" / "2026" / "0103"
    source_dir.mkdir(parents=True)
    src_file = source_dir / "1.txt"
    src_file.write_text(">>> Student Yan\n", encoding="utf-8")

    tool = OrganizeMoveFileTool()
    os.environ["OFFICE_ALLOW_INPLACE_WRITES"] = "true"
    os.environ["OFFICE_OUTPUT_MODE"] = "workspace"
    os.environ["OFFICE_SOURCE_ROOT"] = str(tmp_path / "mount-root")
    os.environ["OFFICE_ALLOWED_BASE_PATHS"] = str(tmp_path / "mount-root" / "2026")
    os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path / "workspace")

    result = tool.execute_sync(action="copy_file", src=str(src_file), dst="Yan/2026-01/0103-1.txt")

    assert result.success, result.error

    os.environ.pop("OFFICE_ALLOW_INPLACE_WRITES", None)
    os.environ.pop("OFFICE_OUTPUT_MODE", None)
    os.environ.pop("OFFICE_SOURCE_ROOT", None)
    os.environ.pop("OFFICE_ALLOWED_BASE_PATHS", None)
    os.environ.pop("OFFICE_WORKSPACE_ROOT", None)
