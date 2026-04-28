from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from office import app as office_app


def _make_message(capability: str, paths: list[str], workspace: str, *, output_mode: str = "workspace") -> dict:
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
        },
    }


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------

class TestExecuteSummary(unittest.TestCase):
    def test_writes_markdown_to_workspace(self):
        with tempfile.TemporaryDirectory(prefix="office_summary_") as workspace:
            source = Path(workspace, "essay.txt")
            source.write_text(">>> Student Yan\nHello world\n", encoding="utf-8")
            message = _make_message("office.document.summarize", [str(source)], workspace)
            with mock.patch.object(office_app, "_run_agentic_json", return_value={
                "summary_markdown": "# Summary\n\nYan wrote about hello world.",
                "warnings": [],
            }):
                result = office_app._execute_capability("task-1", message)
            summary_path = Path(workspace, "office-agent", "summary.md")
            self.assertTrue(summary_path.is_file())
            self.assertIn("# Summary", summary_path.read_text(encoding="utf-8"))
            self.assertIn("Summary created", result["summary"])
            self.assertEqual(result["artifacts"][0]["metadata"]["capability"], "office.document.summarize")

    def test_folder_summarize_multi_file(self):
        """office.folder.summarize reads all text files in a directory."""
        with tempfile.TemporaryDirectory(prefix="office_folder_sum_") as workspace:
            src_dir = Path(workspace, "docs")
            src_dir.mkdir()
            (src_dir / "a.txt").write_text("File A content.", encoding="utf-8")
            (src_dir / "b.txt").write_text("File B content.", encoding="utf-8")
            message = _make_message("office.folder.summarize", [str(src_dir)], workspace)
            with mock.patch.object(office_app, "_run_agentic_json", return_value={
                "summary_markdown": "# Folder Summary\n\nTwo files.",
                "warnings": [],
            }):
                result = office_app._execute_capability("task-fs", message)
            self.assertIn("Summary created", result["summary"])
            self.assertEqual(result["artifacts"][0]["metadata"]["capability"], "office.folder.summarize")

    def test_inplace_output_uses_task_id_suffix(self):
        """In inplace mode the output filename embeds the task id."""
        with tempfile.TemporaryDirectory(prefix="office_inplace_") as workspace:
            source = Path(workspace, "report.txt")
            source.write_text("Content here.", encoding="utf-8")
            message = _make_message(
                "office.document.summarize", [str(source)], workspace, output_mode="inplace"
            )
            with mock.patch.object(office_app, "_run_agentic_json", return_value={
                "summary_markdown": "# Inplace Summary",
                "warnings": [],
            }):
                result = office_app._execute_capability("task-inplace", message)
            # Output lives next to the source file, not in audit dir
            self.assertIn("task-inplace", result["summary"])

    def test_conflict_avoidance_renames_existing_file(self):
        """When summary.md already exists in workspace mode, a timestamped copy is created."""
        with tempfile.TemporaryDirectory(prefix="office_conflict_") as workspace:
            source = Path(workspace, "doc.txt")
            source.write_text("Some text.", encoding="utf-8")
            audit_dir = Path(workspace, "office-agent")
            audit_dir.mkdir(parents=True, exist_ok=True)
            existing = audit_dir / "summary.md"
            existing.write_text("Old summary.", encoding="utf-8")
            message = _make_message("office.document.summarize", [str(source)], workspace)
            with mock.patch.object(office_app, "_run_agentic_json", return_value={
                "summary_markdown": "# New Summary",
                "warnings": [],
            }):
                result = office_app._execute_capability("task-conflict", message)
            # The old file must still exist unchanged
            self.assertEqual(existing.read_text(encoding="utf-8"), "Old summary.")
            # A new file with timestamp must exist
            md_files = list(audit_dir.glob("summary*.md"))
            self.assertGreaterEqual(len(md_files), 2)


