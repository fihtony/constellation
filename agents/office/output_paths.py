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
    mode = (output_mode or "").strip().lower()
    if mode == "inplace":
        base_dir = source_path if os.path.isdir(source_path) else os.path.dirname(source_path)
        return os.path.join(base_dir, os.path.basename(filename))
    return os.path.join(artifacts_dir, os.path.basename(filename))


def target_with_suffix(
    output_mode: str,
    source_path: str,
    artifacts_dir: str,
    suffix: str,
) -> str:
    """Convenience wrapper; filename = ``<basename(source_path)><suffix>``."""
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
        file_count = 0
        for path in validated_paths:
            if not path:
                continue
            expected.append(target_with_suffix(output_mode, path, artifacts_dir, ".summary.md"))
            file_count += 1
        if file_count > 1 and validated_paths:
            base_path = next((p for p in validated_paths if p), validated_paths[0])
            expected.append(target_for_source(output_mode, base_path, artifacts_dir, "combined-summary.md"))
        return expected

    if capability == "organize" and validated_paths:
        expected.append(
            target_for_source(output_mode, validated_paths[0], artifacts_dir, "organization-plan.md")
        )
        source_root = validated_paths[0] if validated_paths else ""
        if (output_mode or "").strip().lower() == "inplace":
            expected.append(os.path.join(source_root, "organized-output", "files"))
        else:
            expected.append(os.path.join(artifacts_dir, "organized-output", "files"))
        return expected

    return expected