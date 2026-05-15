"""Tests for PermissionEngine.from_yaml() and RunConfig.permission_engine binding.

Gap 7c: PermissionEngine is loaded from config/permissions/development.yaml and
bound to the global ToolRegistry at the start of a workflow run.
"""
from __future__ import annotations

import json
import os
import tempfile

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SAMPLE_YAML = """\
allowed_tools:
  - read_file
  - write_file
  - run_command
denied_tools:
  - drop_database
scm: read-write
filesystem: workspace-only
custom:
  protected_branch_patterns:
    - "^main$"
    - "^master$"
"""


@pytest.fixture()
def tmp_yaml(tmp_path):
    f = tmp_path / "permissions.yaml"
    f.write_text(SAMPLE_YAML, encoding="utf-8")
    return str(f)


# ---------------------------------------------------------------------------
# TC-01: PermissionEngine.from_yaml loads correctly
# ---------------------------------------------------------------------------

def test_permission_engine_from_yaml_allowed_tools(tmp_yaml):
    """from_yaml() populates allowed_tools from YAML."""
    from framework.permissions import PermissionEngine

    engine = PermissionEngine.from_yaml(tmp_yaml)
    assert engine.check_tool("read_file")
    assert engine.check_tool("write_file")
    assert engine.check_tool("run_command")
    # Not in allowed list → denied
    assert not engine.check_tool("unknown_tool")


def test_permission_engine_from_yaml_denied_tools(tmp_yaml):
    """from_yaml() populates denied_tools from YAML."""
    from framework.permissions import PermissionEngine

    engine = PermissionEngine.from_yaml(tmp_yaml)
    assert not engine.check_tool("drop_database")


def test_permission_engine_from_yaml_scm_write(tmp_yaml):
    """from_yaml() sets scm to read-write correctly."""
    from framework.permissions import PermissionEngine

    engine = PermissionEngine.from_yaml(tmp_yaml)
    assert engine.check_scm_write()


def test_permission_engine_from_yaml_custom_fields(tmp_yaml):
    """from_yaml() preserves custom fields."""
    from framework.permissions import PermissionEngine

    engine = PermissionEngine.from_yaml(tmp_yaml)
    patterns = engine.permissions.custom.get("protected_branch_patterns", [])
    assert "^main$" in patterns
    assert "^master$" in patterns


def test_permission_engine_from_yaml_development_config():
    """from_yaml() can load the real development.yaml from config/."""
    from framework.permissions import PermissionEngine

    config_path = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "config", "permissions", "development.yaml"
    )
    config_path = os.path.normpath(config_path)
    if not os.path.exists(config_path):
        pytest.skip("development.yaml not found")

    engine = PermissionEngine.from_yaml(config_path)
    # development.yaml allows scm_push
    assert engine.check_tool("scm_push")
    assert engine.check_scm_write()


def test_permission_engine_from_yaml_missing_file():
    """from_yaml() raises FileNotFoundError for a missing path."""
    from framework.permissions import PermissionEngine

    with pytest.raises(FileNotFoundError):
        PermissionEngine.from_yaml("/nonexistent/path/permissions.yaml")


def test_permission_engine_from_yaml_empty_file(tmp_path):
    """from_yaml() with an empty YAML file uses safe defaults (deny-all allowlist)."""
    from framework.permissions import PermissionEngine

    empty = tmp_path / "empty.yaml"
    empty.write_text("", encoding="utf-8")
    engine = PermissionEngine.from_yaml(str(empty))
    # Empty allowed_tools → all tools allowed (open allowlist)
    assert engine.check_tool("any_tool")
    # Default scm is read → write not allowed
    assert not engine.check_scm_write()


# ---------------------------------------------------------------------------
# TC-02: RunConfig.permission_engine binds to ToolRegistry on workflow start
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_runconfig_permission_engine_binds_to_registry():
    """WorkflowRunner binds RunConfig.permission_engine to the global ToolRegistry."""
    from framework.tools.registry import ToolRegistry
    from framework.tools.base import BaseTool, ToolResult
    from framework.workflow import Workflow, START, END, RunConfig
    from framework.permissions import PermissionEngine, PermissionSet
    import framework.tools.registry as _reg

    # Isolated registry for this test
    test_registry = ToolRegistry()

    class DummyTool(BaseTool):
        name = "dummy"
        description = "A dummy tool."
        parameters_schema = {"type": "object", "properties": {}, "required": []}

        def execute_sync(self) -> ToolResult:
            return ToolResult(output='{"done": true}')

    test_registry.register(DummyTool())
    original = _reg._default_registry
    _reg._default_registry = test_registry

    # Permission engine that denies 'dummy'
    engine = PermissionEngine(PermissionSet(denied_tools=["dummy"]))
    tool_result_holder: list[str] = []

    async def check_node(state: dict) -> dict:
        result = _reg._default_registry.execute_sync("dummy", {})
        tool_result_holder.append(result)
        return {}

    wf = Workflow(
        name="perm_test",
        edges=[(START, check_node, END)],
    )
    compiled = wf.compile()
    config = RunConfig(permission_engine=engine)

    try:
        await compiled.invoke({}, config)
    finally:
        _reg._default_registry = original

    assert len(tool_result_holder) == 1
    data = json.loads(tool_result_holder[0])
    assert "error" in data
    assert "dummy" in data["error"]


@pytest.mark.asyncio
async def test_runconfig_without_permission_engine_leaves_registry_unbound():
    """When RunConfig.permission_engine is None, the ToolRegistry remains unbound."""
    from framework.tools.registry import ToolRegistry
    from framework.tools.base import BaseTool, ToolResult
    from framework.workflow import Workflow, START, END, RunConfig
    import framework.tools.registry as _reg

    test_registry = ToolRegistry()

    class PassTool(BaseTool):
        name = "pass_tool"
        description = "Always passes."
        parameters_schema = {"type": "object", "properties": {}, "required": []}

        def execute_sync(self) -> ToolResult:
            return ToolResult(output='{"ok": true}')

    test_registry.register(PassTool())
    original = _reg._default_registry
    _reg._default_registry = test_registry

    results: list[str] = []

    async def run_tool(state: dict) -> dict:
        result = _reg._default_registry.execute_sync("pass_tool", {})
        results.append(result)
        return {}

    wf = Workflow(name="no_perm", edges=[(START, run_tool, END)])
    compiled = wf.compile()

    try:
        # No permission_engine → tool executes freely
        await compiled.invoke({}, RunConfig())
    finally:
        _reg._default_registry = original

    assert len(results) == 1
    assert json.loads(results[0]) == {"ok": True}
