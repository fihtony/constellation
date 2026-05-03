"""Constellation unified tool framework.

Tools are defined once as ``ConstellationTool`` subclasses and can be
exposed in two modes:

- **MCP mode**: via ``mcp_adapter.start_mcp_server()`` for claude-code / copilot-cli.
- **Native mode**: via ``native_adapter.get_function_definitions()`` for
  copilot-connect function_calling.

Import barrel files to trigger self-registration::

    import common.tools.dev_agent_tools   # registers all dev-agent tools
    import common.tools.team_lead_tools   # registers all team-lead tools
"""
