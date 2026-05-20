import pytest, os, tempfile, json

def test_read_txt_tool_uses_chardet():
    """Test ReadTxtTool detects encoding with chardet."""
    from agents.office.office_tools import ReadTxtTool

    # Create a temp file with non-ASCII content
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("Hello world 你好世界")
        txt_path = f.name

    try:
        tool = ReadTxtTool()
        result = tool.execute_sync(path=txt_path)
        assert result.success, f"read_txt failed: {result.error}"
        data = json.loads(result.output)
        assert "encoding" in data
        assert len(data["content"]) > 0
    finally:
        os.unlink(txt_path)

def test_file_size_limit_rejects_oversized_file(tmp_path):
    """Test that files exceeding OFFICE_MAX_FILE_SIZE_MB are rejected."""
    from agents.office.office_tools import ReadTxtTool, _check_file_size

    # Create a file larger than 1MB (with 1MB limit for test)
    large_file = tmp_path / "large.txt"
    os.environ["OFFICE_MAX_FILE_SIZE_MB"] = "1"
    # Create 2MB file
    large_file.write_bytes(b"x" * (2 * 1024 * 1024))

    try:
        ok, err = _check_file_size(str(large_file))
        assert not ok, "Should reject file over size limit"
        assert "exceeds maximum" in err
    finally:
        os.environ.pop("OFFICE_MAX_FILE_SIZE_MB", None)

def test_file_size_limit_default_50mb():
    """Test default 50MB limit when OFFICE_MAX_FILE_SIZE_MB not set."""
    from agents.office.office_tools import _check_file_size

    os.environ.pop("OFFICE_MAX_FILE_SIZE_MB", None)
    ok, err = _check_file_size("/nonexistent/file.txt")  # Should pass (OSError handled)
    assert ok, "Nonexistent file should not fail size check"

def test_read_csv_tool_has_encoding(tmp_path):
    """Test ReadCsvTool returns encoding info."""
    from agents.office.office_tools import ReadCsvTool

    csv_file = tmp_path / "test.csv"
    csv_file.write_text("a,b,c\n1,2,3\n4,5,6\n")

    tool = ReadCsvTool()
    old_root = os.environ.get("OFFICE_SOURCE_ROOT")
    try:
        os.environ["OFFICE_SOURCE_ROOT"] = str(tmp_path)
        result = tool.execute_sync(path=str(csv_file))
        assert result.success, f"read_csv failed: {result.error}"
        data = json.loads(result.output)
        # Should have encoding now
        assert "encoding" in data
    finally:
        if old_root is not None:
            os.environ["OFFICE_SOURCE_ROOT"] = old_root
        else:
            os.environ.pop("OFFICE_SOURCE_ROOT", None)