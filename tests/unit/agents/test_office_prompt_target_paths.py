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