class TestExecuteAnalysis(unittest.TestCase):
    def test_writes_markdown_to_workspace(self):
        with tempfile.TemporaryDirectory(prefix="office_analysis_") as workspace:
            source = Path(workspace, "sales.csv")
            source.write_text("name,amount\nAlice,10\nBob,20\n", encoding="utf-8")
            message = _make_message("office.data.analyze", [str(source)], workspace)
            with mock.patch.object(office_app, "_run_agentic_json", return_value={
                "summary_markdown": "# Analysis\n\nBob has the higher amount.",
                "warnings": [],
            }):
                result = office_app._execute_capability("task-2", message)
            report_path = Path(workspace, "office-agent", "analysis.md")
            self.assertTrue(report_path.is_file())
            self.assertIn("# Analysis", report_path.read_text(encoding="utf-8"))
            self.assertIn("Analysis created", result["summary"])

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

    def test_partial_failure_writes_warnings(self):
        """If one file in a folder fails, the task continues and writes warnings.md."""
        with tempfile.TemporaryDirectory(prefix="office_partial_") as workspace:
            src_dir = Path(workspace, "data")
            src_dir.mkdir()
            (src_dir / "good.csv").write_text("x,y\n1,2\n", encoding="utf-8")
            # .xls that will raise on read
            (src_dir / "bad.xls").write_bytes(b"\x00CORRUPT")
            message = _make_message("office.data.analyze", [str(src_dir)], workspace)
            with mock.patch.object(office_app, "_run_agentic_json", return_value={
                "summary_markdown": "# Partial Analysis",
                "warnings": [],
            }):
                result = office_app._execute_capability("task-partial", message)
            warnings_path = Path(workspace, "office-agent", "warnings.md")
            self.assertTrue(warnings_path.is_file(), "warnings.md should be written for partial failures")
            self.assertGreater(len(result["warnings"]), 0)

    def test_no_data_files_raises(self):
        """analyze on a directory with only .txt files raises RuntimeError."""
        with tempfile.TemporaryDirectory(prefix="office_nodata_") as workspace:
            src_dir = Path(workspace, "docs")
            src_dir.mkdir()
            (src_dir / "readme.txt").write_text("Just text.", encoding="utf-8")
            message = _make_message("office.data.analyze", [str(src_dir)], workspace)
            with mock.patch.object(office_app, "_run_agentic_json", return_value={}):
                with self.assertRaises(RuntimeError, msg="Should fail when no CSV/XLSX present"):
                    office_app._execute_capability("task-nodata", message)


