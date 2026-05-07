"""pytest configuration for Constellation tests.

Provides stub fixtures for integration test files that are also used as
standalone scripts.  These stubs cause integration-only tests to be skipped
when run via pytest without a live agent.  Pass the real values as CLI
arguments when running the scripts directly (e.g.
``python3 tests/test_async_skills.py --container``).
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Integration stubs — skipped automatically when no agent is running
# ---------------------------------------------------------------------------


@pytest.fixture
def bb_url():
    """Bitbucket/SCM agent URL — provided via --scm-url when running standalone."""
    pytest.skip("Requires a running SCM agent (use --scm-url to run standalone)")


@pytest.fixture
def report():
    """TestReport instance — provided internally when running standalone."""
    pytest.skip("Requires running as a standalone script (not via pytest)")


@pytest.fixture
def base_url():
    """Agent base URL — provided via --agent-url when running standalone."""
    pytest.skip("Requires a running agent (use --agent-url to run standalone)")


@pytest.fixture
def project():
    """SCM project key — loaded from tests/.env when running standalone."""
    pytest.skip("Requires tests/.env configuration")


@pytest.fixture
def repo():
    """SCM repository slug — loaded from tests/.env when running standalone."""
    pytest.skip("Requires tests/.env configuration")


@pytest.fixture
def android_url():
    """Android agent URL — provided via --android-url when running standalone."""
    pytest.skip("Requires a running Android agent")


@pytest.fixture
def verbose():
    """Verbose flag — set via -v when running standalone."""
    return False
