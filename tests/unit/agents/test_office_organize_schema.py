"""Tests for organized-output/files/ schema enforcement in organize operations."""

import os
import pytest
import tempfile
import json

from agents.office.office_tools import (
    _normalize_organized_path,
    _is_under_organized_output,
    OrganizeMoveFileTool,
    ORGANIZED_OUTPUT_ROOT,
    VALID_CATEGORIES,
)


class TestNormalizeOrganizedPath:
    """Tests for _normalize_organized_path function."""

    def test_strips_grouped_prefix(self):
        """Strips 'grouped/' wrapper prefix."""
        result = _normalize_organized_path("grouped/Ethan/essay.txt")
        assert result == "organized-output/files/Ethan/essay.txt"

    def test_strips_by_student_prefix(self):
        """Strips 'by-student/' wrapper prefix."""
        result = _normalize_organized_path("by-student/Ethan/essay.txt")
        assert result == "organized-output/files/Ethan/essay.txt"

    def test_strips_organized_prefix(self):
        """Strips 'organized/' wrapper prefix."""
        result = _normalize_organized_path("organized/documents/report.pdf")
        assert result == "organized-output/files/documents/report.pdf"

    def test_strips_output_prefix(self):
        """Strips 'output/' wrapper prefix."""
        result = _normalize_organized_path("output/students/Yan/essay.txt")
        assert result == "organized-output/files/students/Yan/essay.txt"

    def test_preserves_organized_output_files_prefix(self):
        """Preserves paths already starting with organized-output/files/."""
        result = _normalize_organized_path("organized-output/files/students/Ethan/essay.txt")
        assert result == "organized-output/files/students/Ethan/essay.txt"

    def test_adds_prefix_to_relative_path(self):
        """Adds organized-output/files/ prefix to simple relative paths."""
        result = _normalize_organized_path("students/Ethan/essay.txt")
        assert result == "organized-output/files/students/Ethan/essay.txt"

    def test_adds_prefix_to_documents_path(self):
        """Adds prefix to documents category path."""
        result = _normalize_organized_path("documents/report.pdf.summary.md")
        assert result == "organized-output/files/documents/report.pdf.summary.md"

    def test_adds_prefix_to_data_path(self):
        """Adds prefix to data category path."""
        result = _normalize_organized_path("data/sales.csv.analysis.md")
        assert result == "organized-output/files/data/sales.csv.analysis.md"

    def test_strips_leading_slash(self):
        """Strips leading slashes from paths."""
        result = _normalize_organized_path("/students/Ethan/essay.txt")
        assert result == "organized-output/files/students/Ethan/essay.txt"

    def test_handles_nested_category_paths(self):
        """Handles nested category paths like students/GroupA/Ethan."""
        result = _normalize_organized_path("students/GroupA/Ethan/essay.txt")
        assert result == "organized-output/files/students/GroupA/Ethan/essay.txt"


class TestIsUnderOrganizedOutput:
    """Tests for _is_under_organized_output function."""

    def test_returns_true_for_valid_schema_path(self):
        """Returns True for paths under organized-output/files/."""
        assert _is_under_organized_output("organized-output/files/students/Ethan/essay.txt") is True
        assert _is_under_organized_output("organized-output/files/documents/report.pdf") is True
        assert _is_under_organized_output("organized-output/files/data/sales.csv") is True

    def test_returns_true_for_relative_category_path(self):
        """Returns True for category-relative paths."""
        assert _is_under_organized_output("students/Ethan/essay.txt") is True
        assert _is_under_organized_output("documents/report.pdf.summary.md") is True

    def test_returns_false_for_path_outside_schema(self):
        """Returns False for paths outside organized-output/files/ schema."""
        assert _is_under_organized_output("grouped/Ethan/essay.txt") is False
        assert _is_under_organized_output("output/students/Ethan/essay.txt") is False
        assert _is_under_organized_output("by-student/Ethan/essay.txt") is False


class TestOrganizedOutputRootConstant:
    """Tests for ORGANIZED_OUTPUT_ROOT constant."""

    def test_organized_output_root_value(self):
        """ORGANIZED_OUTPUT_ROOT should be 'organized-output/files/'."""
        assert ORGANIZED_OUTPUT_ROOT == "organized-output/files/"

    def test_valid_categories_contains_expected(self):
        """VALID_CATEGORIES should contain all expected categories."""
        expected = {"students", "documents", "data", "code", "images", "presentations"}
        assert expected.issubset(VALID_CATEGORIES)