class TestExecuteOrganize(unittest.TestCase):
    def test_writes_fragments_into_workspace(self):
        with tempfile.TemporaryDirectory(prefix="office_organize_") as workspace:
            source_dir = Path(workspace, "2026")
            source_dir.mkdir(parents=True, exist_ok=True)
            source_file = source_dir / "0103.txt"
            source_file.write_text(
                ">>> Student Yan\nEssay A\n\n>>> Student Ethan\nEssay B\n",
                encoding="utf-8",
            )
            organize_context, _ = office_app._build_organize_context([str(source_dir)])
            yan_fragment = next(item for item in organize_context["fragments"] if item["title"] == "Student Yan")
            message = _make_message("office.folder.organize", [str(source_dir)], workspace)
            with mock.patch.object(office_app, "_run_agentic_json", return_value={
                "summary_markdown": "# Organize Report\n\nCreated per-student files.",
                "actions": [
                    {"action": "mkdir", "destination": "students/Yan"},
                    {
                        "action": "write_fragment",
                        "fragment_id": yan_fragment["fragmentId"],
                        "destination": "students/Yan/essay_0103.txt",
                    },
                ],
                "warnings": [],
            }):
                result = office_app._execute_capability("task-3", message)
            output_file = Path(workspace, "office-agent", "organized-output", "students", "Yan", "essay_0103.txt")
            plan_file = Path(workspace, "office-agent", "operations-plan.json")
            self.assertTrue(output_file.is_file())
            self.assertTrue(plan_file.is_file())
            self.assertIn("Essay A", output_file.read_text(encoding="utf-8"))
            self.assertIn("Organize plan executed", result["summary"])

    def test_plan_saved_before_actions(self):
        """operations-plan.json must be written before any action is executed (R8/§9.6)."""
        with tempfile.TemporaryDirectory(prefix="office_plan_order_") as workspace:
            src_dir = Path(workspace, "src")
            src_dir.mkdir()
            (src_dir / "x.txt").write_text(">>> A\nHello\n", encoding="utf-8")
            organize_context, _ = office_app._build_organize_context([str(src_dir)])
            fragment = organize_context["fragments"][0]
            written_before_action = []

            original_write = office_app._write_text_file

            def tracking_write(path: str, content: str):
                plan_path = str(Path(workspace, "office-agent", "operations-plan.json"))
                if path != plan_path and os.path.exists(plan_path):
                    written_before_action.append(True)
                original_write(path, content)

            message = _make_message("office.folder.organize", [str(src_dir)], workspace)
            with mock.patch.object(office_app, "_run_agentic_json", return_value={
                "summary_markdown": "Done.",
                "actions": [
                    {
                        "action": "write_fragment",
                        "fragment_id": fragment["fragmentId"],
                        "destination": "out/result.txt",
                    }
                ],
                "warnings": [],
            }), mock.patch.object(office_app, "_write_text_file", side_effect=tracking_write):
                office_app._execute_capability("task-order", message)
            self.assertTrue(written_before_action, "operations-plan.json was not saved before action writes")

    def test_copy_file_action(self):
        """copy_file action copies a source file to a destination inside output root."""
        with tempfile.TemporaryDirectory(prefix="office_copy_") as workspace:
            src_dir = Path(workspace, "src")
            src_dir.mkdir()
            source_file = src_dir / "doc.txt"
            source_file.write_text("Content to copy.", encoding="utf-8")
            organize_context, _ = office_app._build_organize_context([str(src_dir)])
            message = _make_message("office.folder.organize", [str(src_dir)], workspace)
            with mock.patch.object(office_app, "_run_agentic_json", return_value={
                "summary_markdown": "Copied.",
                "actions": [
                    {
                        "action": "copy_file",
                        "source": str(source_file),
                        "destination": "archive/doc.txt",
                    }
                ],
                "warnings": [],
            }):
                result = office_app._execute_capability("task-copy", message)
            dest = Path(workspace, "office-agent", "organized-output", "archive", "doc.txt")
            self.assertTrue(dest.is_file())
            self.assertEqual(dest.read_text(encoding="utf-8"), "Content to copy.")

    def test_inplace_mode_writes_to_source_dir(self):
        """In inplace outputMode, organised files are created inside the source directory tree."""
        with tempfile.TemporaryDirectory(prefix="office_inplace_org_") as workspace:
            src_dir = Path(workspace, "src")
            src_dir.mkdir()
            (src_dir / "a.txt").write_text(">>> Alice\nHi\n", encoding="utf-8")
            organize_context, _ = office_app._build_organize_context([str(src_dir)])
            fragment = organize_context["fragments"][0]
            message = _make_message(
                "office.folder.organize", [str(src_dir)], workspace, output_mode="inplace"
            )
            with mock.patch.object(office_app, "_run_agentic_json", return_value={
                "summary_markdown": "Inplace done.",
                "actions": [
                    {
                        "action": "write_fragment",
                        "fragment_id": fragment["fragmentId"],
                        "destination": "people/alice.txt",
                    }
                ],
                "warnings": [],
            }):
                result = office_app._execute_capability("task-ip", message)
            out_file = src_dir / "people" / "alice.txt"
            self.assertTrue(out_file.is_file())
            self.assertIn("Hi", out_file.read_text(encoding="utf-8"))

    def test_organize_conflict_avoidance_renames_existing_file(self):
        """Organize must not overwrite an existing output file; it creates a renamed copy instead."""
        with tempfile.TemporaryDirectory(prefix="office_org_conflict_") as workspace:
            src_dir = Path(workspace, "src")
            src_dir.mkdir()
            (src_dir / "a.txt").write_text(">>> Alice\nEssay\n", encoding="utf-8")
            organize_context, _ = office_app._build_organize_context([str(src_dir)])
            fragment = organize_context["fragments"][0]
            dest_dir = Path(workspace, "office-agent", "organized-output", "students", "Alice")
            dest_dir.mkdir(parents=True, exist_ok=True)
            existing = dest_dir / "essay.txt"
            existing.write_text("existing", encoding="utf-8")
            message = _make_message("office.folder.organize", [str(src_dir)], workspace)
            with mock.patch.object(office_app, "_run_agentic_json", return_value={
                "summary_markdown": "Done.",
                "actions": [
                    {
                        "action": "write_fragment",
                        "fragment_id": fragment["fragmentId"],
                        "destination": "students/Alice/essay.txt",
                    }
                ],
                "warnings": [],
            }):
                result = office_app._execute_capability("task-conflict-org", message)
            self.assertEqual(existing.read_text(encoding="utf-8"), "existing")
            candidates = list(dest_dir.glob("essay*.txt"))
            self.assertGreaterEqual(len(candidates), 2)
            self.assertTrue(any("Avoided overwrite" in item for item in result["warnings"]))

    def test_partial_manifest_written_when_action_fails(self):
        """When organize fails mid-run, the partial manifest should already be persisted for recovery."""
        with tempfile.TemporaryDirectory(prefix="office_manifest_partial_") as workspace:
            src_dir = Path(workspace, "src")
            src_dir.mkdir()
            (src_dir / "a.txt").write_text(">>> Alice\nEssay\n", encoding="utf-8")
            organize_context, _ = office_app._build_organize_context([str(src_dir)])
            fragment = organize_context["fragments"][0]
            original_write = office_app._write_text_file

            def fail_on_second_output(path: str, content: str):
                if path.endswith(os.path.join("out", "broken.txt")):
                    raise OSError("disk full")
                original_write(path, content)

            message = _make_message("office.folder.organize", [str(src_dir)], workspace)
            with mock.patch.object(office_app, "_run_agentic_json", return_value={
                "summary_markdown": "Done.",
                "actions": [
                    {
                        "action": "write_fragment",
                        "fragment_id": fragment["fragmentId"],
                        "destination": "out/ok.txt",
                    },
                    {
                        "action": "write_text",
                        "destination": "out/broken.txt",
                        "content": "will fail",
                    },
                ],
                "warnings": [],
            }), mock.patch.object(office_app, "_write_text_file", side_effect=fail_on_second_output):
                with self.assertRaises(OSError):
                    office_app._execute_capability("task-manifest", message)

            manifest_path = Path(workspace, "office-agent", "organized-output", ".office-agent-manifest.json")
            self.assertTrue(manifest_path.is_file())
            manifest = manifest_path.read_text(encoding="utf-8")
            self.assertIn("out/ok.txt", manifest)

    def test_rejects_unsafe_destination(self):
        with tempfile.TemporaryDirectory(prefix="office_organize_fail_") as workspace:
            source_dir = Path(workspace, "2026")
            source_dir.mkdir(parents=True, exist_ok=True)
            (source_dir / "0103.txt").write_text(">>> Student Yan\nEssay A\n", encoding="utf-8")
            message = _make_message("office.folder.organize", [str(source_dir)], workspace)
            with mock.patch.object(office_app, "_run_agentic_json", return_value={
                "summary_markdown": "# Organize Report",
                "actions": [{"action": "write_text", "destination": "../escape.txt", "content": "bad"}],
                "warnings": [],
            }):
                with self.assertRaises(RuntimeError):
                    office_app._execute_capability("task-4", message)

    def test_rejects_unknown_fragment_id(self):
        with tempfile.TemporaryDirectory(prefix="office_bad_frag_") as workspace:
            src_dir = Path(workspace, "src")
            src_dir.mkdir()
            (src_dir / "f.txt").write_text(">>> A\nContent\n", encoding="utf-8")
            organize_context, _ = office_app._build_organize_context([str(src_dir)])
            message = _make_message("office.folder.organize", [str(src_dir)], workspace)
            with mock.patch.object(office_app, "_run_agentic_json", return_value={
                "summary_markdown": ".",
                "actions": [{"action": "write_fragment", "fragment_id": "nonexistent::99", "destination": "out.txt"}],
                "warnings": [],
            }):
                with self.assertRaises(RuntimeError, msg="Unknown fragment_id must be rejected"):
                    office_app._execute_capability("task-badfrag", message)

    def test_write_text_normalizes_escaped_newlines(self):
        with tempfile.TemporaryDirectory(prefix="office_readme_newlines_") as workspace:
            src_dir = Path(workspace, "src")
            src_dir.mkdir()
            (src_dir / "f.txt").write_text(">>> Student Yan\nEssay\n", encoding="utf-8")
            message = _make_message("office.folder.organize", [str(src_dir)], workspace)
            with mock.patch.object(office_app, "_run_agentic_json", return_value={
                "summary_markdown": "Done.",
                "actions": [
                    {
                        "action": "write_text",
                        "destination": "students/Yan/README.txt",
                        "content": "Line one\\n\\nLine two",
                    }
                ],
                "warnings": [],
            }):
                office_app._execute_capability("task-readme", message)
            readme = Path(workspace, "office-agent", "organized-output", "students", "Yan", "README.txt")
            self.assertEqual(readme.read_text(encoding="utf-8"), "Line one\n\nLine two")

    def test_rejects_unsupported_action(self):
        with tempfile.TemporaryDirectory(prefix="office_bad_action_") as workspace:
            src_dir = Path(workspace, "src")
            src_dir.mkdir()
            (src_dir / "f.txt").write_text("Content.", encoding="utf-8")
            message = _make_message("office.folder.organize", [str(src_dir)], workspace)
            with mock.patch.object(office_app, "_run_agentic_json", return_value={
                "summary_markdown": ".",
                "actions": [{"action": "shell_exec", "destination": "out.txt", "cmd": "rm -rf /"}],
                "warnings": [],
            }):
                with self.assertRaises(RuntimeError, msg="Unsupported action must be rejected"):
                    office_app._execute_capability("task-badact", message)


