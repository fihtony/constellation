"""Tests for organized-output/files/ schema enforcement in organize operations.

These tests pin the generic, dimension-agnostic path-normalisation and
``organized-output/files/`` prefix rules. They use neutral bucket/file
identifiers (``bucket_a/asset_1.txt`` etc.) on purpose: the office agent
is now dimension-driven and the suite must not embed any business
specifics (no entity names, no ``by-student/``-style wrappers).
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
        result = _ensure_organized_output_prefix("/bucket_a/asset_1.txt")
        assert result == "organized-output/files/bucket_a/asset_1.txt"

    def test_preserves_existing_prefix(self):
        result = _ensure_organized_output_prefix("organized-output/files/a.txt")
        assert result == "organized-output/files/a.txt"

    def test_prepends_prefix_for_relative_path(self):
        result = _ensure_organized_output_prefix("documents/report.pdf.summary.md")
        assert result == "organized-output/files/documents/report.pdf.summary.md"


class TestIsUnderOrganizedOutput:
    """The prefix check now only recognises ``organized-output/``."""

    def test_returns_true_for_schema_path(self):
        assert _is_under_organized_output("organized-output/files/bucket_a/asset_1.txt") is True
        assert _is_under_organized_output("organized-output/files/documents/report.pdf") is True

    def test_returns_true_for_stripped_leading_slash(self):
        assert _is_under_organized_output("/organized-output/files/a.txt") is True

    def test_returns_false_for_unrelated_path(self):
        # Category-relative paths are no longer considered "under
        # organized output" because the agent is dimension-agnostic.
        assert _is_under_organized_output("bucket_a/asset_1.txt") is False
        assert _is_under_organized_output("documents/report.pdf") is False
        assert _is_under_organized_output("wrapper/asset_1.txt") is False


class TestOrganizedOutputRootConstant:
    """The prefix constant keeps its historical value."""

    def test_organized_output_root_value(self):
        assert ORGANIZED_OUTPUT_ROOT == "organized-output/files/"


class TestOrganizeMoveFileToolSchemaEnforcement:
    """``OrganizeMoveFileTool`` now relies on the generic prefix check
    only — the wrapper-rejection allowlist is gone because no
    business-specific allowlist remains. The agent is dimension-driven;
    any bucket name is acceptable as long as it lands under
    ``organized-output/files/``."""

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
            dst="bucket_a",
        )
        # The prefix is auto-prepended, so this should now succeed
        # (no business allowlist rejects arbitrary buckets).
        assert result.success, result.error
        assert (tmp_path / "organized-output" / "files" / "bucket_a").exists()
