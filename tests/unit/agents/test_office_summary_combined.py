import unicodedata

from agents.office.nodes import (
    _canonicalize_summary_output_filenames,
    _ensure_combined_summary_exact_filenames,
)


def test_ensure_combined_summary_exact_filenames_preserves_source_unicode(tmp_path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    source_a = tmp_path / "Activite\u0301s midi - Master - Mode\u0300le.pdf"
    source_b = tmp_path / "Bibliothe\u0300que.txt"
    source_a.write_text("pdf", encoding="utf-8")
    source_b.write_text("txt", encoding="utf-8")

    combined_path = artifacts_dir / "combined-summary.md"
    combined_path.write_text(
        "# Combined Summary: All Documents\n\n"
        f"- {unicodedata.normalize('NFC', source_a.name)}\n"
        f"- {unicodedata.normalize('NFC', source_b.name)}\n",
        encoding="utf-8",
    )

    _ensure_combined_summary_exact_filenames(
        [str(source_a), str(source_b)],
        "workspace",
        str(artifacts_dir),
    )

    combined_text = combined_path.read_text(encoding="utf-8")
    assert source_a.name in combined_text
    assert source_b.name in combined_text
    assert "## Exact Source Filenames" in combined_text


def test_canonicalize_summary_output_filenames_repairs_near_miss_names(tmp_path):
    artifacts_dir = tmp_path / "artifacts"
    artifacts_dir.mkdir()

    source_a = tmp_path / "Rapport-de-decembre-2025.pdf"
    source_b = tmp_path / "Rapport-de-fevrier-2026.pdf"
    source_a.write_text("a", encoding="utf-8")
    source_b.write_text("b", encoding="utf-8")

    drifted_output = artifacts_dir / "Rapport-de-december-2025.pdf.summary.md"
    exact_output = artifacts_dir / f"{source_b.name}.summary.md"
    drifted_output.write_text("summary a", encoding="utf-8")
    exact_output.write_text("summary b", encoding="utf-8")

    repaired = _canonicalize_summary_output_filenames(
        [str(source_a), str(source_b)],
        "workspace",
        str(artifacts_dir),
    )

    expected_a = artifacts_dir / f"{source_a.name}.summary.md"
    assert repaired == [str(expected_a)]
    assert expected_a.exists()
    assert expected_a.read_text(encoding="utf-8") == "summary a"
    assert not drifted_output.exists()
    assert exact_output.exists()