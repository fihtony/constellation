"""Pin the prompt builders' target-path output.

The Office agent's prompt builders and the delivery-verification
helper must compute the same target path. These tests pin the
prompt text for each capability x mode combination.
"""
from __future__ import annotations

import pytest

from agents.office.nodes import (
    _build_analyze_prompt,
    _build_organize_prompt,
    _build_summarize_prompt,
)


def test_analyze_prompt_inplace_dir_contains_inside_source_path(tmp_path):
    """Bug A: the prompt must advertise the target INSIDE the source
    directory, not as a sibling.

    The helper target_with_suffix relies on os.path.isdir(), so we
    need a real directory on the filesystem (tmp_path) rather than a
    hard-coded non-existent path like "/data".
    """
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    prompt = _build_analyze_prompt([str(source_dir)], "inplace", "/app/userdata")
    expected = str(source_dir / "data.analysis.md")
    assert expected in prompt
    # The buggy old form would have been sibling: /tmp/.../data.analysis.md
    # We assert the sibling form is NOT present as a standalone target path.
    sibling_form = f"{tmp_path}.analysis.md"
    assert sibling_form not in prompt


def test_analyze_prompt_workspace_dir_uses_basename_only(tmp_path):
    source_dir = tmp_path / "data"
    source_dir.mkdir()
    prompt = _build_analyze_prompt([str(source_dir)], "workspace", "/app/userdata")
    assert "data.analysis.md" in prompt
    # In workspace mode, the prompt tells the LLM to use write_workspace
    # with a bare filename. The absolute path of the deliverable is
    # decided by the helper downstream; the prompt just needs the
    # filename, not the full path.
    assert "Target filename" in prompt


def test_analyze_prompt_inplace_file_target_next_to_source(tmp_path):
    source_file = tmp_path / "sales.csv"
    source_file.write_text("a,b\n1,2\n", encoding="utf-8")
    prompt = _build_analyze_prompt([str(source_file)], "inplace", "/app/userdata")
    expected = str(tmp_path / "sales.csv.analysis.md")
    assert expected in prompt


def test_organize_prompt_inplace_does_not_contain_literal_placeholder():
    """Bug B: the inplace branch used to ship a literal
    `{source_folder}` token that was never interpolated."""
    prompt = _build_organize_prompt(["/data"], "inplace", "/app/userdata")
    assert "{source_folder}" not in prompt


def test_organize_prompt_inplace_advertises_source_dir_path():
    prompt = _build_organize_prompt(["/data"], "inplace", "/app/userdata")
    assert "/data/organization-plan.md" in prompt


def test_summarize_prompt_inplace_file_target_next_to_source(tmp_path):
    source_file = tmp_path / "sales.csv"
    source_file.write_text("a,b\n1,2\n", encoding="utf-8")
    prompt = _build_summarize_prompt([str(source_file)], "inplace", "/app/userdata")
    expected = str(tmp_path / "sales.csv.summary.md")
    assert expected in prompt


def test_summarize_prompt_inplace_multi_file_combined_in_source_dir():
    """When summarizing multiple files in inplace mode, the combined
    report must be advertised inside the source directory, not in
    artifacts."""
    prompt = _build_summarize_prompt(
        ["/data/a.txt", "/data/b.txt"], "inplace", "/app/userdata"
    )
    assert "/data/a.txt.summary.md" in prompt
    assert "/data/b.txt.summary.md" in prompt
    assert "combined-summary.md" in prompt
    # The combined path must reference /data/, not /app/artifacts/.
    assert "/data/combined-summary.md" in prompt


def test_summarize_prompt_workspace_combined_in_artifacts():
    prompt = _build_summarize_prompt(
        ["/data/a.txt", "/data/b.txt"], "workspace", "/app/userdata"
    )
    assert "combined-summary.md" in prompt
    # The combined-summary label must keep its "Combined report"
    # prefix so the LLM can distinguish it from the per-file
    # "Target filename:" lines.
    assert "Combined report target filename: combined-summary.md" in prompt


