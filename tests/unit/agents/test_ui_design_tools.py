"""Tests for UI Design boundary tool registration."""


def test_ui_design_fine_grained_tools_are_registered_in_tool_list():
    from agents.ui_design.tools import _TOOLS

    names = {tool.name for tool in _TOOLS}
    assert {
        "fetch_figma_page",
        "fetch_stitch_screen",
        "fetch_design_tokens",
        "export_design_screenshot",
    }.issubset(names)