"""Unit tests for PDF extraction metadata in ReadPdfTool."""

from __future__ import annotations

import json
from pathlib import Path

from agents.office.office_tools import ReadPdfTool


PROJECT_ROOT = Path(__file__).resolve().parents[3]


def test_pdf_with_no_extractable_text_reports_metadata():
    """Scanned/image-based PDFs should report missing extractable text without OCR."""
    tool = ReadPdfTool()
    pdf_path = PROJECT_ROOT / "tests" / "data" / "stlouis" / "Journal_CSL10.pdf"

    result = tool.execute_sync(path=str(pdf_path))

    assert result.success is True, result.error
    payload = json.loads(result.output)
    assert payload["total_pages"] > 0
    assert payload["extractable_text"] is False
    assert payload["content"] == ""
    assert payload["extraction_method"] == "none"