# ---------------------------------------------------------------------------
# Input validation tests
# ---------------------------------------------------------------------------

class TestInputValidation(unittest.TestCase):
    def test_missing_capability_raises(self):
        with tempfile.TemporaryDirectory() as workspace:
            message = {
                "parts": [{"text": "Do something."}],
                "metadata": {
                    "requestedCapability": "",
                    "officeTargetPaths": [workspace],
                    "officeInputRoot": workspace,
                    "officeWorkspacePath": workspace,
                    "sharedWorkspacePath": workspace,
                },
            }
            with self.assertRaises(RuntimeError, msg="Missing capability must raise"):
                office_app._execute_capability("task-nocap", message)

    def test_missing_target_paths_raises(self):
        with tempfile.TemporaryDirectory() as workspace:
            message = {
                "parts": [{"text": "Summarize nothing."}],
                "metadata": {
                    "requestedCapability": "office.document.summarize",
                    "officeTargetPaths": [],
                    "officeInputRoot": workspace,
                    "officeWorkspacePath": workspace,
                    "sharedWorkspacePath": workspace,
                },
            }
            with self.assertRaises(RuntimeError, msg="Missing paths must raise"):
                office_app._execute_capability("task-nopaths", message)

    def test_path_outside_input_root_rejected(self):
        with tempfile.TemporaryDirectory() as workspace, tempfile.TemporaryDirectory() as other:
            source = Path(other, "secret.txt")
            source.write_text("secret", encoding="utf-8")
            message = _make_message("office.document.summarize", [str(source)], workspace)
            with self.assertRaises(RuntimeError, msg="Path outside input root must be rejected"):
                office_app._execute_capability("task-escape", message)

    def test_unsupported_capability_raises(self):
        with tempfile.TemporaryDirectory() as workspace:
            source = Path(workspace, "f.txt")
            source.write_text("Content.", encoding="utf-8")
            message = _make_message("office.does.not.exist", [str(workspace)], workspace)
            with self.assertRaises(RuntimeError, msg="Unknown capability must raise"):
                office_app._execute_capability("task-unknowncap", message)


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

    def test_preflight_over_limit_returns_report_artifact(self):
        """When preflight detects over-limit, _execute_summary returns a report instead of failing."""
        with tempfile.TemporaryDirectory() as workspace:
            source = Path(workspace, "big.txt")
            source.write_text("Content.", encoding="utf-8")
            message = _make_message("office.document.summarize", [str(source)], workspace)
            over_limit_scan = {
                "fileCount": 5000, "totalBytes": 300 * 1024 * 1024,
                "largeFiles": [], "overFileCountLimit": True, "overBytesLimit": True,
                "limitFileCount": 2000, "limitTotalMB": 250,
            }
            with mock.patch.object(office_app, "_preflight_scan", return_value=over_limit_scan):
                result = office_app._execute_capability("task-overlimit", message)
            self.assertIn("Preflight limit exceeded", result["summary"])
            self.assertEqual(result["artifacts"][0]["name"], "office-preflight-report")


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
    def test_inplace_write_lock_fails_fast(self):
        with tempfile.TemporaryDirectory(prefix="office_lock_") as workspace:
            source = Path(workspace, "report.txt")
            source.write_text("Content here.", encoding="utf-8")
            message = _make_message(
                "office.document.summarize", [str(source)], workspace, output_mode="inplace"
            )
            office_app._acquire_write_root(workspace)
            try:
                with mock.patch.object(office_app, "_run_agentic_json", return_value={
                    "summary_markdown": "# Summary",
                    "warnings": [],
                }):
                    with self.assertRaises(RuntimeError):
                        office_app._execute_capability("task-lock", message)
            finally:
                office_app._release_write_root(workspace)


if __name__ == "__main__":
    unittest.main()
