from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common.task_permissions import PermissionDeniedError, grant_permission, load_permission_grant
from office import app as office_app

# Default office permissions grant for unit tests — mirrors what Compass attaches.
_OFFICE_PERMISSIONS = load_permission_grant("office").to_dict()
# Extend the grant to allow writes — used by inplace and organize tests.
_OFFICE_RW_PERMISSIONS = grant_permission(
    _OFFICE_PERMISSIONS,
    agent="office",
    action="write",
    scope="task_root",
    description="Allow in-place write during unit tests",
)


def _make_message(
    capability: str,
    paths: list[str],
    workspace: str,
    *,
    output_mode: str = "workspace",
    permissions: dict | None = None,
) -> dict:
    if permissions is None:
        # Inplace mode needs write permission; workspace mode only needs read.
        permissions = _OFFICE_RW_PERMISSIONS if output_mode == "inplace" else _OFFICE_PERMISSIONS
    return {
        "parts": [{"text": f"Run {capability} on the given files."}],
        "metadata": {
            "requestedCapability": capability,
            "officeTargetPaths": paths,
            "officeInputRoot": workspace,
            "officeOutputMode": output_mode,
            "officeWorkspacePath": workspace,
            "sharedWorkspacePath": workspace,
            "orchestratorTaskId": "task-unit",
            "permissions": permissions,
        },
    }


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------

class TestExecuteSummary(unittest.TestCase):
    pass


class TestExecuteAnalysis(unittest.TestCase):

    def test_csv_profile_includes_grouped_numeric_totals(self):
        with tempfile.TemporaryDirectory(prefix="office_analysis_profile_") as workspace:
            source = Path(workspace, "sales.csv")
            source.write_text(
                "Sales_Rep,Sales_Amount\nAlice,10\nBob,20\nBob,5\nCharlie,7\n",
                encoding="utf-8",
            )
            profile = office_app._build_csv_profile(str(source))
            grouped = profile["groupedNumericTotals"]["Sales_Rep"]["Sales_Amount"]
            self.assertEqual(grouped[0]["group"], "Bob")
            self.assertEqual(grouped[0]["sum"], 25.0)
            self.assertEqual(grouped[0]["count"], 2)


class TestExecuteOrganize(unittest.TestCase):
    pass


# ---------------------------------------------------------------------------
# Input validation tests
# ---------------------------------------------------------------------------

class TestInputValidation(unittest.TestCase):
    pass


# ---------------------------------------------------------------------------
# File format tests
# ---------------------------------------------------------------------------

class TestFileFormats(unittest.TestCase):
    def test_legacy_doc_rejected(self):
        """A .doc file must raise RuntimeError immediately (unsupported MVP format)."""
        with tempfile.TemporaryDirectory() as workspace:
            doc = Path(workspace, "old.doc")
            doc.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1FAKE_DOC")
            with self.assertRaises(RuntimeError, msg=".doc must be rejected with conversion hint"):
                office_app._extract_document_preview(str(doc))

    def test_legacy_ppt_rejected(self):
        """A .ppt file must raise RuntimeError immediately."""
        with tempfile.TemporaryDirectory() as workspace:
            ppt = Path(workspace, "old.ppt")
            ppt.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1FAKE_PPT")
            with self.assertRaises(RuntimeError, msg=".ppt must be rejected with conversion hint"):
                office_app._extract_document_preview(str(ppt))

    def test_unknown_extension_rejected(self):
        with tempfile.TemporaryDirectory() as workspace:
            f = Path(workspace, "data.bin")
            f.write_bytes(b"\x00\x01\x02")
            with self.assertRaises(RuntimeError):
                office_app._extract_document_preview(str(f))

    def test_txt_file_preview(self):
        with tempfile.TemporaryDirectory() as workspace:
            f = Path(workspace, "note.txt")
            f.write_text("Hello world!", encoding="utf-8")
            result = office_app._extract_document_preview(str(f))
            self.assertEqual(result["type"], ".txt")
            self.assertIn("Hello world!", result["preview"])

    def test_csv_file_preview(self):
        with tempfile.TemporaryDirectory() as workspace:
            f = Path(workspace, "data.csv")
            f.write_text("a,b\n1,2\n3,4\n", encoding="utf-8")
            result = office_app._extract_document_preview(str(f))
            self.assertEqual(result["type"], ".csv")
            self.assertIn("a", result["fields"])


# ---------------------------------------------------------------------------
# Resource-limit tests
# ---------------------------------------------------------------------------

