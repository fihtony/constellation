import unicodedata

from agents.office.nodes import _ensure_combined_summary_exact_filenames


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