import json
from pathlib import Path

from agents.office.nodes import _repair_missing_organize_outputs, _verify_organize_materialization


def _write_operation(workspace_root: Path, action: str, src: Path, dst: str) -> None:
    plan_path = workspace_root / "operations-plan.json"
    with open(plan_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({
            "action": action,
            "src": str(src),
            "dst": dst,
        }) + "\n")


def test_verify_organize_materialization_rejects_missing_canonical_destination(tmp_path):
    """Verification fails when files are materialized at the wrong canonical destination."""
    source_root = tmp_path / "source" / "2026"
    essay_dir = source_root / "0103"
    essay_dir.mkdir(parents=True)
    essay = essay_dir / "1.txt"
    essay.write_text(">>> Student Yan\n", encoding="utf-8")

    workspace_root = tmp_path / "workspace"
    artifacts_dir = workspace_root / "artifacts"
    output_root = artifacts_dir / "organized-output" / "files" / "Ethan" / "2026-01"
    output_root.mkdir(parents=True)
    (output_root / "0103-1.txt").write_text(">>> Student Yan\n", encoding="utf-8")
    _write_operation(artifacts_dir, "copy_file", essay, str(output_root / "0103-1.txt"))

    errors = _verify_organize_materialization("workspace", str(artifacts_dir), [str(source_root)])

    assert any("Missing canonical organized files" in error for error in errors)
    assert any("Unexpected organized files present" in error for error in errors)


def test_verify_organize_materialization_rejects_missing_source_file(tmp_path):
    """Verification fails when some canonical outputs were never materialized."""
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

    assert any("Missing canonical organized files" in error for error in errors)


def test_verify_organize_materialization_rejects_unexpected_extra_files(tmp_path):
    """Verification fails when stray files remain under organized-output."""
    source_root = tmp_path / "source" / "2026"
    essay_dir = source_root / "0103"
    essay_dir.mkdir(parents=True)
    essay = essay_dir / "1.txt"
    essay.write_text(">>> Student Yan\n", encoding="utf-8")

    workspace_root = tmp_path / "workspace"
    artifacts_dir = workspace_root / "artifacts"
    output_root = artifacts_dir / "organized-output" / "files" / "Yan" / "2026-01"
    output_root.mkdir(parents=True)
    (output_root / "0103-1.txt").write_text(">>> Student Yan\n", encoding="utf-8")
    stray_root = artifacts_dir / "organized-output" / "files" / "Ethan" / "2026-01"
    stray_root.mkdir(parents=True)
    (stray_root / "input-0-0103-1.txt").write_text(">>> Student Yan\n", encoding="utf-8")
    _write_operation(artifacts_dir, "copy_file", essay, str(output_root / "0103-1.txt"))

    errors = _verify_organize_materialization("workspace", str(artifacts_dir), [str(source_root)])

    assert any("Unexpected organized files present" in error for error in errors)


def test_repair_missing_organize_outputs_materializes_skipped_sources(tmp_path):
    """Canonical repair should rewrite wrong destinations and materialize missing files."""
    source_root = tmp_path / "source" / "2026"
    jan_dir = source_root / "0103"
    feb_dir = source_root / "0221"
    jan_dir.mkdir(parents=True)
    feb_dir.mkdir(parents=True)
    essay1 = jan_dir / "1.txt"
    essay2 = jan_dir / "4.txt"
    essay3 = feb_dir / "1.txt"
    essay1.write_text(">>> Student Yan\n", encoding="utf-8")
    essay2.write_text(">>> Student Ethan\n", encoding="utf-8")
    essay3.write_text(">>> Student Ethan\n", encoding="utf-8")

    workspace_root = tmp_path / "workspace"
    artifacts_dir = workspace_root / "artifacts"
    output_root = artifacts_dir / "organized-output" / "files" / "Ethan" / "2026-01"
    output_root.mkdir(parents=True)
    (output_root / "0103-1.txt").write_text(">>> Student Ethan\n", encoding="utf-8")
    _write_operation(artifacts_dir, "copy_file", essay1, str(output_root / "0103-1.txt"))
    wrong_root = artifacts_dir / "organized-output" / "files" / "Yan" / "2026-01"
    wrong_root.mkdir(parents=True)
    (wrong_root / "0103-4.txt").write_text(">>> Student Ethan\n", encoding="utf-8")
    _write_operation(artifacts_dir, "copy_file", essay2, str(wrong_root / "0103-4.txt"))
    weird_root = artifacts_dir / "organized-output" / "files" / "Ethan" / "2026-02"
    weird_root.mkdir(parents=True)
    (weird_root / "input-0-0221-1.txt").write_text(">>> Student Ethan\n", encoding="utf-8")
    _write_operation(artifacts_dir, "copy_file", essay3, str(weird_root / "input-0-0221-1.txt"))

    repaired = _repair_missing_organize_outputs("workspace", str(artifacts_dir), [str(source_root)])

    assert str(essay1.resolve()) in repaired
    assert str(essay2.resolve()) in repaired
    assert str(essay3.resolve()) in repaired
    assert (artifacts_dir / "organized-output" / "files" / "Yan" / "2026-01" / "0103-1.txt").exists()
    assert (artifacts_dir / "organized-output" / "files" / "Ethan" / "2026-01" / "0103-4.txt").exists()
    assert (artifacts_dir / "organized-output" / "files" / "Ethan" / "2026-02" / "0221-1.txt").exists()
    assert not (artifacts_dir / "organized-output" / "files" / "Ethan" / "2026-01" / "0103-1.txt").exists()
    assert not (artifacts_dir / "organized-output" / "files" / "Yan" / "2026-01" / "0103-4.txt").exists()
    assert not (artifacts_dir / "organized-output" / "files" / "Ethan" / "2026-02" / "input-0-0221-1.txt").exists()

    errors = _verify_organize_materialization("workspace", str(artifacts_dir), [str(source_root)])
    assert errors == []
