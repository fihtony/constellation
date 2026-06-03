"""Path-safety helpers shared by the plan-output gate and the office tools.

The Office agent must never let LLM-driven file mutations escape their
intended root. The functions here are the only place that should resolve a
caller-supplied relative path against a fixed root and verify the result
is genuinely inside that root (post-symlink resolution).
"""
from __future__ import annotations

import os
import re


class PathSafetyError(ValueError):
    """Raised when a path violates the safety contract."""


_DRIVE_LETTER_RE = re.compile(r"^[a-zA-Z]:[\\/]")


def normalize_relative_path(relative: str) -> str:
    """Strip trailing separators and normalize backslashes to forward slashes.

    Does NOT resolve ``..`` segments — that is the job of
    :func:`resolve_within_root`, which combines the path with a real root.
    """
    if not isinstance(relative, str):
        raise PathSafetyError(f"path must be a string, got {type(relative).__name__}")
    cleaned = relative.replace("\\", "/").rstrip("/")
    return cleaned


def is_within_root(root: str, candidate: str) -> bool:
    """Return True if ``candidate`` resolves to a path inside ``root``.

    Both arguments are resolved with ``realpath`` so symlinks that escape the
    root are caught.
    """
    real_root = os.path.realpath(root)
    real_candidate = os.path.realpath(candidate)
    if real_candidate == real_root:
        return True
    return real_candidate.startswith(real_root.rstrip(os.sep) + os.sep)


def resolve_within_root(root: str, relative: str) -> str:
    """Resolve ``relative`` against ``root`` and return its real path.

    Raises :class:`PathSafetyError` for any of:
    * ``..`` segments after normalization
    * absolute paths (POSIX or Windows)
    * drive letters
    * backslash separators (forces POSIX-only on the contract)
    * symlinks whose chain escapes ``root``
    * the path itself does not exist (we need realpath to chase symlinks)

    On success returns the absolute real path.
    """
    if not isinstance(root, str) or not root:
        raise PathSafetyError("root must be a non-empty string")
    real_root = os.path.realpath(os.path.abspath(root))
    normalized = normalize_relative_path(relative)
    if not normalized:
        raise PathSafetyError("path is empty after normalization")
    if normalized.startswith("/"):
        raise PathSafetyError(f"absolute path not allowed: {relative!r}")
    if _DRIVE_LETTER_RE.match(normalized):
        raise PathSafetyError(f"drive-letter path not allowed: {relative!r}")
    if ".." in normalized.split("/"):
        raise PathSafetyError(f"parent traversal not allowed: {relative!r}")

    candidate = os.path.join(real_root, normalized)
    if not os.path.exists(candidate):
        raise PathSafetyError(f"path does not exist: {candidate}")
    real = os.path.realpath(candidate)
    if not is_within_root(real_root, real):
        raise PathSafetyError(
            f"symlink escapes root: {relative!r} -> {real!r}"
        )
    return real
