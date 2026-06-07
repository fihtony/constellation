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


def test_verify_organize_materialization_accepts_dimension_tool_layout(tmp_path):
    """Block 2: verification now accepts whatever layout the bounded
    dimension tool produced. Canonical destination honours
    ``suggested_destination`` when present, else falls back to
    ``<output_root>/<relative_path>``. Entity inference is gone."""
    source_root = tmp_path / "source"
    essay_dir = source_root / "0103"
    essay_dir.mkdir(parents=True)
    essay = essay_dir / "1.txt"
    essay.write_text("Hello, world.", encoding="utf-8")

    workspace_root = tmp_path / "workspace"
    artifacts_dir = workspace_root / "artifacts"
    output_root = artifacts_dir / "organized-output" / "files" / "0103"
    output_root.mkdir(parents=True)
    (output_root / "1.txt").write_text("Hello, world.", encoding="utf-8")
    _write_operation(artifacts_dir, "copy_file", essay, str(output_root / "1.txt"))

    errors = _verify_organize_materialization("workspace", str(artifacts_dir), [str(source_root)])

    assert errors == []


def test_verify_organize_materialization_rejects_missing_source_file(tmp_path):
    """Verification fails when some canonical outputs were never materialized."""
    source_root = tmp_path / "source"
    jan_dir = source_root / "0103"
    feb_dir = source_root / "0207"
    jan_dir.mkdir(parents=True)
    feb_dir.mkdir(parents=True)
    jan_essay = jan_dir / "1.txt"
    feb_essay = feb_dir / "1.txt"
    jan_essay.write_text("Hello, jan.", encoding="utf-8")
    feb_essay.write_text("Hello, feb.", encoding="utf-8")

    workspace_root = tmp_path / "workspace"
    artifacts_dir = workspace_root / "artifacts"
    output_root = artifacts_dir / "organized-output" / "files" / "0103"
    output_root.mkdir(parents=True)
    (output_root / "1.txt").write_text("Hello, jan.", encoding="utf-8")
    _write_operation(artifacts_dir, "copy_file", jan_essay, str(output_root / "1.txt"))

    errors = _verify_organize_materialization("workspace", str(artifacts_dir), [str(source_root)])

    assert any("Missing canonical organized files" in error for error in errors)


def test_verify_organize_materialization_rejects_unexpected_extra_files(tmp_path):
    """Verification fails when stray files remain under organized-output."""
    source_root = tmp_path / "source"
    essay_dir = source_root / "0103"
    essay_dir.mkdir(parents=True)
    essay = essay_dir / "1.txt"
    essay.write_text("Hello, world.", encoding="utf-8")

    workspace_root = tmp_path / "workspace"
    artifacts_dir = workspace_root / "artifacts"
    output_root = artifacts_dir / "organized-output" / "files" / "0103"
    output_root.mkdir(parents=True)
    (output_root / "1.txt").write_text("Hello, world.", encoding="utf-8")
    stray_root = artifacts_dir / "organized-output" / "files" / "extra"
    stray_root.mkdir(parents=True)
    (stray_root / "ghost.txt").write_text("Ghost.", encoding="utf-8")
    _write_operation(artifacts_dir, "copy_file", essay, str(output_root / "1.txt"))

    errors = _verify_organize_materialization("workspace", str(artifacts_dir), [str(source_root)])

    assert any("Unexpected organized files present" in error for error in errors)


def test_repair_missing_organize_outputs_materializes_skipped_sources(tmp_path):
    """Canonical repair re-materializes any source file that is missing
    from the dimension-tool output. The destination is
    ``<output_root>/<relative_path>`` (no entity/date bucketing)."""
    source_root = tmp_path / "source"
    jan_dir = source_root / "0103"
    feb_dir = source_root / "0221"
    jan_dir.mkdir(parents=True)
    feb_dir.mkdir(parents=True)
    essay1 = jan_dir / "1.txt"
    essay2 = jan_dir / "4.txt"
    essay3 = feb_dir / "1.txt"
    for essay, content in (
        (essay1, "Hello, essay1."),
        (essay2, "Hello, essay2."),
        (essay3, "Hello, essay3."),
    ):
        essay.write_text(content, encoding="utf-8")

    workspace_root = tmp_path / "workspace"
    artifacts_dir = workspace_root / "artifacts"
    artifacts_dir.mkdir(parents=True)
    # An empty operations-plan.json is required for the repair function
    # to engage; create a stub so every source is repaired.
    (artifacts_dir / "operations-plan.json").write_text("", encoding="utf-8")

    repaired = _repair_missing_organize_outputs("workspace", str(artifacts_dir), [str(source_root)])

    assert str(essay1.resolve()) in repaired
    assert str(essay2.resolve()) in repaired
    assert str(essay3.resolve()) in repaired
    # All sources are now materialized under their source-relative path.
    assert (artifacts_dir / "organized-output" / "files" / "0103" / "1.txt").exists()
    assert (artifacts_dir / "organized-output" / "files" / "0103" / "4.txt").exists()
    assert (artifacts_dir / "organized-output" / "files" / "0221" / "1.txt").exists()

    errors = _verify_organize_materialization("workspace", str(artifacts_dir), [str(source_root)])
    assert errors == []


def test_repair_missing_organize_outputs_uses_dimension_tool_layout(tmp_path):
    """Block 2: repair canonicalises to the dimension-tool layout, not
    to entity/date buckets. A file at ``<output_root>/0103/1.txt`` is
    repaired in place (not re-bucketed by entity/date)."""
    source_root = tmp_path / "source"
    essay_dir = source_root / "0103"
    essay_dir.mkdir(parents=True)
    essay = essay_dir / "1.txt"
    essay.write_text("Hello, world.", encoding="utf-8")

    workspace_root = tmp_path / "workspace"
    artifacts_dir = workspace_root / "artifacts"
    wrong_root = artifacts_dir / "organized-output" / "files" / "unknown"
    wrong_root.mkdir(parents=True)
    (wrong_root / "1.txt").write_text("Hello, world.", encoding="utf-8")
    _write_operation(artifacts_dir, "copy_file", essay, str(wrong_root / "1.txt"))

    repaired = _repair_missing_organize_outputs("workspace", str(artifacts_dir), [str(source_root)])

    assert str(essay.resolve()) in repaired
    assert (artifacts_dir / "organized-output" / "files" / "0103" / "1.txt").exists()

    errors = _verify_organize_materialization("workspace", str(artifacts_dir), [str(source_root)])
    assert errors == []