class TestResourceLimits(unittest.TestCase):
    def test_single_file_over_size_limit_raises(self):
        with tempfile.TemporaryDirectory() as workspace:
            big_file = Path(workspace, "big.txt")
            big_file.write_bytes(b"x" * 10)  # tiny file
            # Patch the limit to 5 bytes so the file exceeds it
            with mock.patch.object(office_app, "MAX_FILE_SIZE_BYTES", 5):
                with self.assertRaises(RuntimeError, msg="Single file over size limit must raise"):
                    office_app._collect_files([str(big_file)])

    def test_directory_file_count_limit_raises(self):
        with tempfile.TemporaryDirectory() as workspace:
            src = Path(workspace, "src")
            src.mkdir()
            for i in range(5):
                (src / f"{i}.txt").write_text("x", encoding="utf-8")
            with mock.patch.object(office_app, "MAX_DIR_FILE_COUNT", 3):
                with self.assertRaises(RuntimeError, msg="Directory file count limit must raise"):
                    office_app._collect_files([str(src)])

    def test_directory_total_bytes_limit_raises(self):
        with tempfile.TemporaryDirectory() as workspace:
            src = Path(workspace, "src")
            src.mkdir()
            for i in range(3):
                (src / f"{i}.txt").write_bytes(b"x" * 100)
            with mock.patch.object(office_app, "MAX_DIR_TOTAL_BYTES", 200):
                with self.assertRaises(RuntimeError, msg="Directory total bytes limit must raise"):
                    office_app._collect_files([str(src)])

    def test_preflight_scan_returns_stats(self):
        with tempfile.TemporaryDirectory() as workspace:
            src = Path(workspace, "src")
            src.mkdir()
            for i in range(3):
                (src / f"{i}.txt").write_bytes(b"x" * 50)
            scan = office_app._preflight_scan([str(src)])
            self.assertEqual(scan["fileCount"], 3)
            self.assertEqual(scan["totalBytes"], 150)
            self.assertFalse(scan["overFileCountLimit"])
            self.assertFalse(scan["overBytesLimit"])


# ---------------------------------------------------------------------------
# Utility function tests
# ---------------------------------------------------------------------------

class TestPathUtils(unittest.TestCase):
    def test_path_within_base_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            child = os.path.join(tmp, "subdir", "file.txt")
            self.assertTrue(office_app._path_within_base(child, tmp))

    def test_path_within_base_false(self):
        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            self.assertFalse(office_app._path_within_base(tmp2, tmp1))

    def test_safe_output_path_valid(self):
        with tempfile.TemporaryDirectory() as root:
            result = office_app._safe_output_path(root, "subdir/file.txt")
            self.assertTrue(result.startswith(os.path.realpath(root)))

    def test_safe_output_path_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RuntimeError):
                office_app._safe_output_path(root, "../outside.txt")

    def test_safe_output_path_absolute_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(RuntimeError):
                office_app._safe_output_path(root, "/etc/passwd")

    def test_safe_output_path_symlink_escape_rejected(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as outside:
            link_path = Path(root, "escape")
            link_path.symlink_to(outside, target_is_directory=True)
            with self.assertRaises(RuntimeError):
                office_app._safe_output_path(root, "escape/file.txt")

    def test_non_overwrite_path_no_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.md")
            self.assertEqual(office_app._non_overwrite_path(path), path)

    def test_non_overwrite_path_with_conflict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "out.md")
            Path(path).write_text("existing", encoding="utf-8")
            new_path = office_app._non_overwrite_path(path)
            self.assertNotEqual(new_path, path)
            self.assertTrue(new_path.endswith(".md"))


class TestTxtFragments(unittest.TestCase):
    def test_extracts_multiple_students(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp, "essay.txt")
            f.write_text(">>> Student Yan\nEssay A\n\n>>> Student Ethan\nEssay B\n", encoding="utf-8")
            fragments = office_app._extract_txt_fragments(str(f))
            titles = [fr["title"] for fr in fragments]
            self.assertIn("Student Yan", titles)
            self.assertIn("Student Ethan", titles)

    def test_empty_file_returns_no_fragments(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp, "empty.txt")
            f.write_text("", encoding="utf-8")
            self.assertEqual(office_app._extract_txt_fragments(str(f)), [])

    def test_no_markers_returns_no_fragments(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp, "plain.txt")
            f.write_text("Just plain text without markers.", encoding="utf-8")
            # Without >>> markers no fragments are extracted
            self.assertEqual(office_app._extract_txt_fragments(str(f)), [])


