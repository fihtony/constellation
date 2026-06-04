from agents.office.nodes import _capability_tool_names


def test_capability_tool_names_includes_delete_output_file_for_all_three():
    for capability in ("analyze", "summarize", "organize"):
        names = _capability_tool_names(capability, "workspace")
        assert "delete_output_file" in names, capability


def test_delete_output_file_in_office_tools_registry():
    """The framework-side _OFFICE_TOOLS list must contain a DeleteOutputFileTool instance.

    Spec: 'the tool must be registered for all three capabilities ... if the
    registration check fails, the gate must surface a tool_unavailable error'
    """
    from agents.office.office_tools import _OFFICE_TOOLS, DeleteOutputFileTool
    # Either a DeleteOutputFileTool instance OR any tool with the matching name works
    assert any(
        (isinstance(t, DeleteOutputFileTool) or getattr(t, "name", None) == "delete_output_file")
        for t in _OFFICE_TOOLS
    ), "delete_output_file is not registered in _OFFICE_TOOLS"
