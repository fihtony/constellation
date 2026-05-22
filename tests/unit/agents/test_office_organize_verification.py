import json
from pathlib import Path

from agents.office.nodes import _verify_organize_materialization


def _write_operation(workspace_root: Path, action: str, src: Path, dst: str) -> None:
    plan_path = workspace_root / "operations-plan.json"
    with open(plan_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "action": action,
            "src": str(src),
            "dst": dst,
        }) + "\n")


def test_verify_organize_materialization_rejects_duplicate_copy(tmp_path):
    """Verification fails when the same source file is copied more than once."""
    source_root = tmp_path / "source" / "2026"
    essay_dir = source_root / "0103"
    essay_dir.mkdir(parents=True)
    essay = essay_dir / "1.txt"
    essay.write_text(">>> Student Yan\n", encoding="utf-8")

    workspace_root = tmp_path / "workspace"
    artifacts_dir = workspace_root / "artifacts"
    output_root = artifacts_dir / "organized-output" / "files" / "students" / "Yan" / "2026-01"
    output_root.mkdir(parents=True)
    (output_root / "0103-1.txt").write_text(">>> Student Yan\n", encoding="utf-8")
    _write_operation(artifacts_dir, "copy_file", essay, str(output_root / "0103-1.txt"))
    _write_operation(artifacts_dir, "copy_file", essay, str(artifacts_dir / "organized-output" / "files" / "students" / "Ethan" / "2026-01" / "0103-1.txt"))

    errors = _verify_organize_materialization("workspace", str(artifacts_dir), [str(source_root)])

    assert any("copied more than once" in error for error in errors)


def test_verify_organize_materialization_rejects_missing_source_file(tmp_path):
    """Verification fails when some source files were never copied."""
    source_root = tmp_path / "source" / "2026"
    jan_dir = source_root / "0103"
    feb_dir = source_root / "0207"
    jan_dir.mkdir(parents=True)
    feb_dir.mkdir(parents=True)
    jan_essay = jan_dir / "1.txt"
    feb_essay = feb_dir / "1.txt"
    jan_essay.write_text(">>> Student Liam\n", encoding="utf-8")
    feb_essay.write_text(">>> Student Liam\n", encoding="utf-8")

    workspace_root = tmp_path / "workspace"
    artifacts_dir = workspace_root / "artifacts"
    output_root = artifacts_dir / "organized-output" / "files" / "students" / "Liam" / "2026-01"
    output_root.mkdir(parents=True)
    (output_root / "0103-1.txt").write_text(">>> Student Liam\n", encoding="utf-8")
    _write_operation(artifacts_dir, "copy_file", jan_essay, str(output_root / "0103-1.txt"))

    errors = _verify_organize_materialization("workspace", str(artifacts_dir), [str(source_root)])

    assert any("were not copied" in error for error in errors)