class TestBuildOrganizeContext(unittest.TestCase):
    def test_builds_context_with_inventory(self):
        with tempfile.TemporaryDirectory() as workspace:
            src = Path(workspace, "src")
            src.mkdir()
            (src / "a.txt").write_text(">>> Alice\nText A\n", encoding="utf-8")
            (src / "b.csv").write_text("x,y\n1,2\n", encoding="utf-8")
            ctx, warnings = office_app._build_organize_context([str(src)])
            paths = [item["relativePath"] for item in ctx["files"]]
            self.assertTrue(any("a.txt" in p for p in paths))
            self.assertTrue(any("b.csv" in p for p in paths))
            self.assertGreater(len(ctx["fragments"]), 0)

    def test_runtime_organize_context_omits_full_fragment_content(self):
        with tempfile.TemporaryDirectory() as workspace:
            src = Path(workspace, "src")
            src.mkdir()
            (src / "a.txt").write_text(">>> Alice\n" + ("Essay text. " * 80), encoding="utf-8")
            ctx, _ = office_app._build_organize_context([str(src)])
            runtime_ctx = office_app._runtime_organize_context(ctx, preview_chars=120)
            self.assertGreater(len(ctx["fragments"][0]["content"]), 120)
            self.assertNotIn("content", runtime_ctx["fragments"][0])
            self.assertLessEqual(len(runtime_ctx["fragments"][0]["preview"]), 120)

    def test_fragment_ids_remain_unique_with_duplicate_basenames(self):
        with tempfile.TemporaryDirectory() as workspace:
            src = Path(workspace, "src")
            (src / "0103").mkdir(parents=True)
            (src / "0110").mkdir(parents=True)
            (src / "0103" / "1.txt").write_text(">>> Student Yan\nEssay A\n", encoding="utf-8")
            (src / "0110" / "1.txt").write_text(">>> Student Ethan\nEssay B\n", encoding="utf-8")
            ctx, _ = office_app._build_organize_context([str(src)])
            fragment_ids = [item["fragmentId"] for item in ctx["fragments"]]
            self.assertEqual(len(fragment_ids), 2)
            self.assertEqual(len(set(fragment_ids)), 2)
            self.assertIn("0103/1.txt::1", fragment_ids)
            self.assertIn("0110/1.txt::1", fragment_ids)

    def test_collect_files_skips_oversized_in_folder(self):
        """Oversized files in a folder are skipped with a warning (not hard fail)."""
        with tempfile.TemporaryDirectory() as workspace:
            src = Path(workspace, "src")
            src.mkdir()
            (src / "small.txt").write_bytes(b"x" * 10)
            (src / "huge.txt").write_bytes(b"x" * 20)
            with mock.patch.object(office_app, "MAX_FILE_SIZE_BYTES", 15):
                files, warnings = office_app._collect_files([str(src)])
            self.assertEqual(len(files), 1)
            self.assertTrue(any("huge.txt" in w for w in warnings))


class TestWriteRootLocking(unittest.TestCase):
    pass


# ---------------------------------------------------------------------------
# TC-ERR-02: Password-protected / encrypted PDF
# ---------------------------------------------------------------------------

class TestPasswordProtectedPdf(unittest.TestCase):
    pass


# ---------------------------------------------------------------------------
# TC-ERR-03: Mixed folder with corrupted .xls + normal files
# ---------------------------------------------------------------------------

class TestMixedFolderPartialSuccess(unittest.TestCase):
    pass


# ---------------------------------------------------------------------------
# TC-ERR-09: Concurrent write to same directory
# ---------------------------------------------------------------------------

class TestConcurrentWriteRejection(unittest.TestCase):
    pass


# ---------------------------------------------------------------------------
# TC-PERM-05: Path containing .. (path traversal)
# ---------------------------------------------------------------------------

class TestPathTraversal(unittest.TestCase):
    pass


# ---------------------------------------------------------------------------
# TC-ERR-01: Legacy .doc file rejection
# ---------------------------------------------------------------------------

class TestLegacyDocRejection(unittest.TestCase):
    pass


# ---------------------------------------------------------------------------
# TC-ERR-07: Runtime generates illegal action
# ---------------------------------------------------------------------------

class TestIllegalRuntimeAction(unittest.TestCase):
    pass


# ---------------------------------------------------------------------------
# TC-ERR-06: Output file already exists — non-overwrite behavior
# ---------------------------------------------------------------------------

class TestNonOverwriteOutput(unittest.TestCase):
    pass


# ---------------------------------------------------------------------------
# TC-ERR-10: Large directory preflight rejection
# ---------------------------------------------------------------------------

class TestPreflightLargeDirectory(unittest.TestCase):
    pass


# ---------------------------------------------------------------------------
# Rules loading verification
# ---------------------------------------------------------------------------

class TestRulesLoading(unittest.TestCase):
    def test_office_rules_exist_and_are_loadable(self):
        """Verify rules files exist per agentic runtime doc §4.4."""
        from common.rules_loader import load_rules
        rules_text = load_rules("office")
        self.assertIn("Office", rules_text)
        self.assertIn("safety", rules_text.lower())

    def test_build_system_prompt_includes_rules(self):
        """Verify build_system_prompt injects rules into prompts."""
        from common.rules_loader import build_system_prompt
        prompt = build_system_prompt("Base system prompt.", "office")
        self.assertIn("Base system prompt.", prompt)
        self.assertIn("AGENT RULES", prompt)


# ---------------------------------------------------------------------------
# Workflow file existence
# ---------------------------------------------------------------------------

class TestWorkflowFileExists(unittest.TestCase):
    def test_office_workflow_file_exists(self):
        """Verify workflows/default-workflow.md exists per agentic runtime doc §4.4."""
        workflow_path = Path(__file__).resolve().parent.parent / "office" / "workflows" / "default-workflow.md"
        self.assertTrue(workflow_path.is_file(), f"Missing: {workflow_path}")


if __name__ == "__main__":
    unittest.main()
