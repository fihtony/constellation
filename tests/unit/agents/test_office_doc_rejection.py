"""Unit tests for .doc format rejection in ReadDocxTool."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from agents.office.office_tools import ReadDocxTool


class TestReadDocxToolDocRejection:
    """Tests for explicit .doc format rejection."""

    def test_doc_extension_rejected_with_clear_message(self, tmp_path):
        """Legacy .doc files are rejected with helpful error message."""
        tool = ReadDocxTool()

        # Create a fake .doc file
        doc_file = tmp_path / "legacy_document.doc"
        doc_file.write_bytes(b"FAKE_DOC_FILE")

        result = tool.execute_sync(path=str(doc_file))

        assert result.success is False
        assert "Legacy .doc format is not supported" in result.error or ".doc" in result.error

    def test_docx_extension_accepted(self, tmp_path):
        """Valid .docx files are not rejected at the extension check."""
        tool = ReadDocxTool()

        # Create a fake .docx file
        docx_file = tmp_path / "valid_document.docx"
        # Write minimal ZIP header (docx files are ZIP archives)
        docx_file.write_bytes(b"PK\x03\x04 fake docx content")

        result = tool.execute_sync(path=str(docx_file))

        # Should fail for other reasons (not valid docx), but not because of extension
        # The error should NOT be about .doc format rejection
        if not result.success:
            assert ".doc format is not supported" not in result.error

    def test_non_word_file_rejected(self, tmp_path):
        """Non-Word files are rejected with appropriate message."""
        tool = ReadDocxTool()

        txt_file = tmp_path / "readme.txt"
        txt_file.write_text("plain text content")

        result = tool.execute_sync(path=str(txt_file))

        assert result.success is False
        assert "Not a Word document" in result.error or "not a DOCX" in result.error

    def test_uppercase_doc_extension_rejected(self, tmp_path):
        """Uppercase .DOC extension is also rejected."""
        tool = ReadDocxTool()

        doc_file = tmp_path / "legacy_document.DOC"
        doc_file.write_bytes(b"FAKE_DOC_FILE")

        result = tool.execute_sync(path=str(doc_file))

        assert result.success is False
        assert ".doc format is not supported" in result.error