# ---------------------------------------------------------------------------
# Regression guards: the prompt must not nudge the LLM to rename the
# deliverable based on inner files. Bug observed in task e1a19c246f4b:
# the analyze prompt's "Preserve the full original filename" line made
# the LLM write `/data/csv/sales_data.csv.analysis.md` (named after the
# inner file) instead of `/data/csv/csv.analysis.md` (named after the
# source directory basename, which is what the helper + verifier expect).
# ---------------------------------------------------------------------------


def test_analyze_prompt_does_not_tell_llm_to_preserve_inner_filename(tmp_path):
    """Pin that the misleading 'Preserve the full original filename,
    including its extension' instruction is gone from the analyze prompt.

    For directory inputs that wording was hijacking the LLM into naming
    the report after an inner file (e.g. ``sales_data.csv.analysis.md``)
    rather than after the source directory basename (``csv.analysis.md``).
    """
    source_dir = tmp_path / "csv"
    source_dir.mkdir()
    (source_dir / "sales_data.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    prompt = _build_analyze_prompt([str(source_dir)], "inplace", "/app/userdata")
    assert "Preserve the full original filename" not in prompt
    assert "before appending `.analysis.md`" not in prompt


def test_analyze_prompt_tells_llm_to_use_exact_target(tmp_path):
    """The replacement instruction must tell the LLM to use the
    authoritative target path verbatim and NOT derive a different
    filename from files inside the source folder.
    """
    source_dir = tmp_path / "csv"
    source_dir.mkdir()
    prompt = _build_analyze_prompt([str(source_dir)], "inplace", "/app/userdata")
    assert "EXACT target" in prompt
    # The replacement instruction must explicitly warn against
    # renaming based on inner files (this is what the previous wording
    # accidentally encouraged). The wording varies but the key concept
    # must be present: do not use the name of any inner file.
    lower = prompt.lower()
    assert "do not use" in lower
    assert "inside the source" in lower


def test_analyze_prompt_clarifies_directory_target_naming(tmp_path):
    """For directory sources the target filename is the BASENAME of
    the directory, not any filename from inside it. The prompt must
    state that explicitly so the LLM does not regress to the inner-file
    rename.
    """
    source_dir = tmp_path / "csv"
    source_dir.mkdir()
    prompt = _build_analyze_prompt([str(source_dir)], "inplace", "/app/userdata")
    # The clarification line must reference the directory → basename rule
    # so the LLM understands why the target is ``csv.analysis.md``
    # even though the directory contains other files.
    assert "directory" in prompt.lower()
    assert "basename" in prompt.lower() or "BASENAME" in prompt


def test_summarize_prompt_does_not_tell_llm_to_preserve_inner_filename(tmp_path):
    """Same regression guard for the summarize prompt: the misleading
    'Preserve the full original filename' line must be gone.
    """
    source_dir = tmp_path / "docs"
    source_dir.mkdir()
    prompt = _build_summarize_prompt([str(source_dir)], "inplace", "/app/userdata")
    assert "Preserve the full original filename" not in prompt
    assert "before appending `.summary.md`" not in prompt


def test_summarize_prompt_tells_llm_to_use_exact_target(tmp_path):
    """Replacement instruction must tell the LLM to use the
    authoritative target path verbatim for summaries too.
    """
    source_dir = tmp_path / "docs"
    source_dir.mkdir()
    prompt = _build_summarize_prompt([str(source_dir)], "inplace", "/app/userdata")
    assert "EXACT target" in prompt
    lower = prompt.lower()
    assert "do not use" in lower
    assert "inside the source" in lower


def test_summarize_prompt_clarifies_directory_target_naming(tmp_path):
    """For directory sources the summary filename is the BASENAME of
    the directory, not any filename from inside it.
    """
    source_dir = tmp_path / "docs"
    source_dir.mkdir()
    prompt = _build_summarize_prompt([str(source_dir)], "inplace", "/app/userdata")
    assert "directory" in prompt.lower()
    assert "basename" in prompt.lower() or "BASENAME" in prompt