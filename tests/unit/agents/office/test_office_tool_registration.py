from agents.office.nodes import _capability_tool_names


def test_capability_tool_names_includes_delete_output_file_for_all_three():
    for capability in ("analyze", "summarize", "organize"):
        names = _capability_tool_names(capability, "workspace")
        assert "delete_output_file" in names, capability
