from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common.task_permissions import PermissionDeniedError, grant_permission, load_permission_grant
from office import app as office_app

# Default office permissions grant for unit tests — mirrors what Compass attaches.
_OFFICE_PERMISSIONS = load_permission_grant("office").to_dict()
# Extend the grant to allow writes — used by inplace and organize tests.
_OFFICE_RW_PERMISSIONS = grant_permission(
    _OFFICE_PERMISSIONS,
    agent="office",
    action="write",
    scope="task_root",
    description="Allow in-place write during unit tests",
)


def _make_message(
    capability: str,
    paths: list[str],
    workspace: str,
    *,
    output_mode: str = "workspace",
    permissions: dict | None = None,
) -> dict:
    if permissions is None:
        # Inplace mode needs write permission; workspace mode only needs read.
        permissions = _OFFICE_RW_PERMISSIONS if output_mode == "inplace" else _OFFICE_PERMISSIONS
    return {
        "parts": [{"text": f"Run {capability} on the given files."}],
        "metadata": {
            "requestedCapability": capability,
            "officeTargetPaths": paths,
            "officeInputRoot": workspace,
            "officeOutputMode": output_mode,
            "officeWorkspacePath": workspace,
            "sharedWorkspacePath": workspace,
            "orchestratorTaskId": "task-unit",
            "permissions": permissions,
        },
    }


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------

class TestExecuteSummary(unittest.TestCase):
    pass


class TestExecuteAnalysis(unittest.TestCase):
    pass


class TestExecuteOrganize(unittest.TestCase):
    pass


# ---------------------------------------------------------------------------
# Input validation tests
# ---------------------------------------------------------------------------

class TestInputValidation(unittest.TestCase):
    pass


# ---------------------------------------------------------------------------
# Utility function tests — only functions still present in office/app.py
# ---------------------------------------------------------------------------

class TestPathUtils(unittest.TestCase):
    def test_path_within_base_true(self):
        with tempfile.TemporaryDirectory() as tmp:
            child = os.path.join(tmp, "subdir", "file.txt")
            self.assertTrue(office_app._path_within_base(child, tmp))

    def test_path_within_base_false(self):
        with tempfile.TemporaryDirectory() as tmp1, tempfile.TemporaryDirectory() as tmp2:
            self.assertFalse(office_app._path_within_base(tmp2, tmp1))


class TestWriteRootLocking(unittest.TestCase):
    pass


class TestPasswordProtectedPdf(unittest.TestCase):
    pass


class TestMixedFolderPartialSuccess(unittest.TestCase):
    pass


class TestConcurrentWriteRejection(unittest.TestCase):
    pass


class TestPathTraversal(unittest.TestCase):
    pass


class TestLegacyDocRejection(unittest.TestCase):
    pass


class TestIllegalRuntimeAction(unittest.TestCase):
    pass


class TestNonOverwriteOutput(unittest.TestCase):
    pass


class TestPreflightLargeDirectory(unittest.TestCase):
    pass


# ---------------------------------------------------------------------------
# Rules loading verification
# ---------------------------------------------------------------------------

class TestRulesLoading(unittest.TestCase):
    def test_office_rules_exist_and_are_loadable(self):
        """Verify rules files exist per agentic runtime doc §4.4."""
        from common.rules_loader import load_rules
        rules_text = load_rules("office")
        self.assertIn("Office", rules_text)
        self.assertIn("safety", rules_text.lower())

    def test_build_system_prompt_includes_rules(self):
        """Verify build_system_prompt injects rules into prompts."""
        from common.rules_loader import build_system_prompt
        prompt = build_system_prompt("Base system prompt.", "office")
        self.assertIn("Base system prompt.", prompt)
        self.assertIn("AGENT RULES", prompt)


# ---------------------------------------------------------------------------
# Workflow file existence
# ---------------------------------------------------------------------------

class TestWorkflowFileExists(unittest.TestCase):
    def test_office_workflow_file_exists(self):
        """Verify workflows/default-workflow.md exists per agentic runtime doc §4.4."""
        workflow_path = Path(__file__).resolve().parent.parent / "office" / "workflows" / "default-workflow.md"
        self.assertTrue(workflow_path.is_file(), f"Missing: {workflow_path}")


if __name__ == "__main__":
    unittest.main()
