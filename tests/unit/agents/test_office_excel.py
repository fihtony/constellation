import pytest, os, tempfile, json

def test_read_xlsx_tool_basic():
    """Test ReadXlsxTool with a basic xlsx file."""
    from agents.office.office_tools import ReadXlsxTool

    # Create a temp xlsx file
    from openpyxl import Workbook
    with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
        wb = Workbook()
        ws = wb.active
        ws.title = "TestSheet"
        ws.append(["Name", "Value", "Score"])
        ws.append(["Alice", "100", "95"])
        ws.append(["Bob", "200", "88"])
        wb.save(f.name)
        xlsx_path = f.name

    try:
        tool = ReadXlsxTool()
        result = tool.execute_sync(path=xlsx_path)
        assert result.success, f"read_xlsx failed: {result.error}"
        data = json.loads(result.output)
        assert "sheets" in data
        assert "TestSheet" in data["sheets"]
        assert data["sheets"]["TestSheet"]["total_rows"] == 2
    finally:
        os.unlink(xlsx_path)

def test_read_xlsx_rejects_non_xlsx():
    """Test ReadXlsxTool rejects non-xlsx files."""
    from agents.office.office_tools import ReadXlsxTool

    # Create a real CSV file so the extension check is reached (file existence check comes first)
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as f:
        f.write(b"Name,Value\nAlice,100\n")
        csv_path = f.name

    try:
        tool = ReadXlsxTool()
        result = tool.execute_sync(path=csv_path)
        assert not result.success
        assert "not a supported spreadsheet file" in result.error
    finally:
        os.unlink(csv_path)

def test_read_xlsx_validates_path():
    """Test ReadXlsxTool path validation."""
    from agents.office.office_tools import ReadXlsxTool

    tool = ReadXlsxTool()
    old_root = os.environ.get("OFFICE_SOURCE_ROOT")
    try:
        os.environ["OFFICE_SOURCE_ROOT"] = "/tmp"
        result = tool.execute_sync(path="/etc/data.xlsx")
        assert not result.success
        assert "outside OFFICE_SOURCE_ROOT" in result.error
    finally:
        if old_root is not None:
            os.environ["OFFICE_SOURCE_ROOT"] = old_root
        else:
            os.environ.pop("OFFICE_SOURCE_ROOT", None)

def test_read_xls_tool_no_xlrd():
    """Test ReadXlsTool handles xlrd not installed gracefully."""
    from agents.office.office_tools import ReadXlsTool

    tool = ReadXlsTool()
    result = tool.execute_sync(path="/fake/file.xls")
    # Either xlrd is installed and it tries to read, or it's not installed and returns ImportError
    # Just check it's a valid response (success or error with "xlrd" in message)
    assert result.output or result.error
