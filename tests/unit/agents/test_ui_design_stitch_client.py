"""Unit tests for the Stitch MCP client fallback behavior."""

from agents.ui_design.clients.stitch_mcp import StitchMcpClient


def test_get_screen_falls_back_to_list_screens_on_tool_error(monkeypatch):
    client = StitchMcpClient(api_key="token")

    def fake_post(method, params, timeout=60):
        assert method == "tools/call"
        assert params["name"] == "get_screen"
        return {"result": {"isError": True}}

    monkeypatch.setattr(client, "_post", fake_post)
    monkeypatch.setattr(
        client,
        "list_screens",
        lambda project_id, timeout=60: ([{
            "name": f"projects/{project_id}/screens/screen-1",
            "title": "Landing Page",
            "htmlCode": {"downloadUrl": "https://example.com/code.html"},
            "screenshot": {"downloadUrl": "https://example.com/screen.png"},
        }], "ok"),
    )

    data, status = client.get_screen("project-1", "screen-1")

    assert status == "ok"
    assert data["screenId"] == "screen-1"
    assert data["text"].startswith("{")
    assert data["htmlCode"]["downloadUrl"] == "https://example.com/code.html"
    assert data["imageUrls"] == ["https://example.com/screen.png"]


def test_get_screen_keeps_tool_error_when_list_fallback_has_no_match(monkeypatch):
    client = StitchMcpClient(api_key="token")

    monkeypatch.setattr(
        client,
        "_post",
        lambda method, params, timeout=60: {"result": {"isError": True}},
    )
    monkeypatch.setattr(
        client,
        "list_screens",
        lambda project_id, timeout=60: ([{
            "name": f"projects/{project_id}/screens/other-screen",
            "title": "Other Screen",
        }], "ok"),
    )

    data, status = client.get_screen("project-1", "screen-1")

    assert status == "tool_error"
    assert data == {}