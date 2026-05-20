"""Tests for timestamp-based backup behavior in write_workspace and write_file tools."""

import json
import os
import re
import time
from pathlib import Path

import pytest


class TestBackupExistingFile:
    """Test _backup_existing_file helper function."""

    def test_backup_creates_timestamped_file(self, tmp_path):
        """Existing file gets backed up with timestamp suffix."""
        from agents.office.office_tools import _backup_existing_file

        test_file = tmp_path / "report.txt"
        test_file.write_text("original content")

        backup_path = _backup_existing_file(str(test_file))

        assert backup_path is not None
        assert Path(backup_path).exists()
        assert Path(backup_path).read_text() == "original content"
        # Check timestamp format: report.txt.YYYYMMDD-HHMMSS.bak
        assert test_file.name in backup_path
        timestamp_pattern = r"\.\d{8}-\d{6}\.bak$"
        assert re.search(timestamp_pattern, backup_path), f"Backup path {backup_path} doesn't match timestamp pattern"

    def test_backup_returns_none_for_missing_file(self, tmp_path):
        """Returns None when file doesn't exist."""
        from agents.office.office_tools import _backup_existing_file

        non_existent = tmp_path / "does_not_exist.txt"
        result = _backup_existing_file(str(non_existent))

        assert result is None

    def test_backup_returns_none_when_disabled_via_env(self, tmp_path, monkeypatch):
        """Backup is skipped when OFFICE_BACKUP_ENABLED=false."""
        from agents.office.office_tools import _backup_existing_file

        test_file = tmp_path / "data.csv"
        test_file.write_text("some data")

        monkeypatch.setenv("OFFICE_BACKUP_ENABLED", "false")
        result = _backup_existing_file(str(test_file))

        assert result is None
        assert test_file.exists()  # Original still there
        assert test_file.read_text() == "some data"


class TestWriteWorkspaceBackup:
    """Test backup behavior in WriteWorkspaceTool."""

    def test_write_workspace_creates_backup_when_file_exists(self, tmp_path):
        """Writing to existing file creates timestamped backup."""
        from agents.office.office_tools import WriteWorkspaceTool

        tool = WriteWorkspaceTool()
        os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)
        try:
            # First write
            result1 = tool.execute_sync(filename="summary.md", content="# Original")
            assert result1.success

            # Second write to same file
            result2 = tool.execute_sync(filename="summary.md", content="# Updated")
            assert result2.success

            data = json.loads(result2.output)
            assert "backup_path" in data

            # Verify backup file exists with timestamp
            backup_path = data["backup_path"]
            assert Path(backup_path).exists()
            assert Path(backup_path).read_text() == "# Original"

            # Verify new file has updated content
            assert (tmp_path / "summary.md").read_text() == "# Updated"
        finally:
            os.environ.pop("OFFICE_WORKSPACE_ROOT", None)

    def test_write_workspace_no_backup_for_new_file(self, tmp_path):
        """Writing a new file doesn't create a backup."""
        from agents.office.office_tools import WriteWorkspaceTool

        tool = WriteWorkspaceTool()
        os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)
        try:
            result = tool.execute_sync(filename="new.txt", content="hello")
            assert result.success

            data = json.loads(result.output)
            assert "backup_path" not in data
        finally:
            os.environ.pop("OFFICE_WORKSPACE_ROOT", None)

    def test_write_workspace_timestamp_format(self, tmp_path):
        """Backup filename uses correct timestamp format YYYYMMDD-HHMMSS."""
        from agents.office.office_tools import WriteWorkspaceTool

        tool = WriteWorkspaceTool()
        os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)
        try:
            # Create initial file
            tool.execute_sync(filename="doc.txt", content="v1")
            # Overwrite
            result = tool.execute_sync(filename="doc.txt", content="v2")
            assert result.success

            data = json.loads(result.output)
            assert "backup_path" in data

            # Extract timestamp part
            backup_path = data["backup_path"]
            match = re.search(r"\.(\d{8}-\d{6})\.bak$", backup_path)
            assert match, f"Timestamp not found in {backup_path}"

            timestamp = match.group(1)
            # Verify format by parsing - should be valid date/time
            parsed = time.strptime(timestamp, "%Y%m%d-%H%M%S")
            assert parsed.tm_year == 2026  # Current year check
        finally:
            os.environ.pop("OFFICE_WORKSPACE_ROOT", None)


class TestWriteFileBackup:
    """Test backup behavior in WriteFileTool."""

    def test_write_file_creates_backup_when_file_exists(self, tmp_path):
        """Writing to existing file creates timestamped backup."""
        from agents.office.office_tools import WriteFileTool

        tool = WriteFileTool()

        # Set up environment for inplace writes
        os.environ["OFFICE_ALLOW_INPLACE_WRITES"] = "true"
        os.environ["OFFICE_SOURCE_ROOT"] = str(tmp_path)
        try:
            test_file = tmp_path / "data.json"
            test_file.write_text('{"version": 1}')

            # Overwrite the file
            result = tool.execute_sync(path=str(test_file), content='{"version": 2}')
            assert result.success

            data = json.loads(result.output)
            assert "backup_path" in data

            # Verify backup
            backup_path = data["backup_path"]
            assert Path(backup_path).exists()
            assert Path(backup_path).read_text() == '{"version": 1}'

            # Verify new content
            assert test_file.read_text() == '{"version": 2}'
        finally:
            os.environ.pop("OFFICE_ALLOW_INPLACE_WRITES", None)
            os.environ.pop("OFFICE_SOURCE_ROOT", None)

    def test_write_file_no_backup_for_new_file(self, tmp_path):
        """Writing a new file doesn't create a backup."""
        from agents.office.office_tools import WriteFileTool

        tool = WriteFileTool()
        os.environ["OFFICE_ALLOW_INPLACE_WRITES"] = "true"
        os.environ["OFFICE_SOURCE_ROOT"] = str(tmp_path)
        try:
            result = tool.execute_sync(path=str(tmp_path / "brand_new.txt"), content="new")
            assert result.success

            data = json.loads(result.output)
            assert "backup_path" not in data
        finally:
            os.environ.pop("OFFICE_ALLOW_INPLACE_WRITES", None)
            os.environ.pop("OFFICE_SOURCE_ROOT", None)


class TestBackupDisabled:
    """Test OFFICE_BACKUP_ENABLED environment variable."""

    def test_backup_can_be_disabled(self, tmp_path, monkeypatch):
        """When OFFICE_BACKUP_ENABLED=false, no backup is created."""
        from agents.office.office_tools import WriteWorkspaceTool

        tool = WriteWorkspaceTool()
        os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)
        monkeypatch.setenv("OFFICE_BACKUP_ENABLED", "false")
        try:
            # First write
            tool.execute_sync(filename="page.md", content="v1")
            # Second write
            result = tool.execute_sync(filename="page.md", content="v2")
            assert result.success

            data = json.loads(result.output)
            assert "backup_path" not in data
            # Original file should be overwritten (not backed up)
            assert (tmp_path / "page.md").read_text() == "v2"
        finally:
            os.environ.pop("OFFICE_WORKSPACE_ROOT", None)