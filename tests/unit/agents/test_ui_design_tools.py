"""Tests for UI Design boundary tool registration."""

import json


def test_ui_design_fine_grained_tools_are_registered_in_tool_list():
    from agents.ui_design.tools import _TOOLS

    names = {tool.name for tool in _TOOLS}
    assert {
        "fetch_figma_page",
        "fetch_stitch_screen",
        "fetch_design_tokens",
        "export_design_screenshot",
    }.issubset(names)


def test_sanitize_download_url_strips_query_parameters():
    from agents.ui_design.tools import _sanitize_download_url

    sanitized = _sanitize_download_url(
        "https://example.com/download/file.html?token=secret&signature=abc"
    )

    assert sanitized == "https://example.com/download/file.html"


def test_sanitize_stitch_payload_redacts_download_urls(tmp_path):
    from agents.ui_design.tools import _sanitize_stitch_payload

    payload = {
        "screen": {
            "htmlCode": {"downloadUrl": "https://example.com/code.html?token=secret"},
            "screenshot": {"downloadUrl": "https://cdn.example.com/screen.png?sig=abc"},
        }
    }

    sanitized = _sanitize_stitch_payload(payload)
    serialized = json.dumps(sanitized, ensure_ascii=False)

    assert "token=secret" not in serialized
    assert "sig=abc" not in serialized
    assert sanitized["screen"]["htmlCode"]["downloadUrl"] == "https://example.com/code.html"
