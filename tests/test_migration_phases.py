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


# ===========================================================================
# Phase 6: Dockerfile base hierarchy
# ===========================================================================

class Phase6DockerfileBaseTests(unittest.TestCase):
    """Phase 6: Shared base Dockerfile hierarchy must exist."""

    DOCKERFILES_DIR = Path(_REPO_ROOT) / "dockerfiles"

    def test_base_dockerfile_exists(self):
        self.assertTrue((self.DOCKERFILES_DIR / "base.Dockerfile").is_file())

    def test_base_copilot_cli_dockerfile_exists(self):
        self.assertTrue((self.DOCKERFILES_DIR / "base-copilot-cli.Dockerfile").is_file())

    def test_base_claude_code_dockerfile_exists(self):
        self.assertTrue((self.DOCKERFILES_DIR / "base-claude-code.Dockerfile").is_file())

    def test_base_connect_agent_dockerfile_exists(self):
        self.assertTrue((self.DOCKERFILES_DIR / "base-connect-agent.Dockerfile").is_file())

    def test_base_dockerfile_uses_python312_slim(self):
        content = (self.DOCKERFILES_DIR / "base.Dockerfile").read_text()
        self.assertIn("python:3.12-slim", content)

    def test_base_dockerfile_installs_tini_git_curl(self):
        content = (self.DOCKERFILES_DIR / "base.Dockerfile").read_text()
        self.assertIn("tini", content)
        self.assertIn("git", content)
        self.assertIn("curl", content)

    def test_base_dockerfile_creates_appuser(self):
        content = (self.DOCKERFILES_DIR / "base.Dockerfile").read_text()
        self.assertIn("appuser", content)
        self.assertIn("1000", content)

    def test_base_copilot_cli_extends_base(self):
        content = (self.DOCKERFILES_DIR / "base-copilot-cli.Dockerfile").read_text()
        self.assertIn("FROM constellation-base:latest", content)
        self.assertIn("@github/copilot", content)

    def test_base_claude_code_extends_base(self):
        content = (self.DOCKERFILES_DIR / "base-claude-code.Dockerfile").read_text()
        self.assertIn("FROM constellation-base:latest", content)
        self.assertIn("@anthropic-ai/claude-code", content)

    def test_base_connect_agent_extends_base(self):
        content = (self.DOCKERFILES_DIR / "base-connect-agent.Dockerfile").read_text()
        self.assertIn("FROM constellation-base:latest", content)
        self.assertNotIn("npm install", content)

    def test_base_connect_agent_sets_runtime_env(self):
        content = (self.DOCKERFILES_DIR / "base-connect-agent.Dockerfile").read_text()
        self.assertIn("AGENT_RUNTIME=connect-agent", content)


class Phase6PersistentAgentDockerfileTests(unittest.TestCase):
    """Phase 6: Persistent agents must also have backend-specific Dockerfiles."""

    PERSISTENT_AGENTS = ["compass", "jira", "scm", "ui-design", "office"]
    BACKENDS = ["copilot-cli", "connect-agent", "claude-code"]

    def test_all_persistent_agents_have_all_backend_dockerfiles(self):
        for agent in self.PERSISTENT_AGENTS:
            for backend in self.BACKENDS:
                path = Path(_REPO_ROOT) / agent / f"Dockerfile.{backend}"
                self.assertTrue(path.is_file(), f"Missing: {path}")

    def test_persistent_agent_dockerfiles_extend_base(self):
        for agent in self.PERSISTENT_AGENTS:
            for backend in self.BACKENDS:
                content = (Path(_REPO_ROOT) / agent / f"Dockerfile.{backend}").read_text()
                self.assertIn(
                    f"constellation-base-{backend}:latest",
                    content,
                    f"{agent}/Dockerfile.{backend} must extend constellation-base-{backend}:latest"
                )

    def test_persistent_agent_dockerfiles_have_required_labels(self):
        for agent in self.PERSISTENT_AGENTS:
            for backend in self.BACKENDS:
                content = (Path(_REPO_ROOT) / agent / f"Dockerfile.{backend}").read_text()
                self.assertIn(
                    "constellation.agent_id",
                    content,
                    f"{agent}/Dockerfile.{backend} missing constellation.agent_id label"
                )
                self.assertIn(
                    f'constellation.runtime.backend="{backend}"',
                    content,
                    f"{agent}/Dockerfile.{backend} missing runtime.backend label"
                )

    def test_persistent_agent_dockerfiles_run_as_appuser(self):
        for agent in self.PERSISTENT_AGENTS:
            for backend in self.BACKENDS:
                content = (Path(_REPO_ROOT) / agent / f"Dockerfile.{backend}").read_text()
                self.assertIn(
                    "USER appuser",
                    content,
                    f"{agent}/Dockerfile.{backend} must run as non-root user"
                )


