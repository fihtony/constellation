"""Install slug — deterministic 8-character identifier for a project root.

Multiple Constellation instances on the same host (dev/staging/prod) can
coexist by using their slug as a suffix in Docker network names, image tags,
and Registry ports.

Usage::

    from common.install_slug import get_install_slug

    slug = get_install_slug()          # e.g. "a3f8c21b"
    network_name = f"constellation-{slug}"
"""

from __future__ import annotations

import hashlib
import os


def get_install_slug(project_root: str | None = None) -> str:
    """Return a deterministic 8-character hex slug for *project_root*.

    Defaults to the repository root (two levels above this file).
    """
    if project_root is None:
        project_root = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..")
        )
    return hashlib.sha1(os.path.abspath(project_root).encode()).hexdigest()[:8]
