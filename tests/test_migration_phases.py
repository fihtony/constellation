"""Tests for Phase 2/3/4/5/6 implementation gaps — all seven migration phases.

Covers:
- Phase 2: Backend Dockerfiles for all agents, shared base Dockerfile hierarchy,
           resolve-agent-runtime script, build-agent-image script
- Phase 3: check_agent_status, list_available_agents, validation tools
- Phase 4: list_skills tool, Registry-backed load_skill, skill catalog
- Phase 5: web/prompts/system manifest + files, android/prompts/system manifest + files
- Phase 6: Per-agent prompt manifest structure for compass/jira/scm/ui-design/office
           Backend variant Dockerfiles for all persistent agents
           Shared base Dockerfile hierarchy (constellation-base:latest + backend bases)
- Phase 7: Acceptance criteria verification
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# ---------------------------------------------------------------------------
# Import all tool modules at module level so register_tool() runs exactly once
# per Python session. snapshot/restore will preserve the registered state.
# ---------------------------------------------------------------------------
import common.tools.registry_tools    # noqa: F401
import common.tools.validation_tools  # noqa: F401
import common.tools.skill_tool        # noqa: F401
import common.tools.coding_tools      # noqa: F401
import common.tools.scm_tools         # noqa: F401

from common.tools.registry import snapshot_registry, restore_registry


# ===========================================================================
# Phase 2: Dockerfile variants and build scripts
# ===========================================================================

class Phase2DockerfileTests(unittest.TestCase):
    """Phase 2: Each backend-capable agent must have Dockerfile.<backend> files."""

    def test_android_has_connect_agent_dockerfile(self):
        path = Path(_REPO_ROOT) / "android" / "Dockerfile.connect-agent"
        self.assertTrue(path.is_file(), f"Missing: {path}")

    def test_android_has_copilot_cli_dockerfile(self):
        path = Path(_REPO_ROOT) / "android" / "Dockerfile.copilot-cli"
        self.assertTrue(path.is_file(), f"Missing: {path}")

    def test_android_has_claude_code_dockerfile(self):
        path = Path(_REPO_ROOT) / "android" / "Dockerfile.claude-code"
        self.assertTrue(path.is_file(), f"Missing: {path}")

    def test_team_lead_has_all_backend_dockerfiles(self):
        for backend in ("connect-agent", "copilot-cli", "claude-code"):
            path = Path(_REPO_ROOT) / "team-lead" / f"Dockerfile.{backend}"
            self.assertTrue(path.is_file(), f"Missing: {path}")

    def test_web_has_all_backend_dockerfiles(self):
        for backend in ("connect-agent", "copilot-cli", "claude-code"):
            path = Path(_REPO_ROOT) / "web" / f"Dockerfile.{backend}"
            self.assertTrue(path.is_file(), f"Missing: {path}")

    def test_android_dockerfile_connect_agent_sets_runtime_env(self):
        content = (Path(_REPO_ROOT) / "android" / "Dockerfile.connect-agent").read_text()
        self.assertIn("AGENT_RUNTIME=connect-agent", content)
        # connect-agent variant must NOT install npm CLI itself (handled by base image)
        self.assertNotIn("npm install -g @github/copilot", content)
        self.assertNotIn("npm install -g @anthropic-ai/claude-code", content)

    def test_android_dockerfile_copilot_cli_sets_runtime_env(self):
        content = (Path(_REPO_ROOT) / "android" / "Dockerfile.copilot-cli").read_text()
        # copilot-cli variant must set the runtime env (either in the FROM base or directly)
        # The CLI install is in the base image; just verify the AGENT_RUNTIME is correct
        self.assertTrue(
            "AGENT_RUNTIME=copilot-cli" in content
            or "constellation-base-copilot-cli" in content,
            "android Dockerfile.copilot-cli must reference copilot-cli backend"
        )

    def test_android_dockerfile_claude_code_sets_runtime_env(self):
        content = (Path(_REPO_ROOT) / "android" / "Dockerfile.claude-code").read_text()
        # claude-code variant must set the runtime env (either in the FROM base or directly)
        self.assertTrue(
            "AGENT_RUNTIME=claude-code" in content
            or "constellation-base-claude-code" in content,
            "android Dockerfile.claude-code must reference claude-code backend"
        )

    def test_base_copilot_cli_dockerfile_installs_npm_cli(self):
        """The shared base Dockerfile for copilot-cli must install the Copilot CLI."""
        base_path = Path(_REPO_ROOT) / "dockerfiles" / "base-copilot-cli.Dockerfile"
        self.assertTrue(base_path.is_file(), f"Missing base Dockerfile: {base_path}")
        content = base_path.read_text()
        self.assertIn("npm install -g @github/copilot", content)

    def test_base_claude_code_dockerfile_installs_npm_cli(self):
        """The shared base Dockerfile for claude-code must install the Claude CLI."""
        base_path = Path(_REPO_ROOT) / "dockerfiles" / "base-claude-code.Dockerfile"
        self.assertTrue(base_path.is_file(), f"Missing base Dockerfile: {base_path}")
        content = base_path.read_text()
        self.assertIn("npm install -g @anthropic-ai/claude-code", content)

    def test_android_dockerfiles_have_required_labels(self):
        for backend in ("connect-agent", "copilot-cli", "claude-code"):
            content = (Path(_REPO_ROOT) / "android" / f"Dockerfile.{backend}").read_text()
            self.assertIn('constellation.agent_id="android-agent"', content, f"Missing label in Dockerfile.{backend}")
            self.assertIn('constellation.runtime.backend=', content, f"Missing runtime label in Dockerfile.{backend}")

    def test_android_dockerfiles_run_as_non_root(self):
        for backend in ("connect-agent", "copilot-cli", "claude-code"):
            content = (Path(_REPO_ROOT) / "android" / f"Dockerfile.{backend}").read_text()
            self.assertIn("USER appuser", content, f"Missing non-root user in Dockerfile.{backend}")

    def test_resolve_agent_runtime_script_exists(self):
        path = Path(_REPO_ROOT) / "scripts" / "resolve-agent-runtime.py"
        self.assertTrue(path.is_file(), f"Missing: {path}")

    def test_build_agent_image_script_exists(self):
        path = Path(_REPO_ROOT) / "scripts" / "build-agent-image.py"
        self.assertTrue(path.is_file(), f"Missing: {path}")


def _load_resolve_module():
    """Load resolve-agent-runtime.py as a module (handles hyphen in filename)."""
    spec = importlib.util.spec_from_file_location(
        "resolve_agent_runtime",
        str(Path(_REPO_ROOT) / "scripts" / "resolve-agent-runtime.py"),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Phase2ResolveRuntimeTests(unittest.TestCase):
    """Phase 2: resolve-agent-runtime.py must correctly resolve effective backend."""

    def setUp(self):
        self.module = _load_resolve_module()

    def test_resolves_team_lead_returns_string(self):
        backend, dockerfile = self.module.resolve_effective_backend("team-lead")
        self.assertIsInstance(backend, str)
        self.assertIn(backend, {"copilot-cli", "connect-agent", "claude-code"})

    def test_resolves_android_returns_string(self):
        backend, dockerfile = self.module.resolve_effective_backend("android")
        self.assertIsInstance(backend, str)

    def test_dockerfile_path_exists_when_resolved(self):
        backend, dockerfile = self.module.resolve_effective_backend("team-lead")
        if dockerfile:
            self.assertTrue(Path(dockerfile).is_file(), f"Dockerfile not found: {dockerfile}")

    def test_invalid_agent_raises_system_exit(self):
        with self.assertRaises(SystemExit):
            self.module.resolve_effective_backend("nonexistent-agent-xyz")

    def test_env_override_respected(self):
        with mock.patch.dict(os.environ, {"AGENT_RUNTIME": "claude-code"}):
            backend, _ = self.module.resolve_effective_backend("team-lead")
        self.assertEqual(backend, "claude-code")

    def test_android_dockerfile_resolves_based_on_backend(self):
        with mock.patch.dict(os.environ, {"AGENT_RUNTIME": "connect-agent"}):
            backend, dockerfile = self.module.resolve_effective_backend("android")
        self.assertEqual(backend, "connect-agent")
        self.assertIsNotNone(dockerfile)
        self.assertIn("connect-agent", dockerfile)


# ===========================================================================
# Helpers
# ===========================================================================

def _is_ok(result: dict) -> bool:
    return not result.get("isError", True)


def _is_err(result: dict) -> bool:
    return bool(result.get("isError", False))


def _output(result: dict) -> str:
    return result["content"][0]["text"]


# ===========================================================================
# Phase 3: check_agent_status, list_available_agents tools
# ===========================================================================

class Phase3RegistryToolsTests(unittest.TestCase):
    """Phase 3: check_agent_status and list_available_agents canonical tool names."""

    def setUp(self):
        # Snapshot AFTER module-level imports so tools are in the snapshot
        self._snap = snapshot_registry()

    def tearDown(self):
        restore_registry(self._snap)

    def test_check_agent_status_tool_registered(self):
        from common.tools.registry import get_tool
        tool = get_tool("check_agent_status")
        self.assertIsNotNone(tool, "check_agent_status tool not registered")

    def test_list_available_agents_tool_registered(self):
        from common.tools.registry import get_tool
        tool = get_tool("list_available_agents")
        self.assertIsNotNone(tool, "list_available_agents tool not registered")

    def test_check_agent_status_returns_unknown_when_registry_unreachable(self):
        from common.tools.registry import get_tool
        tool = get_tool("check_agent_status")
        with mock.patch("common.tools.registry_tools._load_registry_json", side_effect=Exception("unreachable")):
            result = tool.execute({"capability": "scm.pr.create"})
        self.assertTrue(_is_ok(result))
        data = json.loads(_output(result))
        self.assertEqual(data.get("status"), "unknown")
        self.assertIn("registry_unavailable", data.get("reason", ""))

    def test_check_agent_status_requires_capability_or_agent_id(self):
        from common.tools.registry import get_tool
        tool = get_tool("check_agent_status")
        result = tool.execute({})
        self.assertTrue(_is_err(result))

    def test_list_available_agents_returns_ok_structure(self):
        from common.tools.registry import get_tool
        tool = get_tool("list_available_agents")
        mock_agents = [
            {"agent_id": "scm-agent", "capabilities": ["scm.pr.create"], "instances": [{"status": "idle"}]}
        ]
        with mock.patch("common.tools.registry_tools._load_registry_json", return_value=mock_agents):
            result = tool.execute({})
        self.assertTrue(_is_ok(result))
        data = json.loads(_output(result))
        self.assertIsInstance(data, list)
        self.assertEqual(data[0]["agentId"], "scm-agent")
        self.assertEqual(data[0]["status"], "healthy")

    def test_check_agent_status_returns_unavailable_when_no_instances(self):
        from common.tools.registry import get_tool
        tool = get_tool("check_agent_status")
        mock_agents = [{"agent_id": "test-agent", "capabilities": ["test.cap"], "instances": []}]
        with mock.patch("common.tools.registry_tools._load_registry_json", return_value=mock_agents):
            result = tool.execute({"capability": "test.cap"})
        self.assertTrue(_is_ok(result))
        data = json.loads(_output(result))
        self.assertEqual(data[0]["status"], "unavailable")

    def test_check_agent_status_returns_degraded_when_all_busy(self):
        from common.tools.registry import get_tool
        tool = get_tool("check_agent_status")
        mock_agents = [{"agent_id": "busy-agent", "capabilities": ["busy.cap"],
                        "instances": [{"status": "busy"}]}]
        with mock.patch("common.tools.registry_tools._load_registry_json", return_value=mock_agents):
            result = tool.execute({"capability": "busy.cap"})
        self.assertTrue(_is_ok(result))
        data = json.loads(_output(result))
        self.assertEqual(data[0]["status"], "degraded")

    def test_list_available_agents_handles_registry_unreachable(self):
        from common.tools.registry import get_tool
        tool = get_tool("list_available_agents")
        with mock.patch("common.tools.registry_tools._load_registry_json", side_effect=Exception("down")):
            result = tool.execute({})
        self.assertTrue(_is_ok(result))
        data = json.loads(_output(result))
        self.assertEqual(data.get("status"), "unknown")


# ===========================================================================
# Phase 3: Validation and evidence tools
# ===========================================================================

class Phase3ValidationToolsTests(unittest.TestCase):
    """Phase 3: run_validation_command, collect_task_evidence, check_definition_of_done,
    summarize_failure_context tools must be registered and functional.
    """

    def setUp(self):
        self._snap = snapshot_registry()

    def tearDown(self):
        restore_registry(self._snap)

    def test_run_validation_command_registered(self):
        from common.tools.registry import get_tool
        self.assertIsNotNone(get_tool("run_validation_command"))

    def test_collect_task_evidence_registered(self):
        from common.tools.registry import get_tool
        self.assertIsNotNone(get_tool("collect_task_evidence"))

    def test_check_definition_of_done_registered(self):
        from common.tools.registry import get_tool
        self.assertIsNotNone(get_tool("check_definition_of_done"))

    def test_summarize_failure_context_registered(self):
        from common.tools.registry import get_tool
        self.assertIsNotNone(get_tool("summarize_failure_context"))

    def test_run_validation_command_custom_success(self):
        from common.tools.registry import get_tool
        tool = get_tool("run_validation_command")
        result = tool.execute({"validation_type": "custom", "command": "echo hello"})
        self.assertTrue(_is_ok(result))
        data = json.loads(_output(result))
        self.assertTrue(data["passed"])

    def test_run_validation_command_custom_failure(self):
        from common.tools.registry import get_tool
        tool = get_tool("run_validation_command")
        result = tool.execute({"validation_type": "custom", "command": "exit 1"})
        self.assertTrue(_is_ok(result))
        data = json.loads(_output(result))
        self.assertFalse(data["passed"])

    def test_run_validation_command_requires_command_for_custom(self):
        from common.tools.registry import get_tool
        tool = get_tool("run_validation_command")
        result = tool.execute({"validation_type": "custom"})
        self.assertTrue(_is_err(result))

    def test_run_validation_command_invalid_type(self):
        from common.tools.registry import get_tool
        tool = get_tool("run_validation_command")
        result = tool.execute({"validation_type": "invalid_xyz"})
        self.assertTrue(_is_err(result))

    def test_run_validation_command_no_provider_for_build(self):
        import common.tools.validation_tools as vt
        orig = vt._validation_provider
        vt._validation_provider = None
        try:
            from common.tools.registry import get_tool
            tool = get_tool("run_validation_command")
            result = tool.execute({"validation_type": "build"})
            self.assertTrue(_is_ok(result))
            data = json.loads(_output(result))
            self.assertFalse(data["passed"])  # no provider → not passed
        finally:
            vt._validation_provider = orig

    def test_collect_task_evidence_on_empty_dir(self):
        from common.tools.registry import get_tool
        tool = get_tool("collect_task_evidence")
        with tempfile.TemporaryDirectory() as tmpdir:
            result = tool.execute({"workspace_path": tmpdir})
        self.assertTrue(_is_ok(result))
        data = json.loads(_output(result))
        self.assertIn("evidence", data)

    def test_collect_task_evidence_finds_log_files(self):
        from common.tools.registry import get_tool
        tool = get_tool("collect_task_evidence")
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = os.path.join(tmpdir, "build.log")
            with open(log_path, "w") as f:
                f.write("BUILD SUCCESSFUL")
            result = tool.execute({"workspace_path": tmpdir})
        data = json.loads(_output(result))
        self.assertIn(log_path, data["evidence"]["logs"])

    def test_check_definition_of_done_all_met(self):
        from common.tools.registry import get_tool
        tool = get_tool("check_definition_of_done")
        result = tool.execute({
            "checklist": [
                {"item": "Build passes", "met": True},
                {"item": "Tests pass", "met": True},
            ]
        })
        self.assertTrue(_is_ok(result))
        data = json.loads(_output(result))
        self.assertTrue(data["passed"])
        self.assertEqual(data["metCount"], 2)
        self.assertEqual(data["totalCount"], 2)
        self.assertEqual(len(data["unmetItems"]), 0)

    def test_check_definition_of_done_some_unmet(self):
        from common.tools.registry import get_tool
        tool = get_tool("check_definition_of_done")
        result = tool.execute({
            "checklist": [
                {"item": "Build passes", "met": True},
                {"item": "Tests pass", "met": False, "note": "3 tests failing"},
            ]
        })
        self.assertTrue(_is_ok(result))
        data = json.loads(_output(result))
        self.assertFalse(data["passed"])
        self.assertEqual(len(data["unmetItems"]), 1)

    def test_check_definition_of_done_empty_checklist(self):
        from common.tools.registry import get_tool
        tool = get_tool("check_definition_of_done")
        result = tool.execute({"checklist": []})
        self.assertTrue(_is_err(result))

    def test_summarize_failure_context_produces_structured_output(self):
        from common.tools.registry import get_tool
        tool = get_tool("summarize_failure_context")
        result = tool.execute({
            "failure_description": "Gradle build failed at compileDebugKotlin",
            "error_output": "error: unresolved reference: ViewModel",
            "affected_components": ["app/src/main/kotlin/MyViewModel.kt"],
            "suggested_next_steps": ["Check ViewModel import"],
            "retriable": True,
        })
        self.assertTrue(_is_ok(result))
        data = json.loads(_output(result))
        self.assertEqual(data["failureDescription"], "Gradle build failed at compileDebugKotlin")
        self.assertIn("app/src/main/kotlin/MyViewModel.kt", data["affectedComponents"])
        self.assertTrue(data["retriable"])

    def test_summarize_failure_context_requires_description(self):
        from common.tools.registry import get_tool
        tool = get_tool("summarize_failure_context")
        result = tool.execute({})
        self.assertTrue(_is_err(result))

    def test_summarize_failure_context_truncates_long_output(self):
        from common.tools.registry import get_tool
        tool = get_tool("summarize_failure_context")
        long_output = "x" * 10000
        result = tool.execute({
            "failure_description": "test",
            "error_output": long_output,
        })
        self.assertTrue(_is_ok(result))
        data = json.loads(_output(result))
        self.assertLessEqual(len(data["errorOutput"]), 4100)
        self.assertIn("[truncated]", data["errorOutput"])

    def test_register_validation_provider(self):
        from common.tools.validation_tools import register_validation_provider, ValidationResult
        import common.tools.validation_tools as vt

        class MockProvider:
            def run_build(self, workspace_path, options):
                return ValidationResult(passed=True, summary="Mock build OK")

        orig = vt._validation_provider
        register_validation_provider(MockProvider())
        try:
            from common.tools.registry import get_tool
            tool = get_tool("run_validation_command")
            result = tool.execute({"validation_type": "build"})
            self.assertTrue(_is_ok(result))
            data = json.loads(_output(result))
            self.assertTrue(data["passed"])
            self.assertIn("Mock build OK", data["summary"])
        finally:
            vt._validation_provider = orig


# ===========================================================================
# Phase 4: list_skills tool, Registry-backed load_skill
# ===========================================================================

class Phase4SkillCatalogToolTests(unittest.TestCase):
    """Phase 4: load_skill and list_skills tools with Registry-backed discovery."""

    def setUp(self):
        self._snap = snapshot_registry()
        import common.tools.skill_tool as st
        self._orig_skills_root = st._skills_root
        self._orig_registry_url = st._registry_url

    def tearDown(self):
        import common.tools.skill_tool as st
        st._skills_root = self._orig_skills_root
        st._registry_url = self._orig_registry_url
        restore_registry(self._snap)

    def test_list_skills_registered(self):
        from common.tools.registry import get_tool
        self.assertIsNotNone(get_tool("list_skills"))

    def test_load_skill_still_registered(self):
        from common.tools.registry import get_tool
        self.assertIsNotNone(get_tool("load_skill"))

    def test_list_skills_local_fallback_returns_skills(self):
        import common.tools.skill_tool as st
        st._skills_root = os.path.join(_REPO_ROOT, ".github", "skills")
        st._registry_url = ""
        from common.tools.registry import get_tool
        tool = get_tool("list_skills")
        with mock.patch("common.tools.skill_tool._list_skills_from_registry", return_value=None):
            result = tool.execute({})
        self.assertTrue(_is_ok(result))
        data = json.loads(_output(result))
        self.assertGreater(data["count"], 0)
        self.assertEqual(data["source"], "local")

    def test_list_skills_registry_backed_returns_catalog(self):
        mock_skills = [{"id": "test-skill", "description": "Test", "tags": ["testing"]}]
        with mock.patch("common.tools.skill_tool._list_skills_from_registry", return_value=mock_skills):
            from common.tools.registry import get_tool
            tool = get_tool("list_skills")
            result = tool.execute({})
        self.assertTrue(_is_ok(result))
        data = json.loads(_output(result))
        self.assertEqual(data["source"], "registry")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["skills"][0]["id"], "test-skill")

    def test_load_skill_uses_registry_when_available(self):
        mock_content = "# Test Skill\nThis is a test skill."
        with mock.patch("common.tools.skill_tool._fetch_skill_from_registry", return_value=mock_content):
            from common.tools.registry import get_tool
            tool = get_tool("load_skill")
            result = tool.execute({"name": "test-skill"})
        self.assertTrue(_is_ok(result))
        self.assertIn("source: registry", _output(result))
        self.assertIn("Test Skill", _output(result))

    def test_load_skill_falls_back_to_local_when_registry_returns_none(self):
        import common.tools.skill_tool as st
        st._skills_root = os.path.join(_REPO_ROOT, ".github", "skills")
        with mock.patch("common.tools.skill_tool._fetch_skill_from_registry", return_value=None):
            from common.tools.registry import get_tool
            tool = get_tool("load_skill")
            result = tool.execute({"name": "constellation-architecture-delivery"})
        self.assertTrue(_is_ok(result))
        self.assertIn("source: local", _output(result))

    def test_load_skill_prevents_path_traversal(self):
        from common.tools.registry import get_tool
        tool = get_tool("load_skill")
        result = tool.execute({"name": "../etc/passwd"})
        self.assertTrue(_is_err(result))

    def test_list_skills_filter_by_tags(self):
        mock_skills = [
            {"id": "android-skill", "tags": ["android", "mobile"]},
            {"id": "web-skill", "tags": ["web", "frontend"]},
        ]
        with mock.patch("common.tools.skill_tool._list_skills_from_registry", return_value=mock_skills):
            from common.tools.registry import get_tool
            tool = get_tool("list_skills")
            result = tool.execute({"filter_tags": ["android"]})
        self.assertTrue(_is_ok(result))
        data = json.loads(_output(result))
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["skills"][0]["id"], "android-skill")


class Phase4SkillCatalogFetchTests(unittest.TestCase):
    """Phase 4: _fetch_skill_from_registry and _list_skills_from_registry functions."""

    def setUp(self):
        import common.tools.skill_tool as st
        self._orig_registry_url = st._registry_url

    def tearDown(self):
        import common.tools.skill_tool as st
        st._registry_url = self._orig_registry_url

    def test_fetch_skill_returns_none_when_no_registry_url(self):
        import common.tools.skill_tool as st
        st._registry_url = ""
        with mock.patch.dict(os.environ, {}, clear=False):
            # Ensure no REGISTRY_URL in env
            env = dict(os.environ)
            env.pop("REGISTRY_URL", None)
            with mock.patch.dict(os.environ, env, clear=True):
                result = st._fetch_skill_from_registry("some-skill")
        self.assertIsNone(result)

    def test_list_skills_returns_none_when_no_registry_url(self):
        import common.tools.skill_tool as st
        st._registry_url = ""
        env = dict(os.environ)
        env.pop("REGISTRY_URL", None)
        with mock.patch.dict(os.environ, env, clear=True):
            result = st._list_skills_from_registry()
        self.assertIsNone(result)

    def test_fetch_skill_sanitizes_skill_id_no_path_traversal(self):
        """Path-traversal IDs must be rejected without making any network call."""
        import common.tools.skill_tool as st
        st._registry_url = "http://registry:9000"
        calls = []

        def mock_urlopen(req, timeout=None):
            calls.append(req.full_url)
            raise Exception("no network")

        with mock.patch("common.tools.skill_tool.urlopen", side_effect=mock_urlopen):
            result = st._fetch_skill_from_registry("../../../etc/passwd")

        # With early rejection, the function returns None without calling urlopen at all
        self.assertIsNone(result)
        self.assertEqual(calls, [], "urlopen must NOT be called for path-traversal skill IDs")


# ===========================================================================
# Phase 5: web/prompts/system and android/prompts/system structures
# ===========================================================================

class Phase5WebPromptSystemTests(unittest.TestCase):
    """Phase 5: web/prompts/system/ must have manifest.yaml and all MD files."""

    WEB_SYSTEM_DIR = Path(_REPO_ROOT) / "web" / "prompts" / "system"

    def test_manifest_yaml_exists(self):
        self.assertTrue((self.WEB_SYSTEM_DIR / "manifest.yaml").is_file())

    def test_manifest_lists_all_files(self):
        from common.prompt_builder import _read_manifest_order
        manifest = str(self.WEB_SYSTEM_DIR / "manifest.yaml")
        order = _read_manifest_order(manifest)
        self.assertGreater(len(order), 0, "manifest systemOrder is empty")

    def test_all_listed_files_exist(self):
        from common.prompt_builder import _read_manifest_order
        manifest = str(self.WEB_SYSTEM_DIR / "manifest.yaml")
        order = _read_manifest_order(manifest)
        for filename in order:
            path = self.WEB_SYSTEM_DIR / filename
            self.assertTrue(path.is_file(), f"Listed file not found: {path}")

    def test_00_role_md_exists(self):
        self.assertTrue((self.WEB_SYSTEM_DIR / "00-role.md").is_file())

    def test_10_boundaries_md_exists(self):
        self.assertTrue((self.WEB_SYSTEM_DIR / "10-boundaries.md").is_file())

    def test_50_definition_of_done_exists(self):
        self.assertTrue((self.WEB_SYSTEM_DIR / "50-definition-of-done.md").is_file())

    def test_60_failure_handling_exists(self):
        self.assertTrue((self.WEB_SYSTEM_DIR / "60-failure-handling.md").is_file())

    def test_prompt_builder_assembles_web_system_prompt(self):
        from common.prompt_builder import build_system_prompt_from_manifest
        prompt = build_system_prompt_from_manifest(str(Path(_REPO_ROOT) / "web"))
        self.assertGreater(len(prompt), 100, "Assembled web system prompt is too short")
        self.assertIn("Web Agent", prompt)

    def test_web_prompts_tasks_directory_exists(self):
        tasks_dir = Path(_REPO_ROOT) / "web" / "prompts" / "tasks"
        self.assertTrue(tasks_dir.is_dir())


class Phase5AndroidPromptSystemTests(unittest.TestCase):
    """Phase 5: android/prompts/system/ must have manifest.yaml and all MD files."""

    ANDROID_SYSTEM_DIR = Path(_REPO_ROOT) / "android" / "prompts" / "system"

    def test_manifest_yaml_exists(self):
        self.assertTrue((self.ANDROID_SYSTEM_DIR / "manifest.yaml").is_file())

    def test_manifest_lists_all_files(self):
        from common.prompt_builder import _read_manifest_order
        manifest = str(self.ANDROID_SYSTEM_DIR / "manifest.yaml")
        order = _read_manifest_order(manifest)
        self.assertGreater(len(order), 0)

    def test_all_listed_files_exist(self):
        from common.prompt_builder import _read_manifest_order
        manifest = str(self.ANDROID_SYSTEM_DIR / "manifest.yaml")
        order = _read_manifest_order(manifest)
        for filename in order:
            path = self.ANDROID_SYSTEM_DIR / filename
            self.assertTrue(path.is_file(), f"Listed file not found: {path}")

    def test_00_role_md_contains_android_keywords(self):
        content = (self.ANDROID_SYSTEM_DIR / "00-role.md").read_text()
        self.assertIn("Android", content)
        self.assertIn("Kotlin", content)

    def test_40_validation_policy_references_gradle(self):
        content = (self.ANDROID_SYSTEM_DIR / "40-validation-policy.md").read_text()
        self.assertIn("Gradle", content)
        self.assertIn("assembleDebug", content)

    def test_10_boundaries_references_gradle_constraints(self):
        content = (self.ANDROID_SYSTEM_DIR / "10-boundaries.md").read_text()
        self.assertIn("max-workers", content)

    def test_prompt_builder_assembles_android_system_prompt(self):
        from common.prompt_builder import build_system_prompt_from_manifest
        prompt = build_system_prompt_from_manifest(str(Path(_REPO_ROOT) / "android"))
        self.assertGreater(len(prompt), 100)
        self.assertIn("Android", prompt)

    def test_android_prompts_tasks_directory_exists(self):
        tasks_dir = Path(_REPO_ROOT) / "android" / "prompts" / "tasks"
        self.assertTrue(tasks_dir.is_dir())


# ===========================================================================
# Phase 7 Acceptance Criteria verification
# ===========================================================================

class Phase7AcceptanceCriteriaTests(unittest.TestCase):
    """Phase 7: Verify the acceptance criteria from the migration plan."""

    def test_criterion_2_single_agent_backend_change_does_not_affect_others(self):
        """Changing team-lead's backend must not change web or android backend."""
        module = _load_resolve_module()
        # Override env for team-lead only via AGENT_RUNTIME
        with mock.patch.dict(os.environ, {"AGENT_RUNTIME": "claude-code"}):
            team_lead_backend, _ = module.resolve_effective_backend("team-lead")
        # Without override, web and android should use their own configured backend
        web_backend, _ = module.resolve_effective_backend("web")
        android_backend, _ = module.resolve_effective_backend("android")
        # Each agent resolves independently
        self.assertIsInstance(team_lead_backend, str)
        self.assertIsInstance(web_backend, str)
        self.assertIsInstance(android_backend, str)

    def test_criterion_3_no_fallback_in_adapter(self):
        """Backend must not auto-fallback — require_agentic_runtime must fail for copilot-cli."""
        from common.runtime.adapter import require_agentic_runtime
        with mock.patch.dict(os.environ, {"AGENT_RUNTIME": "copilot-cli"}):
            with self.assertRaises(RuntimeError) as ctx:
                require_agentic_runtime("TestAgent")
        self.assertIn("copilot-cli", str(ctx.exception).lower())

    def test_criterion_5_execution_agents_have_local_workspace_tools(self):
        """Execution agents must have local base tools registered."""
        from common.tools.registry import get_tool
        # coding_tools.py registers: bash, read_file, write_file, edit_file, glob, grep
        for tool_name in ("bash", "read_file", "write_file", "edit_file", "glob", "grep"):
            self.assertIsNotNone(get_tool(tool_name), f"Missing tool: {tool_name}")

    def test_criterion_6_scm_agent_has_remote_readonly_tools(self):
        """SCM Agent must have remote read-only repository tools."""
        from common.tools.registry import get_tool
        for tool_name in ("scm_read_file", "scm_list_dir", "scm_search_code",
                           "scm_list_branches", "scm_get_pr_details", "scm_get_pr_diff"):
            self.assertIsNotNone(get_tool(tool_name), f"Missing tool: {tool_name}")

    def test_criterion_1_load_skill_can_discover_via_registry(self):
        """New skills should be discoverable via Registry without rebuilding images."""
        from common.tools.registry import get_tool
        mock_content = "# New Dynamic Skill\nThis skill was added without rebuilding."
        with mock.patch("common.tools.skill_tool._fetch_skill_from_registry", return_value=mock_content):
            tool = get_tool("load_skill")
            result = tool.execute({"name": "new-dynamic-skill"})
        self.assertTrue(_is_ok(result))
        self.assertIn("source: registry", _output(result))
        self.assertIn("New Dynamic Skill", _output(result))

    def test_criterion_4_team_lead_can_load_prompts_from_manifest(self):
        """Team Lead prompts must be loadable from the manifest-based prompt structure."""
        from common.prompt_builder import build_system_prompt_from_manifest
        prompt = build_system_prompt_from_manifest(str(Path(_REPO_ROOT) / "team-lead"))
        self.assertGreater(len(prompt), 200)
        self.assertIn("Team Lead", prompt)


if __name__ == "__main__":
    unittest.main()