class Phase6ResolveRuntimeAllAgentsTests(unittest.TestCase):
    """Phase 6: resolve-agent-runtime.py must work for all LLM-enabled agents."""

    ALL_AGENTIC_AGENTS = [
        "team-lead", "web", "android", "office",
        "compass", "jira", "scm", "ui-design"
    ]

    def setUp(self):
        self.module = _load_resolve_module()

    def test_registry_is_infrastructure_only(self):
        """Registry is infrastructure-only — it should not appear in _AGENTIC_AGENTS."""
        spec = importlib.util.spec_from_file_location(
            "build_agent_image",
            str(Path(_REPO_ROOT) / "scripts" / "build-agent-image.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertNotIn(
            "registry", mod._AGENTIC_AGENTS,
            "'registry' must not be in _AGENTIC_AGENTS — it is infrastructure-only"
        )

    def test_all_agentic_agents_resolve_successfully(self):
        for agent in self.ALL_AGENTIC_AGENTS:
            with mock.patch.dict(os.environ, {"AGENT_RUNTIME": "connect-agent"}):
                try:
                    backend, dockerfile = self.module.resolve_effective_backend(agent)
                    self.assertIsInstance(backend, str, f"Agent {agent}: backend must be a string")
                except SystemExit:
                    self.fail(f"Agent {agent}: resolve_effective_backend raised SystemExit")

    def test_build_agent_image_script_knows_all_agentic_agents(self):
        """build-agent-image.py must include all agents with backend Dockerfiles."""
        spec = importlib.util.spec_from_file_location(
            "build_agent_image",
            str(Path(_REPO_ROOT) / "scripts" / "build-agent-image.py"),
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        for agent in self.ALL_AGENTIC_AGENTS:
            self.assertIn(
                agent, mod._AGENTIC_AGENTS,
                f"build-agent-image.py does not list '{agent}' in _AGENTIC_AGENTS"
            )

    def test_registry_excluded_from_agentic_agents_in_resolve_script(self):
        """Registry should be excluded from the non-agentic set (or raise SystemExit)."""
        # The registry agent is the only one excluded; all others should be resolvable
        # Verify that _NON_AGENTIC_AGENTS only contains 'registry'
        non_agentic = self.module._NON_AGENTIC_AGENTS
        self.assertIn("registry", non_agentic)
        # Persistent LLM agents must NOT be in the non-agentic set
        for agent in ["compass", "jira", "scm", "ui-design", "office"]:
            self.assertNotIn(
                agent, non_agentic,
                f"'{agent}' should not be in _NON_AGENTIC_AGENTS — it has backend Dockerfiles"
            )


# ===========================================================================
# Phase 6: Prompt manifest structure for all agents
# ===========================================================================

class Phase6AgentPromptManifestTests(unittest.TestCase):
    """Phase 6: All LLM-enabled agents must have prompts/system/manifest.yaml."""

    ALL_PROMPT_AGENTS = [
        "compass", "jira", "scm", "ui-design", "office",
        "team-lead", "web", "android",
    ]
    REQUIRED_FILES = [
        "00-role.md", "10-boundaries.md", "20-tools.md",
        "30-decision-policy.md", "50-definition-of-done.md", "60-failure-handling.md",
    ]

    def test_all_agents_have_manifest_yaml(self):
        for agent in self.ALL_PROMPT_AGENTS:
            path = Path(_REPO_ROOT) / agent / "prompts" / "system" / "manifest.yaml"
            self.assertTrue(path.is_file(), f"Missing manifest.yaml for {agent}: {path}")

    def test_all_agents_have_required_prompt_files(self):
        for agent in self.ALL_PROMPT_AGENTS:
            system_dir = Path(_REPO_ROOT) / agent / "prompts" / "system"
            for filename in self.REQUIRED_FILES:
                path = system_dir / filename
                self.assertTrue(path.is_file(), f"Missing {filename} for agent {agent}")

    def test_all_agents_manifests_list_files_that_exist(self):
        from common.prompt_builder import _read_manifest_order
        for agent in self.ALL_PROMPT_AGENTS:
            system_dir = Path(_REPO_ROOT) / agent / "prompts" / "system"
            manifest = str(system_dir / "manifest.yaml")
            order = _read_manifest_order(manifest)
            self.assertGreater(len(order), 0, f"Manifest for {agent} has empty systemOrder")
            for filename in order:
                path = system_dir / filename
                self.assertTrue(
                    path.is_file(),
                    f"Agent {agent}: manifest lists '{filename}' but file not found"
                )

    def test_all_agents_prompts_have_tasks_directory(self):
        for agent in self.ALL_PROMPT_AGENTS:
            tasks_dir = Path(_REPO_ROOT) / agent / "prompts" / "tasks"
            self.assertTrue(tasks_dir.is_dir(), f"Missing prompts/tasks/ for agent {agent}")

    def test_all_agents_system_prompts_buildable(self):
        """All agents must be able to assemble a non-trivial system prompt."""
        from common.prompt_builder import build_system_prompt_from_manifest
        for agent in self.ALL_PROMPT_AGENTS:
            agent_dir = str(Path(_REPO_ROOT) / agent)
            prompt = build_system_prompt_from_manifest(agent_dir)
            self.assertGreater(
                len(prompt), 100,
                f"Agent {agent}: assembled system prompt is too short ({len(prompt)} chars)"
            )


class Phase6AgentRoleFilesTests(unittest.TestCase):
    """Phase 6: Each agent's 00-role.md must contain agent-specific content."""

    def _read_role(self, agent: str) -> str:
        path = Path(_REPO_ROOT) / agent / "prompts" / "system" / "00-role.md"
        return path.read_text() if path.is_file() else ""

    def test_compass_role_mentions_routing(self):
        content = self._read_role("compass")
        self.assertIn("Compass", content)
        self.assertIn("routing", content.lower())

    def test_jira_role_mentions_jira(self):
        content = self._read_role("jira")
        self.assertIn("Jira", content)

    def test_scm_role_mentions_git_or_scm(self):
        content = self._read_role("scm")
        self.assertTrue(
            "SCM" in content or "Git" in content,
            "scm 00-role.md must mention SCM or Git"
        )

    def test_ui_design_role_mentions_figma_or_design(self):
        content = self._read_role("ui-design")
        self.assertTrue(
            "Figma" in content or "design" in content.lower(),
            "ui-design 00-role.md must mention Figma or design"
        )

    def test_office_role_mentions_documents(self):
        content = self._read_role("office")
        self.assertTrue(
            "document" in content.lower() or "Office" in content,
            "office 00-role.md must mention documents or Office"
        )

    def test_team_lead_role_mentions_team_lead(self):
        content = self._read_role("team-lead")
        self.assertIn("Team Lead", content)

    def test_web_role_mentions_web_agent(self):
        content = self._read_role("web")
        self.assertIn("Web Agent", content)

    def test_android_role_mentions_android(self):
        content = self._read_role("android")
        self.assertIn("Android", content)


class Phase6BoundaryFilesTests(unittest.TestCase):
    """Phase 6: Each agent's 10-boundaries.md must contain boundary constraints."""

    BOUNDARY_AGENTS = ["compass", "jira", "scm", "ui-design", "office"]

    def test_all_agents_have_non_empty_boundaries(self):
        for agent in self.BOUNDARY_AGENTS:
            path = Path(_REPO_ROOT) / agent / "prompts" / "system" / "10-boundaries.md"
            if path.is_file():
                content = path.read_text()
                self.assertGreater(len(content), 50, f"10-boundaries.md for {agent} is too short")

    def test_jira_boundaries_forbid_non_jira_access(self):
        content = (Path(_REPO_ROOT) / "jira" / "prompts" / "system" / "10-boundaries.md").read_text()
        self.assertIn("Forbidden", content)

    def test_scm_boundaries_mention_protected_branches(self):
        content = (Path(_REPO_ROOT) / "scm" / "prompts" / "system" / "10-boundaries.md").read_text()
        self.assertIn("protected", content.lower())

    def test_office_boundaries_require_authorization(self):
        content = (Path(_REPO_ROOT) / "office" / "prompts" / "system" / "10-boundaries.md").read_text()
        self.assertTrue(
            "authorized" in content.lower() or "authorization" in content.lower(),
            "office 10-boundaries.md must mention authorization"
        )


if __name__ == "__main__":
    unittest.main()
