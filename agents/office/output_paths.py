"""Single source of truth for *where* an office deliverable should live.

The Office agent supports two output modes:

- ``workspace`` — every deliverable lands in the office workspace
  (``artifacts_dir``).
- ``inplace`` — the deliverable lands inside the user's source
  folder (when the source is a directory) or next to it (when the
  source is a file).

Both the LLM prompt builders and the delivery-verification helper
consume this module so the prompt and the verifier agree by
construction.

The helper takes no env-var input. Authorisation (sandbox, mount
whitelist, write grant) is handled by ``WriteFileTool`` and
``_validate_path``; this module only computes the *target path*
given an already-validated source.
"""
from __future__ import annotations

import os
from typing import Literal

OutputMode = Literal["workspace", "inplace"]


def _deliverable_base_dir(output_mode: str, source_path: str, artifacts_dir: str) -> str:
    """Return the base directory an office deliverable should land under.

    - ``workspace``: ``artifacts_dir``
    - ``inplace`` + file: ``dirname(source_path)``
    - ``inplace`` + directory: ``source_path``
    """
    mode = (output_mode or "").strip().lower()
    if mode == "inplace":
        return source_path if os.path.isdir(source_path) else os.path.dirname(source_path)
    return artifacts_dir


def target_for_source(
    output_mode: str,
    source_path: str,
    artifacts_dir: str,
    filename: str,
) -> str:
    """Return the absolute path an office deliverable should be written to.

    - ``workspace``: ``<artifacts_dir>/<filename>``
    - ``inplace`` + file: ``<dir_of(file)>/<filename>``
    - ``inplace`` + directory: ``<dir>/<filename>``

    Unknown ``output_mode`` values fall back to ``workspace``.
    """
    base_dir = _deliverable_base_dir(output_mode, source_path, artifacts_dir)
    return os.path.join(base_dir, os.path.basename(filename))


def target_with_suffix(
    output_mode: str,
    source_path: str,
    artifacts_dir: str,
    suffix: str,
) -> str:
    """Convenience wrapper; filename = ``<basename(source_path)><suffix>``.

    If ``source_path`` is empty (which the analyze/summarize paths
    already filter out), the filename falls back to ``output<suffix>``
    rather than producing an empty string. This is a defensive
    default; callers should pass a real path.
    """
    basename = os.path.basename(source_path.rstrip("/").rstrip(os.sep)) or "output"
    return target_for_source(output_mode, source_path, artifacts_dir, f"{basename}{suffix}")


def all_targets_for_capability(
    capability: str,
    validated_paths: list[str],
    output_mode: str,
    artifacts_dir: str,
) -> list[str]:
    """All required deliverable paths for the current office task.

    - ``analyze``  — one ``<basename>.analysis.md`` per validated path
    - ``summarize`` — one ``<basename>.summary.md`` per validated path
      plus a ``combined-summary.md`` when there is more than one path
    - ``organize`` — the ``organization-plan.md`` plus the materialized
      output root (``<artifacts>/organized-output/files`` for workspace,
      ``<source>/organized-output/files`` for inplace)
    """
    expected: list[str] = []
    if capability == "analyze":
        for path in validated_paths:
            if not path:
                continue
            expected.append(target_with_suffix(output_mode, path, artifacts_dir, ".analysis.md"))
        return expected

    if capability == "summarize":
        non_empty = [p for p in validated_paths if p]
        for path in non_empty:
            expected.append(target_with_suffix(output_mode, path, artifacts_dir, ".summary.md"))
        if len(non_empty) > 1:
            expected.append(
                target_for_source(output_mode, non_empty[0], artifacts_dir, "combined-summary.md")
            )
        return expected

    if capability == "organize" and validated_paths:
        expected.append(
            target_for_source(output_mode, validated_paths[0], artifacts_dir, "organization-plan.md")
        )
        root = _deliverable_base_dir(output_mode, validated_paths[0], artifacts_dir)
        expected.append(os.path.join(root, "organized-output", "files"))
        return expected

    return expected