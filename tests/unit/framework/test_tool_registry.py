"""Tests for ToolRegistry — including permission engine integration."""
import json
import pytest

from framework.tools.base import BaseTool, ToolResult
from framework.tools.registry import ToolRegistry
from framework.permissions import PermissionEngine, PermissionSet
from framework.errors import PermissionDeniedError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _EchoTool(BaseTool):
    name = "echo"
    description = "Returns its input."
    parameters_schema = {
        "type": "object",
        "properties": {"text": {"type": "string"}},
        "required": ["text"],
    }

    def execute_sync(self, **kwargs) -> ToolResult:
        return ToolResult(output=json.dumps({"echo": kwargs.get("text", "")}))


class _DangerousTool(BaseTool):
    name = "dangerous"
    description = "Simulates a dangerous operation."
    parameters_schema = {"type": "object", "properties": {}}

    def execute_sync(self, **kwargs) -> ToolResult:
        return ToolResult(output='{"status": "done"}')


# ---------------------------------------------------------------------------
# Basic registry tests
# ---------------------------------------------------------------------------

class TestToolRegistryBasic:

    def test_register_and_get(self):
        reg = ToolRegistry()
        tool = _EchoTool()
        reg.register(tool)
        assert reg.get("echo") is tool

    def test_names(self):
        reg = ToolRegistry()
        reg.register(_EchoTool())
        reg.register(_DangerousTool())
        assert set(reg.names()) == {"echo", "dangerous"}

    def test_list_schemas(self):
        reg = ToolRegistry()
        reg.register(_EchoTool())
        schemas = reg.list_schemas()
        assert len(schemas) == 1
        assert schemas[0]["function"]["name"] == "echo"

    def test_list_schemas_subset(self):
        reg = ToolRegistry()
        reg.register(_EchoTool())
        reg.register(_DangerousTool())
        schemas = reg.list_schemas(["echo"])
        assert len(schemas) == 1

    def test_execute_sync_success(self):
        reg = ToolRegistry()
        reg.register(_EchoTool())
        result = reg.execute_sync("echo", {"text": "hello"})
        data = json.loads(result)
        assert data["echo"] == "hello"

    def test_execute_sync_unknown_tool(self):
        reg = ToolRegistry()
        result = reg.execute_sync("nonexistent", {})
        data = json.loads(result)
        assert "error" in data

    def test_execute_sync_json_string_args(self):
        reg = ToolRegistry()
        reg.register(_EchoTool())
        result = reg.execute_sync("echo", '{"text": "world"}')
        data = json.loads(result)
        assert data["echo"] == "world"

    async def test_execute_async_success(self):
        reg = ToolRegistry()
        reg.register(_EchoTool())
        result = await reg.execute("echo", {"text": "async"})
        data = json.loads(result)
        assert data["echo"] == "async"


# ---------------------------------------------------------------------------
# Permission engine integration
# ---------------------------------------------------------------------------

class TestToolRegistryPermissions:

    def test_no_permission_engine_allows_all(self):
        reg = ToolRegistry()
        reg.register(_DangerousTool())
        result = reg.execute_sync("dangerous", {})
        data = json.loads(result)
        assert "error" not in data

    def test_permission_engine_blocks_denied_tool(self):
        reg = ToolRegistry()
        reg.register(_EchoTool())
        reg.register(_DangerousTool())

        ps = PermissionSet(denied_tools=["dangerous"])
        engine = PermissionEngine(ps)
        reg.set_permission_engine(engine)

        # "echo" should still work
        result = reg.execute_sync("echo", {"text": "hi"})
        data = json.loads(result)
        assert data.get("echo") == "hi"

        # "dangerous" should be blocked
        result = reg.execute_sync("dangerous", {})
        data = json.loads(result)
        assert "error" in data
        assert "not permitted" in data["error"].lower()

    def test_permission_engine_denial_writes_audit_record(self, tmp_path):
        from framework.audit_log import (
            clear_permission_audit_context,
            set_permission_audit_context,
        )

        reg = ToolRegistry()
        reg.register(_DangerousTool())
        reg.set_permission_engine(PermissionEngine(PermissionSet(denied_tools=["dangerous"])))
        set_permission_audit_context(
            workspace_path=str(tmp_path),
            agent_id="web-dev",
            task_id="task-audit",
        )
        try:
            result = reg.execute_sync("dangerous", {})
        finally:
            clear_permission_audit_context()

        assert "error" in json.loads(result)
        audit_path = tmp_path / "web-dev" / "permission-denials.jsonl"
        records = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines()]
        assert records[-1]["agent_id"] == "web-dev"
        assert records[-1]["task_id"] == "task-audit"
        assert records[-1]["status"] == "denied"
        assert records[-1]["operation"] == "tool"
        assert records[-1]["tool"] == "dangerous"
        assert "not permitted" in records[-1]["reason"].lower()

    def test_permission_engine_allowlist(self):
        reg = ToolRegistry()
        reg.register(_EchoTool())
        reg.register(_DangerousTool())

        ps = PermissionSet(allowed_tools=["echo"])
        engine = PermissionEngine(ps)
        reg.set_permission_engine(engine)

        # "echo" allowed
        result = reg.execute_sync("echo", {"text": "allowed"})
        assert json.loads(result).get("echo") == "allowed"

        # "dangerous" not in allowlist → blocked
        result = reg.execute_sync("dangerous", {})
        assert "error" in json.loads(result)

    async def test_permission_engine_async_blocks(self):
        reg = ToolRegistry()
        reg.register(_DangerousTool())

        ps = PermissionSet(denied_tools=["dangerous"])
        engine = PermissionEngine(ps)
        reg.set_permission_engine(engine)

        result = await reg.execute("dangerous", {})
        data = json.loads(result)
        assert "error" in data

    def test_set_permission_engine_returns_self(self):
        reg = ToolRegistry()
        ps = PermissionSet()
        engine = PermissionEngine(ps)
        assert reg.set_permission_engine(engine) is reg

    def test_permission_engine_default_allows_all(self):
        """A PermissionEngine with default PermissionSet allows everything."""
        reg = ToolRegistry()
        reg.register(_EchoTool())
        reg.register(_DangerousTool())

        engine = PermissionEngine()  # no restrictions
        reg.set_permission_engine(engine)

        result = reg.execute_sync("dangerous", {})
        assert "error" not in json.loads(result)
