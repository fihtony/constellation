"""Tests for the bounded folder summarize flow's tool-free contract.

The user reported (task-555101087925) that, in workspace mode, the
per-document LLM call inside the bounded folder summarize flow
sometimes dropped stray ``<basename>-summary.md`` files directly
under the office workspace root — one level up from the agreed
``artifacts_dir`` delivery folder.  The two observed strays were
named without the source's ``.pdf`` extension (``RECUPERATION-...-summary.md``
instead of ``RECUPERATION-...pdf.summary.md``) and one even had a
typo (``RECUPEERATION-...``).  Re-running the same task did not
reproduce the symptom — the LLM is non-deterministic about whether
it reaches for its native ``Write`` tool at all.

Root cause: the bounded flow calls the LLM via
``runtime.run(prompt, system_prompt, cwd=workspace_root)`` to
produce a per-document summary.  ``claude_code.run`` shells out to
the local ``claude --print --dangerously-skip-permissions`` CLI,
which by default gives the LLM its full native tool surface
(``Read``/``Write``/``Edit``/``Bash``/...).  The system prompt tells
the LLM to not use those tools, but the per-document prompt opens
with "Write an English-only Markdown summary" — the verb "Write"
was reliably triggering the LLM's native ``Write`` tool, dropping a
file at ``<cwd>/<filename>`` = ``<workspace_root>/<filename>`` =
the parent of the agreed ``artifacts_dir`` delivery folder.

Methodology fix (two layers, no test-specific hard-coding):

1. ``runtime.run`` now accepts an optional ``disallowed_tools``
   parameter; the ``claude_code`` backend honours it by passing
   ``--tools ""`` to the CLI.  The bounded folder summarize flow
   opts in with ``disallowed_tools=["*"]`` so the LLM cannot reach
   any native tool — the contract is structural instead of
   advisory.

2. The bounded folder summarize flow also runs a defensive sweep
   (``_sweep_stray_summary_files``) after materialising every
   deliverable, removing any ``*.summary.md`` or
   ``combined-summary.md`` that landed directly in
   ``workspace_root`` instead of inside ``artifacts_dir``.  The
   Python code has already written the canonical copy, so any
   stray is redundant.

These tests pin both layers: the LLM call is given
``disallowed_tools`` and the sweep removes the strays the LLM
might still produce in the rare non-compliant case.  Both must
hold in workspace mode and inplace mode, and the inplace case
must remain a no-op when ``workspace_root == artifacts_dir``.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from agents.office.nodes import (
    _run_bounded_folder_summarize,
    _summarize_payload_with_runtime,
    _sweep_stray_summary_files,
)


# ---------------------------------------------------------------------------
# Layer 1: the LLM call is given disallowed_tools=["*"]
# ---------------------------------------------------------------------------


class _DisallowedToolsRecorder:
    """A fake runtime that records the kwargs of ``run()`` and
    returns a text response (no tool calls)."""

    last_call = None

    def run(self, *args, **kwargs):
        _DisallowedToolsRecorder.last_call = (args, kwargs)
        return {
            "raw_response": (
                "# Summary: fake.txt\n\n## Document Info\n- tiny\n\n"
                "## Key Points\n- a\n\n## Executive Summary\nshort"
            ),
            "summary": "",
        }


def test_summarize_payload_with_runtime_passes_disallowed_tools():
    """The bounded folder summarize's LLM call must disable native tools.

    ``_summarize_payload_with_runtime`` is the per-document LLM call
    inside ``_run_bounded_folder_summarize``.  Without
    ``disallowed_tools=["*"]`` the local ``claude`` CLI gives the
    LLM access to ``Write``/``Edit``/``Bash``/..., which the LLM
    can use to drop a stray file at ``<cwd>/<filename>`` —
    task-555101087925's symptom.
    """
    runtime = _DisallowedToolsRecorder()
    payload = {
        "source_path": "/tmp/fake.txt",
        "content": "hello world",
        "metadata": {},
    }
    text = _summarize_payload_with_runtime(
        runtime,
        path="/tmp/fake.txt",
        payload=payload,
        system_prompt="sys",
        cwd="/tmp",
        plugin_manager=None,
    )
    assert "Summary: fake.txt" in text
    args, kwargs = _DisallowedToolsRecorder.last_call
    assert kwargs.get("disallowed_tools") == ["*"], (
        "the bounded folder summarize flow must opt out of all "
        "native tools so the LLM cannot reach the Write tool; "
        f"got kwargs={kwargs!r}"
    )


def test_summarize_payload_with_runtime_prompt_does_not_say_write():
    """The per-document prompt must not use the verb "Write" to
    describe producing text — the LLM can interpret that as a
    ``Write`` tool call and reach for the filesystem."""
    runtime = _DisallowedToolsRecorder()
    payload = {
        "source_path": "/tmp/produce.txt",
        "content": "hello",
        "metadata": {},
    }
    _summarize_payload_with_runtime(
        runtime,
        path="/tmp/produce.txt",
        payload=payload,
        system_prompt="sys",
        cwd="/tmp",
        plugin_manager=None,
    )
    args, _ = _DisallowedToolsRecorder.last_call
    user_prompt = args[0]
    # The verb "Write" is what triggered the stray file in
    # task-555101087925 — the LLM took it as a tool call.  We
    # replaced it with "Produce" / "Respond with ... only" so the
    # prompt reads as a pure text instruction even if a future
    # backend change accidentally drops the disallowed_tools guard.
    assert "Write an English-only" not in user_prompt, (
        "the bounded folder summarize prompt opens with 'Write ...' "
        "which the LLM interprets as a Write tool call; replace "
        "with a non-tool verb (e.g. 'Produce' or 'Respond with')"
    )
    assert "Respond with the summary text only" in user_prompt


# ---------------------------------------------------------------------------
# Layer 2: the defensive sweep removes strays from workspace_root
# ---------------------------------------------------------------------------


def test_sweep_stray_summary_files_removes_workspace_root_strays(tmp_path):
    """The sweep removes ``*.summary.md`` (and ``combined-summary.md``)
    that landed directly in ``workspace_root`` instead of inside
    ``artifacts_dir``."""
    workspace_root = tmp_path / "office"
    artifacts_dir = workspace_root / "artifacts"
    artifacts_dir.mkdir(parents=True)
    # Canonical copy at the correct location:
    (artifacts_dir / "RECUPERATION-ET-ENRICHISSEMENT-25-26.pdf.summary.md").write_text(
        "canonical", encoding="utf-8"
    )
    (artifacts_dir / "combined-summary.md").write_text("combined", encoding="utf-8")
    # Strays at workspace_root (the parent of artifacts_dir):
    (workspace_root / "RECUPERATION-ET-ENRICHISSEMENT-25-26-summary.md").write_text(
        "stray", encoding="utf-8"
    )
    (workspace_root / "RECUPEERATION-ET-ENRICHISSEMENT-25-26-summary.md").write_text(
        "typo stray", encoding="utf-8"
    )
    (workspace_root / "combined-summary.md").write_text("stray combined", encoding="utf-8")
    # Non-summary files are not touched:
    (workspace_root / "agent.log").write_text("log", encoding="utf-8")
    (workspace_root / "task-report.json").write_text("{}", encoding="utf-8")

    removed = _sweep_stray_summary_files(
        workspace_root=str(workspace_root),
        artifacts_dir=str(artifacts_dir),
    )
    assert sorted(removed) == sorted(
        [
            str(workspace_root / "RECUPERATION-ET-ENRICHISSEMENT-25-26-summary.md"),
            str(workspace_root / "RECUPEERATION-ET-ENRICHISSEMENT-25-26-summary.md"),
            str(workspace_root / "combined-summary.md"),
        ]
    ), f"sweep must remove all three strays; got {removed!r}"
    # Canonical copies survive:
    assert (artifacts_dir / "RECUPERATION-ET-ENRICHISSEMENT-25-26.pdf.summary.md").exists()
    assert (artifacts_dir / "combined-summary.md").exists()
    # Non-summary files survive:
    assert (workspace_root / "agent.log").exists()
    assert (workspace_root / "task-report.json").exists()


def test_sweep_stray_summary_files_no_op_in_inplace_when_workspace_root_equals_artifacts(tmp_path):
    """In inplace mode with a directory source, ``workspace_root`` may
    equal ``artifacts_dir`` (the source folder IS the output root).
    The sweep must be a no-op there — it would otherwise delete the
    canonical per-document files."""
    source = tmp_path / "source"
    source.mkdir()
    # Canonical per-document files (the Python write would put them here):
    (source / "doc1.pdf.summary.md").write_text("a", encoding="utf-8")
    (source / "doc2.pdf.summary.md").write_text("b", encoding="utf-8")
    (source / "combined-summary.md").write_text("combined", encoding="utf-8")

    removed = _sweep_stray_summary_files(
        workspace_root=str(source),
        artifacts_dir=str(source),
    )
    assert removed == [], (
        "inplace mode where workspace_root == artifacts_dir must not "
        f"delete any summary files; got {removed!r}"
    )
    # Canonical files still exist:
    assert (source / "doc1.pdf.summary.md").exists()
    assert (source / "doc2.pdf.summary.md").exists()
    assert (source / "combined-summary.md").exists()


def test_sweep_stray_summary_files_ignores_files_inside_artifacts_dir(tmp_path):
    """The sweep must never touch files that live inside ``artifacts_dir``,
    even if a future change makes ``workspace_root`` a sibling of
    ``artifacts_dir`` (workspace mode layout)."""
    workspace_root = tmp_path / "office"
    artifacts_dir = workspace_root / "artifacts"
    artifacts_dir.mkdir(parents=True)
    (artifacts_dir / "doc.summary.md").write_text("canonical", encoding="utf-8")
    # A deeply nested summary inside artifacts_dir must not be removed:
    nested = artifacts_dir / "subdir"
    nested.mkdir()
    (nested / "nested.summary.md").write_text("nested canonical", encoding="utf-8")

    removed = _sweep_stray_summary_files(
        workspace_root=str(workspace_root),
        artifacts_dir=str(artifacts_dir),
    )
    assert removed == []
    assert (artifacts_dir / "doc.summary.md").exists()
    assert (nested / "nested.summary.md").exists()


# ---------------------------------------------------------------------------
# End-to-end: the bounded folder summarize flow never leaves strays
# ---------------------------------------------------------------------------


def _build_fake_runtime():
    """A fake runtime that returns a fixed per-document summary text.

    Simulates the LLM *also* dropping a stray file at ``<cwd>/<name>``
    (the bug behaviour) — the bounded flow's defensive sweep must
    remove it.
    """

    def _run(prompt, *args, **kwargs):
        # Pick the source filename out of the prompt so the LLM "write"
        # has a plausible destination (this is what the buggy LLM
        # behaviour looked like in task-555101087925).
        cwd = kwargs.get("cwd") or "."
        import re
        m = re.search(r"Filename:\s*(\S+)", prompt)
        if m:
            # Reproduce the bug: the LLM also calls the native
            # ``Write`` tool and drops the file at ``<cwd>/<name>``
            # (one level up from the agreed artifacts_dir).  Use a
            # ``-summary.md`` suffix to match the actual bug
            # behaviour (the LLM dropped the ``.pdf`` part of
            # ``RECUPERATION-ET-ENRICHISSEMENT-25-26.pdf`` and
            # produced ``RECUPERATION-...-summary.md``).
            name = m.group(1)
            bad_name = name + "-summary.md"
            bad_target = os.path.join(cwd, bad_name)
            os.makedirs(os.path.dirname(bad_target) or ".", exist_ok=True)
            with open(bad_target, "w", encoding="utf-8") as fh:
                fh.write("# Summary: " + name + "\n\nstray content\n")
        return {
            "raw_response": (
                "# Summary: placeholder\n\n## Document Info\n- fake\n\n"
                "## Key Points\n- one\n\n## Executive Summary\nshort."
            ),
            "summary": "",
        }

    runtime = MagicMock()
    runtime.run.side_effect = _run
    return runtime


def test_bounded_folder_summarize_sweeps_strays_from_workspace_root(tmp_path, monkeypatch):
    """End-to-end: the bounded folder summarize flow's defensive
    sweep removes any stray summary file that landed in
    ``workspace_root`` (the parent of ``artifacts_dir``) — even when
    the LLM pretends to use its native ``Write`` tool.

    This pins task-555101087925's contract: the office task never
    leaves stray summary files outside the agreed delivery folder,
    regardless of the LLM's non-deterministic tool use.
    """
    # Stub out the doc payload reader so the test does not need a
    # real PDF/text on disk.
    from agents.office import nodes as office_nodes

    def _fake_read(path):
        return {
            "source_path": path,
            "content": f"content of {os.path.basename(path)}",
            "metadata": {"pages": 1, "type": "TXT"},
        }

    monkeypatch.setattr(office_nodes, "_read_summary_payload", _fake_read)

    # Lay out a tiny source tree with three files.
    src = tmp_path / "stlouis"
    src.mkdir()
    (src / "alpha.txt").write_text("alpha content", encoding="utf-8")
    (src / "beta.txt").write_text("beta content", encoding="utf-8")
    (src / "gamma.txt").write_text("gamma content", encoding="utf-8")

    workspace_root = tmp_path / "office"
    artifacts_dir = workspace_root / "artifacts"
    artifacts_dir.mkdir(parents=True)

    # Stub out the LLM-targeted summarizer; the test does not depend
    # on a live LLM.  We use a runtime whose ``run`` also drops a
    # stray file at ``<cwd>/<name>`` to reproduce the buggy behaviour.
    state = {
        "_plugin_manager": None,
        "workspace_root": str(workspace_root),
    }
    result = _run_bounded_folder_summarize(
        state,
        runtime=_build_fake_runtime(),
        validated_paths=[str(src / "alpha.txt"), str(src / "beta.txt"), str(src / "gamma.txt")],
        output_mode="workspace",
        artifacts_dir=str(artifacts_dir),
        system_prompt="sys",
    )
    # The bounded flow succeeded.
    assert result.success is True, (
        f"bounded folder summarize failed: {result.summary!r}"
    )

    # Canonical files are at the agreed location:
    for name in ("alpha.txt.summary.md", "beta.txt.summary.md", "gamma.txt.summary.md"):
        assert (artifacts_dir / name).exists(), (
            f"missing canonical deliverable {name!r} under artifacts_dir"
        )
    assert (artifacts_dir / "combined-summary.md").exists()

    # The stray files the LLM "wrote" to workspace_root are gone.
    strays = sorted(p.name for p in workspace_root.iterdir())
    assert "alpha.txt.summary.md" not in strays, (
        "stray alpha.txt.summary.md in workspace_root — the defensive "
        "sweep did not run or did not clean the parent directory"
    )
    assert "combined-summary.md" not in strays, (
        "stray combined-summary.md in workspace_root — the defensive "
        "sweep did not run or did not clean the parent directory"
    )
    # The only files in workspace_root should be the ``artifacts`` dir
    # itself plus any non-summary files (none in this test).
    assert strays == ["artifacts"], (
        f"workspace_root should contain only the artifacts dir after "
        f"the sweep; got {strays!r}"
    )


def test_bounded_folder_summarize_sweep_noop_in_inplace_directory_mode(tmp_path, monkeypatch):
    """In inplace mode with a directory source, ``workspace_root`` ==
    ``artifacts_dir``.  The sweep must be a no-op there — the
    canonical per-document files live at the root and must not be
    deleted."""
    from agents.office import nodes as office_nodes

    def _fake_read(path):
        return {
            "source_path": path,
            "content": f"content of {os.path.basename(path)}",
            "metadata": {"pages": 1, "type": "TXT"},
        }

    monkeypatch.setattr(office_nodes, "_read_summary_payload", _fake_read)

    src = tmp_path / "stlouis"
    src.mkdir()
    (src / "alpha.txt").write_text("alpha content", encoding="utf-8")
    (src / "beta.txt").write_text("beta content", encoding="utf-8")

    # Inplace layout: workspace_root == artifacts_dir == source dir.
    state = {
        "_plugin_manager": None,
        "workspace_root": str(src),
    }
    result = _run_bounded_folder_summarize(
        state,
        runtime=_build_fake_runtime(),
        validated_paths=[str(src / "alpha.txt"), str(src / "beta.txt")],
        output_mode="inplace",
        artifacts_dir=str(src),
        system_prompt="sys",
    )
    assert result.success is True
    # In inplace mode the per-document files live directly in the
    # source dir; the sweep must not have removed them.
    assert (src / "alpha.txt.summary.md").exists()
    assert (src / "beta.txt.summary.md").exists()
    assert (src / "combined-summary.md").exists()


def test_bounded_folder_summarize_fails_when_runtime_returns_error_text(tmp_path, monkeypatch):
    """A transport/runtime failure message is not a valid document summary.

    The bounded folder summarize flow writes deliverables itself after a
    per-document ``runtime.run`` call. If that call returns an English error
    string such as "endpoint is unreachable", file-existence verification alone
    is not enough; the task must fail generically instead of writing error text
    into ``*.summary.md`` files and reporting success.
    """
    from agents.office import nodes as office_nodes

    def _fake_read(path):
        return {
            "source_path": path,
            "content": f"content of {os.path.basename(path)}",
            "metadata": {"type": "TXT"},
        }

    runtime = MagicMock()
    runtime.run.return_value = {
        "raw_response": "copilot-cli request failed because the endpoint is unreachable.",
        "summary": "",
    }
    monkeypatch.setattr(office_nodes, "_read_summary_payload", _fake_read)

    src = tmp_path / "source"
    src.mkdir()
    first = src / "alpha.txt"
    second = src / "beta.txt"
    first.write_text("alpha", encoding="utf-8")
    second.write_text("beta", encoding="utf-8")
    artifacts_dir = tmp_path / "office" / "artifacts"
    artifacts_dir.mkdir(parents=True)

    result = _run_bounded_folder_summarize(
        {"_plugin_manager": None, "workspace_root": str(artifacts_dir.parent)},
        runtime=runtime,
        validated_paths=[str(first), str(second)],
        output_mode="workspace",
        artifacts_dir=str(artifacts_dir),
        system_prompt="sys",
    )

    assert result.success is False
    assert "endpoint is unreachable" in result.summary
    assert not (artifacts_dir / "alpha.txt.summary.md").exists()


def test_bounded_folder_summarize_failure_surfaces_underlying_reason(tmp_path, monkeypatch):
    """When the runtime returns a generic failure summary but the
    actual reason lives only in the ``warnings`` array (e.g. an LLM
    request that ran past its 90s timeout, the cause of the
    task-63432d83fc65 misclassification), the bounded failure summary
    must include the underlying reason, not just the catch-all
    "endpoint is unreachable" string.  Otherwise operators reading
    the task-report can no longer tell timeout from DNS from TLS.
    """
    from agents.office import nodes as office_nodes

    def _fake_read(path):
        return {
            "source_path": path,
            "content": f"content of {os.path.basename(path)}",
            "metadata": {"type": "TXT"},
        }

    runtime = MagicMock()
    runtime.run.return_value = {
        "raw_response": "copilot-cli request failed because the endpoint is unreachable.",
        "summary": "",
        "warnings": [
            "copilot-cli network error: connect-agent request timed out after 90s"
        ],
    }
    monkeypatch.setattr(office_nodes, "_read_summary_payload", _fake_read)

    src = tmp_path / "source"
    src.mkdir()
    first = src / "alpha.txt"
    first.write_text("alpha", encoding="utf-8")
    artifacts_dir = tmp_path / "office" / "artifacts"
    artifacts_dir.mkdir(parents=True)

    result = _run_bounded_folder_summarize(
        {"_plugin_manager": None, "workspace_root": str(artifacts_dir.parent)},
        runtime=runtime,
        validated_paths=[str(first)],
        output_mode="workspace",
        artifacts_dir=str(artifacts_dir),
        system_prompt="sys",
    )

    assert result.success is False
    # The summary must surface the actual reason (90s timeout), not
    # just the misleading "endpoint is unreachable" wrapper.
    assert "timed out" in result.summary
    assert "connect-agent request timed out" in result.summary
    # No stray summary file should have been written.
    assert not (artifacts_dir / "alpha.txt.summary.md").exists()
