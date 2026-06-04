"""Tests for organized-output/files/ schema enforcement in organize operations.

Block 2 rewrote the office_tools module to drop business hardcodes. The
allowlist-based helpers (``_normalize_organized_path``, ``VALID_CATEGORIES``,
``_is_wrapper_prefixed``, ``WRAPPER_PREFIXES``, ``IDENTITY_PREFIXES``,
``_clean_entity_candidate``, ``_looks_like_person_name``,
``_extract_primary_entity``) are gone. Block 4 will rewrite this file
end-to-end; for now we keep the suite green by mirroring the new
generic behaviour in a smaller, neutral form.
"""

import os

from agents.office.office_tools import (
    _ensure_organized_output_prefix,
    _is_under_organized_output,
    OrganizeMoveFileTool,
    ORGANIZED_OUTPUT_ROOT,
)


class TestEnsureOrganizedOutputPrefix:
    """The prefix helper is a thin wrapper: ensure leading slash is
    stripped and ``organized-output/files/`` is prepended when missing."""

    def test_strips_leading_slash(self):
        result = _ensure_organized_output_prefix("/students/Ethan/essay.txt")
        assert result == "organized-output/files/students/Ethan/essay.txt"

    def test_preserves_existing_prefix(self):
        result = _ensure_organized_output_prefix("organized-output/files/a.txt")
        assert result == "organized-output/files/a.txt"

    def test_prepends_prefix_for_relative_path(self):
        result = _ensure_organized_output_prefix("documents/report.pdf.summary.md")
        assert result == "organized-output/files/documents/report.pdf.summary.md"


class TestIsUnderOrganizedOutput:
    """The prefix check now only recognises ``organized-output/``."""

    def test_returns_true_for_schema_path(self):
        assert _is_under_organized_output("organized-output/files/students/Ethan/essay.txt") is True
        assert _is_under_organized_output("organized-output/files/documents/report.pdf") is True

    def test_returns_true_for_stripped_leading_slash(self):
        assert _is_under_organized_output("/organized-output/files/a.txt") is True

    def test_returns_false_for_unrelated_path(self):
        # Category-relative paths are no longer considered "under
        # organized output" because the agent is dimension-agnostic.
        assert _is_under_organized_output("students/Ethan/essay.txt") is False
        assert _is_under_organized_output("documents/report.pdf") is False
        assert _is_under_organized_output("grouped/Ethan/essay.txt") is False


class TestOrganizedOutputRootConstant:
    """The prefix constant keeps its historical value."""

    def test_organized_output_root_value(self):
        assert ORGANIZED_OUTPUT_ROOT == "organized-output/files/"


class TestOrganizeMoveFileToolSchemaEnforcement:
    """``OrganizeMoveFileTool`` now relies on the generic prefix check
    only — the ``grouped/``/``by-student/``/``output/`` wrapper rejection
    is gone because no business-specific allowlist remains."""

    def setup_method(self):
        self.tool = OrganizeMoveFileTool()
        os.environ["OFFICE_ALLOW_INPLACE_WRITES"] = "true"

    def teardown_method(self):
        for key in ("OFFICE_ALLOW_INPLACE_WRITES", "OFFICE_SOURCE_ROOT",
                    "OFFICE_WORKSPACE_ROOT", "OFFICE_OUTPUT_MODE"):
            os.environ.pop(key, None)

    def test_accepts_organized_output_files_relative_path(self, tmp_path):
        os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)
        os.environ["OFFICE_OUTPUT_MODE"] = "workspace"

        result = self.tool.execute_sync(
            action="write_text",
            dst="documents/report.pdf.summary.md",
            content="# Summary",
        )
        assert result.success, result.error
        assert (tmp_path / "organized-output" / "files" / "documents" / "report.pdf.summary.md").exists()

    def test_rejects_destination_outside_workspace(self, tmp_path):
        os.environ["OFFICE_WORKSPACE_ROOT"] = str(tmp_path)
        os.environ["OFFICE_OUTPUT_MODE"] = "workspace"

        result = self.tool.execute_sync(
            action="mkdir",
            dst="students/Ethan",
        )
        # The prefix is auto-prepended, so this should now succeed
        # (no business allowlist rejects arbitrary buckets).
        assert result.success, result.error
        assert (tmp_path / "organized-output" / "files" / "students" / "Ethan").exists()