class TestOrganizeMoveFileToolSchemaEnforcement:
    """Tests for OrganizeMoveFileTool schema enforcement."""

    def setup_method(self):
        """Set up test environment."""
        self.tool = OrganizeMoveFileTool()
        # Enable inplace mode for tests
        os.environ["OFFICE_ALLOW_INPLACE_WRITES"] = "true"

    def teardown_method(self):
        """Clean up test environment."""
        for key in ["OFFICE_ALLOW_INPLACE_WRITES", "OFFICE_SOURCE_ROOT", "OFFICE_WORKSPACE_ROOT", "OFFICE_OUTPUT_MODE"]:
            if key in os.environ:
                del os.environ[key]

    def test_rejects_grouped_prefix_destination(self, tmp_path):
        """Rejects destinations with grouped/ wrapper prefix."""
        os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)

        result = self.tool.execute_sync(
            action="mkdir",
            dst="grouped/Ethan",
        )
        assert not result.success
        assert "grouped/" in result.error or "outside the organized-output/files/ schema" in result.error

    def test_rejects_by_student_prefix_destination(self, tmp_path):
        """Rejects destinations with by-student/ wrapper prefix."""
        os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)

        result = self.tool.execute_sync(
            action="mkdir",
            dst="by-student/Ethan",
        )
        assert not result.success
        assert "by-student/" in result.error or "outside the organized-output/files/ schema" in result.error

    def test_rejects_output_wrapper_destination(self, tmp_path):
        """Rejects destinations with output/ wrapper prefix."""
        os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)

        result = self.tool.execute_sync(
            action="mkdir",
            dst="output/students",
        )
        assert not result.success
        assert "output/" in result.error or "outside the organized-output/files/ schema" in result.error

    def test_accepts_students_category_path(self, tmp_path):
        """Accepts destinations under students/ category."""
        os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)
        os.environ["OFFICE_OUTPUT_MODE"] = "workspace"

        result = self.tool.execute_sync(
            action="mkdir",
            dst="students/Ethan",
        )
        assert result.success, f"Should accept students category: {result.error}"
        assert (tmp_path / "organized-output" / "files" / "students" / "Ethan").exists()

    def test_accepts_documents_category_path(self, tmp_path):
        """Accepts destinations under documents/ category."""
        os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)
        os.environ["OFFICE_OUTPUT_MODE"] = "workspace"

        result = self.tool.execute_sync(
            action="write_text",
            dst="documents/report.pdf.summary.md",
            content="# Summary",
        )
        assert result.success, f"Should accept documents category: {result.error}"
        assert (tmp_path / "organized-output" / "files" / "documents" / "report.pdf.summary.md").exists()

    def test_accepts_data_category_path(self, tmp_path):
        """Accepts destinations under data/ category."""
        os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)
        os.environ["OFFICE_OUTPUT_MODE"] = "workspace"

        result = self.tool.execute_sync(
            action="write_text",
            dst="data/sales.csv.analysis.md",
            content="# Analysis",
        )
        assert result.success, f"Should accept data category: {result.error}"
        assert (tmp_path / "organized-output" / "files" / "data" / "sales.csv.analysis.md").exists()

    def test_inplace_mode_uses_source_root(self, tmp_path):
        """In inplace mode, resolves path relative to source root."""
        os.environ["OFFICE_SOURCE_ROOT"] = str(tmp_path)
        os.environ["OFFICE_ALLOW_INPLACE_WRITES"] = "true"

        # Use absolute path under organized-output/files/ schema
        target = tmp_path / "organized-output" / "files" / "students" / "Ethan"
        result = self.tool.execute_sync(
            action="mkdir",
            dst=str(target),
        )
        assert result.success, f"Should work in inplace mode: {result.error}"
        # Verify directory was created under source_root/organized-output/files/students/Ethan
        assert target.exists(), f"Directory should exist at {target}"

    def test_absolute_path_under_schema_accepted(self, tmp_path):
        """Accepts absolute paths that are under organized-output/files/."""
        os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)

        target = tmp_path / "organized-output" / "files" / "documents" / "report.txt"
        result = self.tool.execute_sync(
            action="write_text",
            dst=str(target),
            content="test",
        )
        assert result.success, f"Should accept absolute path under schema: {result.error}"
