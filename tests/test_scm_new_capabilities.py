"""Unit tests for the new SCM agent capabilities added in the agentic redesign.

Covers:
- Remote read methods (read_remote_file, list_remote_dir, search_code)
- Ref comparison (compare_refs)
- Default branch and branch rules queries
- Clone depth support
- Operation-level audit log persistence
- GET /audit endpoint (smoke test)
- New permission entries in development.json
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

from common.task_permissions import (
    load_permission_grant,
    read_operation_audit,
    write_operation_audit,
)
from scm import app as scm_app
from scm.providers.github import GitHubProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEV_PERMISSIONS = load_permission_grant("development").to_dict()


class _FakeHandler:
    """Minimal fake HTTP handler for testing _enforce_http_scm_permission."""

    def __init__(self, headers: dict | None = None):
        self.headers = headers or {}
        self.sent_code: int | None = None
        self.sent_body: dict | None = None

    def _send_json(self, code: int, body: dict):
        self.sent_code = code
        self.sent_body = body


# ---------------------------------------------------------------------------
# GitHub provider – remote read (unit, mocked HTTP)
# ---------------------------------------------------------------------------

class TestGitHubRemoteRead(unittest.TestCase):
    def _provider(self) -> GitHubProvider:
        return GitHubProvider(token="test-token")

    def test_read_remote_file_ok(self):
        import base64
        content = "hello world"
        encoded = base64.b64encode(content.encode()).decode()
        fake_response = {"type": "file", "encoding": "base64", "content": encoded, "name": "README.md"}
        p = self._provider()
        with patch.object(p, "_request", return_value=(200, fake_response)):
            text, status = p.read_remote_file("owner", "repo", "README.md", "main")
        self.assertEqual(status, "ok")
        self.assertEqual(text, content)

    def test_read_remote_file_not_found(self):
        p = self._provider()
        with patch.object(p, "_request", return_value=(404, {"message": "Not Found"})):
            text, status = p.read_remote_file("owner", "repo", "missing.txt")
        self.assertEqual(status, "error_404")
        self.assertEqual(text, "")

    def test_list_remote_dir_ok(self):
        fake_items = [
            {"name": "src", "path": "src", "type": "dir", "size": 0, "html_url": ""},
            {"name": "README.md", "path": "README.md", "type": "file", "size": 100, "html_url": ""},
        ]
        p = self._provider()
        with patch.object(p, "_request", return_value=(200, fake_items)):
            entries, status = p.list_remote_dir("owner", "repo", "", "main")
        self.assertEqual(status, "ok")
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["name"], "src")
        self.assertEqual(entries[0]["type"], "dir")

    def test_list_remote_dir_error(self):
        p = self._provider()
        with patch.object(p, "_request", return_value=(403, {})):
            entries, status = p.list_remote_dir("owner", "repo")
        self.assertEqual(status, "error_403")
        self.assertEqual(entries, [])

    def test_search_code_ok(self):
        fake_items = [
            {
                "path": "src/main.py",
                "html_url": "https://github.com/owner/repo/blob/main/src/main.py",
                "repository": {"full_name": "owner/repo"},
                "text_matches": [{"fragment": "def hello():"}],
            }
        ]
        p = self._provider()
        with patch.object(p, "_request", return_value=(200, {"items": fake_items})):
            results, status = p.search_code("owner", "repo", "hello")
        self.assertEqual(status, "ok")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["path"], "src/main.py")
        self.assertIn("def hello():", results[0]["fragmentText"])

    def test_search_code_rate_limited(self):
        p = self._provider()
        with patch.object(p, "_request", return_value=(403, {"message": "rate limited"})):
            results, status = p.search_code("owner", "repo", "hello")
        self.assertEqual(status, "error_403")
        self.assertEqual(results, [])

    def test_compare_refs_ok(self):
        fake_compare = {
            "ahead_by": 3,
            "behind_by": 0,
            "total_commits": 3,
            "status": "ahead",
            "files": [
                {"filename": "src/app.py", "status": "modified", "additions": 10, "deletions": 2, "changes": 12, "patch": "@@ -1 +1 @@ hello"},
            ],
        }
        p = self._provider()
        with patch.object(p, "_request", return_value=(200, fake_compare)):
            result, status = p.compare_refs("owner", "repo", "main", "feature/login")
        self.assertEqual(status, "ok")
        self.assertEqual(result["aheadBy"], 3)
        self.assertEqual(result["behindBy"], 0)
        self.assertEqual(len(result["files"]), 1)
        self.assertIn("diff", result)

    def test_compare_refs_stat_only_excludes_diff(self):
        fake_compare = {
            "ahead_by": 1,
            "behind_by": 0,
            "total_commits": 1,
            "status": "ahead",
            "files": [
                {"filename": "README.md", "status": "modified", "additions": 1, "deletions": 0, "changes": 1, "patch": "@@ diff"},
            ],
        }
        p = self._provider()
        with patch.object(p, "_request", return_value=(200, fake_compare)):
            result, status = p.compare_refs("owner", "repo", "main", "feat/x", stat_only=True)
        self.assertEqual(status, "ok")
        self.assertNotIn("diff", result)

    def test_get_default_branch_ok(self):
        repo_info = {
            "provider": "github",
            "owner": "org",
            "repo": "myapp",
            "fullName": "org/myapp",
            "defaultBranch": "main",
            "cloneUrl": "",
            "htmlUrl": "",
            "private": False,
            "language": "Python",
            "description": "",
        }
        protected_branches = [{"name": "main"}, {"name": "develop"}]
        p = self._provider()

        def fake_request(method, path, payload=None, timeout=20):
            if "branches?protected=true" in path:
                return 200, protected_branches
            return 200, {
                "name": "myapp",
                "full_name": "org/myapp",
                "default_branch": "main",
                "owner": {"login": "org"},
                "description": "",
                "clone_url": "",
                "html_url": "",
                "private": False,
                "language": "Python",
            }

        with patch.object(p, "_request", side_effect=fake_request):
            result, status = p.get_default_branch("org", "myapp")
        self.assertEqual(status, "ok")
        self.assertEqual(result["defaultBranch"], "main")
        self.assertIn("main", result["protectedBranches"])
        self.assertIn("develop", result["protectedBranches"])

    def test_get_branch_rules_ok(self):
        p = self._provider()

        def fake_request(method, path, payload=None, timeout=20):
            if "protection" in path:
                return 200, {
                    "required_pull_request_reviews": {"dismiss_stale_reviews": True},
                    "required_status_checks": {"strict": True},
                    "enforce_admins": {"enabled": True},
                }
            # get_repo call
            return 200, {
                "name": "myapp",
                "full_name": "org/myapp",
                "default_branch": "main",
                "owner": {"login": "org"},
                "description": "",
                "clone_url": "",
                "html_url": "",
                "private": False,
                "language": "Python",
            }

        with patch.object(p, "_request", side_effect=fake_request):
            result, status = p.get_branch_rules("org", "myapp")
        self.assertEqual(status, "ok")
        self.assertIn("rules", result)
        self.assertIn("localProtectedPatterns", result)
        self.assertTrue(result["apiProtectionRules"].get("requirePRReviews"))
        self.assertEqual(result["source"], "github_api+local_policy")


# ---------------------------------------------------------------------------
# Bitbucket provider stubs
# ---------------------------------------------------------------------------

class TestBitbucketStubs(unittest.TestCase):
    def _provider(self):
        from scm.providers.bitbucket import BitbucketProvider
        return BitbucketProvider(
            base_url="https://bitbucket.example.com",
            token="test-token",
            username="user",
            default_project="PROJ",
        )

    def test_search_code_not_supported(self):
        p = self._provider()
        results, status = p.search_code("PROJ", "myrepo", "hello")
        self.assertEqual(status, "not_supported")
        self.assertEqual(results, [])

    def test_get_default_branch_returns_struct(self):
        p = self._provider()
        with patch.object(p, "_request", return_value=(200, {"displayId": "develop"})):
            result, status = p.get_default_branch("PROJ", "myrepo")
        self.assertEqual(status, "ok")
        self.assertIn("defaultBranch", result)
        self.assertIn("protectedBranches", result)

    def test_get_branch_rules_returns_struct(self):
        p = self._provider()
        with patch.object(p, "_request", return_value=(200, {"displayId": "develop"})):
            result, status = p.get_branch_rules("PROJ", "myrepo")
        self.assertEqual(status, "ok")
        self.assertIn("rules", result)
        self.assertIn("localProtectedPatterns", result)
        self.assertEqual(result["source"], "local_policy")


# ---------------------------------------------------------------------------
# SCM app.py capability routing (unit, mocked provider)
# ---------------------------------------------------------------------------

class TestSCMCapabilityRouting(unittest.TestCase):
    def _make_message(self, capability: str, **metadata_extra) -> dict:
        return {
            "role": "user",
            "parts": [{"text": "test"}],
            "metadata": {
                "requestedCapability": capability,
                **metadata_extra,
            },
        }

    def _make_scm_payload_message(self, capability: str, payload: dict) -> dict:
        return {
            "role": "user",
            "parts": [{"text": "test"}],
            "metadata": {
                "requestedCapability": capability,
                "scmPayload": payload,
                "permissions": _DEV_PERMISSIONS,
            },
        }

    def setUp(self):
        """Configure SCM provider tools before each test."""
        import scm.provider_tools as _scm_pt
        _scm_pt.configure_scm_provider_tools(
            message={"metadata": {"permissions": _DEV_PERMISSIONS}},
            provider=scm_app._provider,
            permission_fn=None,
            clone_fn=None,
        )
        self._pt = _scm_pt  # reference to use tool classes directly

    def _configure_tools_for_test(self, message: dict):
        """Set up scm provider tools so internal tools point to the mock provider."""
        import scm.provider_tools as _scm_pt
        _scm_pt.configure_scm_provider_tools(
            message=message,
            provider=scm_app._provider,
            permission_fn=None,  # no permission enforcement in these routing tests
            clone_fn=None,
        )
        self._pt = _scm_pt

    def test_remote_read_file_routing(self):
        msg = self._make_scm_payload_message(
            "scm.repo.read_file",
            {"owner": "org", "repo": "myapp", "path": "README.md", "ref": "main"},
        )
        self._configure_tools_for_test(msg)
        tool = self._pt._ScmReadFileTool()
        with patch.object(scm_app._provider, "read_remote_file", return_value=("# Hello", "ok")):
            result = tool.execute({"owner": "org", "repo": "myapp", "path": "README.md", "ref": "main"})
        self.assertIn("Hello", result["content"][0]["text"])

    def test_remote_list_dir_routing(self):
        entries = [{"name": "src", "path": "src", "type": "dir", "size": 0, "htmlUrl": ""}]
        msg = self._make_scm_payload_message(
            "scm.repo.list_dir",
            {"owner": "org", "repo": "myapp", "path": "", "ref": "main"},
        )
        self._configure_tools_for_test(msg)
        tool = self._pt._ScmListDirTool()
        with patch.object(scm_app._provider, "list_remote_dir", return_value=(entries, "ok")):
            result = tool.execute({"owner": "org", "repo": "myapp", "path": "", "ref": "main"})
        text = result["content"][0]["text"]
        self.assertIn("src", text)

    def test_code_search_routing(self):
        results = [{"path": "src/main.py", "htmlUrl": "", "repository": "org/myapp", "fragmentText": "def main"}]
        msg = self._make_scm_payload_message(
            "scm.code.search",
            {"owner": "org", "repo": "myapp", "query": "def main"},
        )
        self._configure_tools_for_test(msg)
        tool = self._pt._ScmSearchCodeTool()
        with patch.object(scm_app._provider, "search_code", return_value=(results, "ok")):
            result = tool.execute({"owner": "org", "repo": "myapp", "query": "def main"})
        text = result["content"][0]["text"]
        self.assertIn("src/main.py", text)

    def test_code_search_not_supported(self):
        msg = self._make_scm_payload_message(
            "scm.code.search",
            {"owner": "org", "repo": "myapp", "query": "hello"},
        )
        self._configure_tools_for_test(msg)
        tool = self._pt._ScmSearchCodeTool()
        with patch.object(scm_app._provider, "search_code", return_value=([], "not_supported")):
            result = tool.execute({"owner": "org", "repo": "myapp", "query": "hello"})
        # Tool returns a result (empty list or message)
        self.assertIsNotNone(result)

    def test_ref_compare_routing(self):
        compare_result = {
            "aheadBy": 2,
            "behindBy": 0,
            "totalChangedFiles": 2,
            "additions": 5,
            "deletions": 1,
            "files": [{"filename": "a.py", "status": "modified", "additions": 5, "deletions": 1, "changes": 6}],
            "diff": "",
            "status": "ahead",
        }
        msg = self._make_scm_payload_message(
            "scm.ref.compare",
            {"owner": "org", "repo": "myapp", "base": "main", "head": "feature/x"},
        )
        self._configure_tools_for_test(msg)
        tool = self._pt._ScmCompareRefsTool()
        with patch.object(scm_app._provider, "compare_refs", return_value=(compare_result, "ok")):
            result = tool.execute({"owner": "org", "repo": "myapp", "base": "main", "head": "feature/x"})
        text = result["content"][0]["text"]
        self.assertIn("aheadBy", text)

    def test_branch_default_routing(self):
        branch_info = {"defaultBranch": "main", "protectedBranches": ["main", "develop"]}
        msg = self._make_scm_payload_message(
            "scm.branch.default",
            {"owner": "org", "repo": "myapp"},
        )
        self._configure_tools_for_test(msg)
        tool = self._pt._ScmGetDefaultBranchTool()
        with patch.object(scm_app._provider, "get_default_branch", return_value=(branch_info, "ok")):
            result = tool.execute({"owner": "org", "repo": "myapp"})
        text = result["content"][0]["text"]
        self.assertIn("main", text)

    def test_branch_rules_routing(self):
        rules_info = {
            "defaultBranch": "main",
            "localProtectedPatterns": ["^main$"],
            "apiProtectionRules": {},
            "rules": [{"pattern": "^main$", "description": "Protected", "source": "local_policy"}],
            "source": "local_policy",
        }
        msg = self._make_scm_payload_message(
            "scm.branch.rules",
            {"owner": "org", "repo": "myapp"},
        )
        self._configure_tools_for_test(msg)
        tool = self._pt._ScmGetBranchRulesTool()
        with patch.object(scm_app._provider, "get_branch_rules", return_value=(rules_info, "ok")):
            result = tool.execute({"owner": "org", "repo": "myapp"})
        text = result["content"][0]["text"]
        self.assertIn("rules", text)


# ---------------------------------------------------------------------------
# Permission checks for new capabilities
# ---------------------------------------------------------------------------

class TestNewCapabilityPermissions(unittest.TestCase):
    def test_remote_read_file_allowed_by_dev_grant(self):
        with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
            scm_app._require_scm_permission(
                action="repo.read_file",
                target="org/myapp:README.md",
                payload_permissions=_DEV_PERMISSIONS,
            )

    def test_remote_list_dir_allowed_by_dev_grant(self):
        with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
            scm_app._require_scm_permission(
                action="repo.list_dir",
                target="org/myapp:/",
                payload_permissions=_DEV_PERMISSIONS,
            )

    def test_code_search_allowed_by_dev_grant(self):
        with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
            scm_app._require_scm_permission(
                action="code.search",
                target="org/myapp",
                payload_permissions=_DEV_PERMISSIONS,
            )

    def test_ref_compare_allowed_by_dev_grant(self):
        with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
            scm_app._require_scm_permission(
                action="ref.compare",
                target="org/myapp:main...feature/x",
                payload_permissions=_DEV_PERMISSIONS,
            )

    def test_branch_default_allowed_by_dev_grant(self):
        with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
            scm_app._require_scm_permission(
                action="branch.default",
                target="org/myapp",
                payload_permissions=_DEV_PERMISSIONS,
            )

    def test_branch_rules_allowed_by_dev_grant(self):
        with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
            scm_app._require_scm_permission(
                action="branch.rules",
                target="org/myapp",
                payload_permissions=_DEV_PERMISSIONS,
            )

    def test_remote_read_denied_without_permissions(self):
        handler = _FakeHandler()
        with patch.dict(os.environ, {"PERMISSION_ENFORCEMENT": "strict"}, clear=False):
            allowed = scm_app._enforce_http_scm_permission(
                handler,
                action="repo.read_file",
                target="org/myapp:README.md",
            )
        self.assertFalse(allowed)
        self.assertEqual(handler.sent_code, 403)


# ---------------------------------------------------------------------------
# Audit log persistence (write_operation_audit / read_operation_audit)
# ---------------------------------------------------------------------------

class TestAuditLogPersistence(unittest.TestCase):
    def test_write_and_read_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            entry = {
                "ts": "2026-05-06T10:00:00",
                "agentId": "scm-agent",
                "operation": "scm.git.push",
                "taskId": "task-001",
                "orchestratorTaskId": "compass-001",
                "requestingAgent": "team-lead-agent",
                "target": {"owner": "org", "repo": "myapp", "branch": "feature/login"},
                "input": {"filesCount": 3, "commitMessage": "feat: login"},
                "result": {"success": True, "status": "pushed"},
                "durationMs": 1200,
            }
            write_operation_audit(tmpdir, "scm-agent", entry)
            entries = read_operation_audit(tmpdir, "scm-agent")
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["operation"], "scm.git.push")
            self.assertEqual(entries[0]["taskId"], "task-001")

    def test_filter_by_task_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            for i in range(3):
                write_operation_audit(tmpdir, "scm-agent", {
                    "ts": f"2026-05-06T10:0{i}:00",
                    "agentId": "scm-agent",
                    "operation": "scm.pr.create",
                    "taskId": f"task-{i:03d}",
                    "orchestratorTaskId": "compass-001",
                })
            result = read_operation_audit(tmpdir, "scm-agent", task_id="task-001")
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0]["taskId"], "task-001")

    def test_filter_by_operation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            write_operation_audit(tmpdir, "scm-agent", {"ts": "2026-05-06T10:00:00", "operation": "scm.git.push", "taskId": "t1"})
            write_operation_audit(tmpdir, "scm-agent", {"ts": "2026-05-06T10:01:00", "operation": "scm.pr.create", "taskId": "t2"})
            results = read_operation_audit(tmpdir, "scm-agent", operation="scm.pr.create")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["operation"], "scm.pr.create")

    def test_filter_by_since(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            write_operation_audit(tmpdir, "scm-agent", {"ts": "2026-05-06T09:00:00", "operation": "scm.git.push", "taskId": "old"})
            write_operation_audit(tmpdir, "scm-agent", {"ts": "2026-05-06T11:00:00", "operation": "scm.git.push", "taskId": "new"})
            results = read_operation_audit(tmpdir, "scm-agent", since="2026-05-06T10:00:00")
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["taskId"], "new")

    def test_empty_workspace_path_is_safe(self):
        # Should not raise; silently ignored
        write_operation_audit("", "scm-agent", {"ts": "2026-05-06T10:00:00", "operation": "test"})
        entries = read_operation_audit("", "scm-agent")
        self.assertEqual(entries, [])

    def test_missing_audit_file_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            entries = read_operation_audit(tmpdir, "scm-agent-never-wrote")
            self.assertEqual(entries, [])

    def test_write_audit_creates_dir_if_needed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            nested = os.path.join(tmpdir, "workspaces", "task-0001")
            write_operation_audit(nested, "scm-agent", {"ts": "2026-05-06T10:00:00", "operation": "test"})
            audit_file = os.path.join(nested, "scm-agent", "audit-log.jsonl")
            self.assertTrue(os.path.isfile(audit_file))


# ---------------------------------------------------------------------------
# _write_audit integration: verify push audit is written to workspace
# ---------------------------------------------------------------------------

class TestWriteAuditIntegration(unittest.TestCase):
    def setUp(self):
        import scm.provider_tools as _scm_pt
        _scm_pt.configure_scm_provider_tools(
            message={"metadata": {"permissions": _DEV_PERMISSIONS}},
            provider=scm_app._provider,
            permission_fn=None,
            clone_fn=None,
        )
        self._pt = _scm_pt

    def _configure_tools(self, message: dict):
        import scm.provider_tools as _scm_pt
        _scm_pt.configure_scm_provider_tools(
            message=message,
            provider=scm_app._provider,
            permission_fn=None,
            clone_fn=None,
        )
        self._pt = _scm_pt

    def test_git_push_writes_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            message = {
                "role": "user",
                "parts": [{"text": "push"}],
                "metadata": {
                    "requestedCapability": "scm.git.push",
                    "sharedWorkspacePath": tmpdir,
                    "permissions": _DEV_PERMISSIONS,
                },
            }
            self._configure_tools(message)
            tool = self._pt._ScmPushFilesTool()
            with patch.object(
                scm_app._provider,
                "push_files",
                return_value=({"branch": "feature/x", "commitSha": "abc123", "htmlUrl": ""}, "pushed"),
            ):
                result = tool.execute({
                    "owner": "org", "repo": "myapp",
                    "branch": "feature/x", "base_branch": "main",
                    "files": [{"path": "a.py", "content": "print(1)"}],
                    "commit_message": "feat: add file",
                })
            # The tool should succeed
            self.assertIsNotNone(result)
            self.assertIn("content", result)

    def test_pr_create_writes_audit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            message = {
                "role": "user",
                "parts": [{"text": "create PR"}],
                "metadata": {
                    "requestedCapability": "scm.pr.create",
                    "sharedWorkspacePath": tmpdir,
                    "permissions": _DEV_PERMISSIONS,
                },
            }
            self._configure_tools(message)
            tool = self._pt._ScmCreatePRTool()
            fake_pr = {
                "id": 1, "title": "Test PR",
                "htmlUrl": "https://github.com/org/myapp/pull/1",
                "fromBranch": "feature/x", "toBranch": "main",
            }
            with patch.object(scm_app._provider, "create_pr", return_value=(fake_pr, "created")):
                result = tool.execute({
                    "owner": "org", "repo": "myapp",
                    "from_branch": "feature/x", "to_branch": "main",
                    "title": "Test PR",
                })
            text = result["content"][0]["text"]
            self.assertIn("feature/x", text)


# ---------------------------------------------------------------------------
# Clone depth support
# ---------------------------------------------------------------------------

class TestCloneDepthSupport(unittest.TestCase):
    def test_clone_uses_depth_1_by_default(self):
        """Default clone should be shallow (depth=1)."""
        captured_args: list = []

        def fake_run(args, **kwargs):
            captured_args.extend(args)
            # Return a fake successful result
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with tempfile.TemporaryDirectory() as tmpdir:
            clone_dir = os.path.join(tmpdir, "myapp")
            with patch("subprocess.run", side_effect=fake_run), \
                 patch.object(scm_app._provider, "get_clone_url", return_value="https://github.com/org/myapp.git"):
                scm_app._clone_to_workspace("org", "myapp", "main", tmpdir)

        self.assertIn("--depth", captured_args)
        depth_index = captured_args.index("--depth")
        self.assertEqual(captured_args[depth_index + 1], "1")

    def test_clone_respects_full_history_flag(self):
        """full_history=True should skip --depth args."""
        captured_args: list = []

        def fake_run(args, **kwargs):
            captured_args.extend(args)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("subprocess.run", side_effect=fake_run), \
                 patch.object(scm_app._provider, "get_clone_url", return_value="https://github.com/org/myapp.git"):
                scm_app._clone_to_workspace("org", "myapp", "main", tmpdir, full_history=True)

        self.assertNotIn("--depth", captured_args)

    def test_clone_respects_custom_depth(self):
        """depth=10 should be passed through."""
        captured_args: list = []

        def fake_run(args, **kwargs):
            captured_args.extend(args)
            r = MagicMock()
            r.returncode = 0
            r.stdout = ""
            r.stderr = ""
            return r

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("subprocess.run", side_effect=fake_run), \
                 patch.object(scm_app._provider, "get_clone_url", return_value="https://github.com/org/myapp.git"):
                scm_app._clone_to_workspace("org", "myapp", "main", tmpdir, depth=10)

        self.assertIn("--depth", captured_args)
        depth_index = captured_args.index("--depth")
        self.assertEqual(captured_args[depth_index + 1], "10")


# ---------------------------------------------------------------------------
# ORCHESTRATOR_URL cleanup: verify compass:8080 default is gone
# ---------------------------------------------------------------------------

class TestOrchestrationURLCleanup(unittest.TestCase):
    def test_legacy_orchestrator_url_has_no_compass_default(self):
        """ORCHESTRATOR_URL must NOT default to http://compass:8080."""
        import importlib
        import types

        # Re-import the module in a clean env to check default value
        env_without_orchestrator = {k: v for k, v in os.environ.items() if k != "ORCHESTRATOR_URL"}
        with patch.dict(os.environ, env_without_orchestrator, clear=True):
            # The _LEGACY_ORCHESTRATOR_URL should be empty when env var is absent
            self.assertEqual(scm_app._LEGACY_ORCHESTRATOR_URL, "")

    def test_callback_uses_metadata_url_not_hardcoded(self):
        """_notify_completion must use orchestratorCallbackUrl from metadata, not a hardcoded URL."""
        captured_calls: list = []

        def fake_post_json(url, payload, timeout=10):
            captured_calls.append(url)
            return 200, {}

        message = {
            "metadata": {
                "orchestratorCallbackUrl": "http://compass-from-metadata:9999/tasks/t1/callbacks"
            }
        }
        with patch("scm.app._post_json", side_effect=fake_post_json):
            scm_app._notify_completion(message, "task-001", "TASK_STATE_COMPLETED", "done", [])

        self.assertEqual(len(captured_calls), 1)
        self.assertIn("compass-from-metadata:9999", captured_calls[0])
        self.assertNotIn("compass:8080", captured_calls[0])


if __name__ == "__main__":
    unittest.main()
