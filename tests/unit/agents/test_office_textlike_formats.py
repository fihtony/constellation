"""Unit tests for text-like document formats handled by ReadTxtTool."""

from __future__ import annotations

import json

from agents.office.office_tools import ReadTxtTool


def test_read_txt_extracts_text_from_html(tmp_path):
    """HTML files should be converted into readable text."""
    tool = ReadTxtTool()
    html_file = tmp_path / "sample.html"
    html_file.write_text(
        "<html><body><h1>Policy</h1><p>Hello <b>world</b>.</p></body></html>",
        encoding="utf-8",
    )

    result = tool.execute_sync(path=str(html_file))

    assert result.success is True, result.error
    payload = json.loads(result.output)
    assert "Policy" in payload["content"]
    assert "Hello" in payload["content"]
    assert "<html>" not in payload["content"]


def test_read_txt_extracts_text_from_xml(tmp_path):
    """XML files should expose readable text content."""
    tool = ReadTxtTool()
    xml_file = tmp_path / "sample.xml"
    xml_file.write_text(
        "<root><title>Schedule</title><item>Assembly</item><item>Library</item></root>",
        encoding="utf-8",
    )

    result = tool.execute_sync(path=str(xml_file))

    assert result.success is True, result.error
    payload = json.loads(result.output)
    assert "Schedule" in payload["content"]
    assert "Assembly" in payload["content"]
    assert "<root>" not in payload["content"]


def test_read_txt_prettifies_json(tmp_path):
    """JSON files should be exposed as readable structured text."""
    tool = ReadTxtTool()
    json_file = tmp_path / "sample.json"
    json_file.write_text('{"student":"Avery","scores":[8,9],"active":true}', encoding="utf-8")

    result = tool.execute_sync(path=str(json_file))

    assert result.success is True, result.error
    payload = json.loads(result.output)
    assert '"student": "Avery"' in payload["content"]
    assert payload["extraction_method"] == "json-pretty"


def test_read_txt_preserves_log_content(tmp_path):
    """Log files should remain readable plain text."""
    tool = ReadTxtTool()
    log_file = tmp_path / "sample.log"
    log_file.write_text("2026-05-22 INFO office agent started\n2026-05-22 WARN missing slide text\n", encoding="utf-8")

    result = tool.execute_sync(path=str(log_file))

    assert result.success is True, result.error
    payload = json.loads(result.output)
    assert "INFO office agent started" in payload["content"]
    assert payload["extraction_method"] == "plain-text"
