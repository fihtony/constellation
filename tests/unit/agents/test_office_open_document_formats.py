"""Unit tests for OpenDocument and Office-variant support in office tools."""

from __future__ import annotations

import json
import zipfile

from agents.office.office_tools import ReadDocxTool, ReadPptxTool, ReadXlsxTool


ODF_NAMESPACES = (
    'xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
    'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
    'xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"'
)


def test_read_docx_supports_odt(tmp_path):
    """ODT files should be readable through the generic word-document reader."""
    odt_file = tmp_path / "notes.odt"
    with zipfile.ZipFile(odt_file, "w") as archive:
        archive.writestr(
            "content.xml",
            (
                f'<office:document-content {ODF_NAMESPACES}>'
                "<office:body><office:text>"
                "<text:h>Family Update</text:h>"
                "<text:p>Students should bring their library books.</text:p>"
                "</office:text></office:body></office:document-content>"
            ),
        )

    result = ReadDocxTool().execute_sync(path=str(odt_file))

    assert result.success is True, result.error
    payload = json.loads(result.output)
    assert "Family Update" in payload["content"]
    assert "library books" in payload["content"]
    assert payload["extraction_method"] == "odt-zip-xml"


def test_read_pptx_supports_odp(tmp_path):
    """ODP files should be readable through the generic presentation reader."""
    odp_file = tmp_path / "briefing.odp"
    with zipfile.ZipFile(odp_file, "w") as archive:
        archive.writestr(
            "content.xml",
            (
                f'<office:document-content {ODF_NAMESPACES}>'
                "<office:body><office:presentation>"
                "<text:h>Quarterly Review</text:h>"
                "<text:p>Enrollment is trending upward.</text:p>"
                "</office:presentation></office:body></office:document-content>"
            ),
        )

    result = ReadPptxTool().execute_sync(path=str(odp_file))

    assert result.success is True, result.error
    payload = json.loads(result.output)
    assert "Quarterly Review" in payload["content"]
    assert "Enrollment is trending upward." in payload["content"]
    assert payload["extraction_method"] == "odp-zip-xml"


def test_read_xlsx_supports_ods(tmp_path):
    """ODS files should be readable through the generic spreadsheet reader."""
    ods_file = tmp_path / "scores.ods"
    with zipfile.ZipFile(ods_file, "w") as archive:
        archive.writestr(
            "content.xml",
            (
                f'<office:document-content {ODF_NAMESPACES}>'
                "<office:body><office:spreadsheet>"
                '<table:table table:name="Sheet1">'
                "<table:table-row>"
                '<table:table-cell office:value-type="string"><text:p>Name</text:p></table:table-cell>'
                '<table:table-cell office:value-type="string"><text:p>Score</text:p></table:table-cell>'
                "</table:table-row>"
                "<table:table-row>"
                '<table:table-cell office:value-type="string"><text:p>Avery</text:p></table:table-cell>'
                '<table:table-cell office:value-type="float"><text:p>92</text:p></table:table-cell>'
                "</table:table-row>"
                "</table:table>"
                "</office:spreadsheet></office:body></office:document-content>"
            ),
        )

    result = ReadXlsxTool().execute_sync(path=str(ods_file))

    assert result.success is True, result.error
    payload = json.loads(result.output)
    assert payload["sheet_names"] == ["Sheet1"]
    assert payload["sheets"]["Sheet1"]["headers"] == ["Name", "Score"]
    assert payload["sheets"]["Sheet1"]["sample_rows"][0]["Name"] == "Avery"
    assert payload["extraction_method"] == "ods-zip-xml"
