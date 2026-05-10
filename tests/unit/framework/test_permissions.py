"""Tests for framework.permissions — Permission engine."""
import pytest

from framework.errors import PermissionDeniedError
from framework.permissions import PermissionEngine, PermissionSet


class TestPermissionEngine:

    def test_default_allows_all_tools(self):
        engine = PermissionEngine()
        assert engine.check_tool("any_tool") is True

    def test_denied_tools(self):
        ps = PermissionSet(denied_tools=["rm_rf", "drop_table"])
        engine = PermissionEngine(ps)
        assert engine.check_tool("rm_rf") is False
        assert engine.check_tool("read_file") is True

    def test_allowed_tools_whitelist(self):
        ps = PermissionSet(allowed_tools=["read_file", "grep"])
        engine = PermissionEngine(ps)
        assert engine.check_tool("read_file") is True
        assert engine.check_tool("write_file") is False

    def test_require_tool_raises(self):
        ps = PermissionSet(denied_tools=["dangerous"])
        engine = PermissionEngine(ps)
        with pytest.raises(PermissionDeniedError):
            engine.require_tool("dangerous")

    def test_scm_write(self):
        engine_read = PermissionEngine(PermissionSet(scm="read"))
        assert engine_read.check_scm_write() is False

        engine_rw = PermissionEngine(PermissionSet(scm="read-write"))
        assert engine_rw.check_scm_write() is True

    def test_from_dict(self):
        engine = PermissionEngine.from_dict({
            "allowed_tools": ["read_file"],
            "scm": "read-write",
        })
        assert engine.check_tool("read_file") is True
        assert engine.check_tool("write_file") is False
        assert engine.check_scm_write() is True
