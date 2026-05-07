"""Android Agent-specific helpers for runtime-driven execution.

Mirrors the agent-local workflow module pattern used by Web Agent:
Android implementation specialist that clones repos, writes Kotlin/Android code,
runs Gradle builds, validates tests, and creates PRs — all driven by the
agentic runtime via tools.

Key exports
-----------
ANDROID_AGENT_RUNTIME_TOOL_NAMES   — tool list to pass to runtime.run_agentic()
AndroidValidationProvider          — Gradle-backed implementation of ValidationResult
build_android_agent_runtime_config — stage-summary runtime config dict
configure_android_agent_control_tools — wires lifecycle callbacks into control_tools
build_android_task_prompt          — constructs the task prompt for run_agentic()
register_android_validation_provider — registers AndroidValidationProvider globally
"""

from __future__ import annotations

import json
import os
import re
import select
import subprocess
import time

from common.runtime.adapter import summarize_runtime_configuration
from common.tools.control_tools import configure_control_tools
from common.tools.validation_tools import ValidationResult, register_validation_provider

# ---------------------------------------------------------------------------
# Tool names exposed to the agentic runtime backend
# ---------------------------------------------------------------------------

ANDROID_AGENT_RUNTIME_TOOL_NAMES = [
    # --- Control lifecycle ---
    "complete_current_task",
    "fail_current_task",
    "request_user_input",
    "report_progress",
    "get_task_context",
    "get_agent_runtime_status",
    # --- Planning ---
    "todo_write",
    # --- Skill discovery ---
    "load_skill",
    "list_skills",
    # --- Registry / agent status ---
    "registry_query",
    "check_agent_status",
    # --- Local workspace (canonical names) ---
    "read_local_file",
    "write_local_file",
    "edit_local_file",
    "list_local_dir",
    "search_local_files",
    "run_local_command",
    # --- Local workspace (legacy aliases — kept for backend compat) ---
    "read_file",
    "write_file",
    "edit_file",
    "glob",
    "grep",
    "bash",
    # --- SCM boundary tools (via SCM Agent) ---
    "scm_clone_repo",
    "scm_get_default_branch",
    "scm_get_branch_rules",
    "scm_list_branches",
    "scm_create_branch",
    "scm_push_files",
    "scm_create_pr",
    "scm_get_pr_details",
    "scm_get_pr_diff",
    "scm_read_file",
    "scm_list_dir",
    "scm_search_code",
    "scm_repo_inspect",
    # --- Jira boundary tools (via Jira Agent) ---
    "jira_get_ticket",
    "jira_add_comment",
    # --- Validation and evidence ---
    "run_validation_command",
    "collect_task_evidence",
    "check_definition_of_done",
    "summarize_failure_context",
    # --- Design context (supplemental, when Team Lead context is truncated) ---
    "design_fetch_figma_screen",
    "design_fetch_stitch_screen",
]

# Default skill playbooks loaded into the Android agent system prompt.
DEFAULT_ANDROID_AGENT_SKILL_PLAYBOOKS = [
    "constellation-generic-agent-workflow",
    "constellation-architecture-delivery",
    "constellation-backend-delivery",
    "constellation-code-review-delivery",
    "constellation-testing-delivery",
    "constellation-ui-evidence-delivery",
    "android-compose-delivery",
]


# ---------------------------------------------------------------------------
# Runtime config summary
# ---------------------------------------------------------------------------

def build_android_agent_runtime_config(skill_playbooks=None) -> dict:
    """Return runtime config dict for stage-summary.json."""
    return {
        "runtime": summarize_runtime_configuration(),
        "skillPlaybooks": list(skill_playbooks or DEFAULT_ANDROID_AGENT_SKILL_PLAYBOOKS),
    }


# ---------------------------------------------------------------------------
# Tool wiring
# ---------------------------------------------------------------------------

def configure_android_agent_control_tools(
    *,
    task_id: str,
    agent_id: str,
    workspace: str,
    permissions: dict | None,
    compass_task_id: str,
    callback_url: str,
    orchestrator_url: str,
    user_text: str,
    wait_for_input_fn=None,
) -> None:
    """Wire lifecycle callbacks into common control_tools for this task.

    Called by app.py before run_agentic(). The complete_fn and fail_fn are
    left as no-ops here; app.py reads result.success from run_agentic() to
    drive the final task state update, which avoids double-write races.
    """
    configure_control_tools(
        task_context={
            "taskId": task_id,
            "agentId": agent_id,
            "workspacePath": workspace,
            "permissions": permissions,
            "compassTaskId": compass_task_id,
            "callbackUrl": callback_url,
            "orchestratorUrl": orchestrator_url,
            "userText": user_text[:500],
        },
        complete_fn=lambda result, artifacts: None,
        fail_fn=lambda error: None,
        input_required_fn=lambda question, context: None,
        wait_for_input_fn=wait_for_input_fn,
    )


# ---------------------------------------------------------------------------
# Task prompt builder
# ---------------------------------------------------------------------------

def build_android_task_prompt(
    *,
    user_text: str,
    workspace: str,
    compass_task_id: str,
    android_task_id: str,
    acceptance_criteria: list | None = None,
    is_revision: bool = False,
    review_issues: list | None = None,
    tech_stack_constraints: dict | None = None,
    design_context: dict | None = None,
    target_repo_url: str = "",
    jira_context: str = "",
    ticket_key: str = "",
    permissions: dict | None = None,
) -> str:
    """Build the task prompt forwarded to runtime.run_agentic().

    Loads ``android/prompts/tasks/implement.md`` as a template and renders
    it with the task-specific context, mirroring web_agentic_workflow behaviour.
    """
    from common.prompt_builder import build_task_prompt

    # Normalize sections
    criteria_text = (
        "\n".join(f"- {c}" for c in acceptance_criteria)
        if acceptance_criteria
        else "Not specified."
    )
    issues_text = (
        "\n".join(f"- {i}" for i in review_issues) if review_issues else ""
    )
    tech_text = (
        json.dumps(tech_stack_constraints, ensure_ascii=False)
        if tech_stack_constraints
        else "None"
    )
    design_text = str(design_context.get("content") or "") if design_context else ""
    design_url = str(design_context.get("url") or "") if design_context else ""

    revision_section = ""
    if is_revision and issues_text:
        revision_section = (
            "## REVISION REQUEST\n"
            "This is a revision. Fix ALL of the following issues from the previous implementation:\n"
            f"{issues_text}"
        )

    jira_section = ""
    if ticket_key and jira_context:
        jira_section = (
            f"## Jira Ticket Context ({ticket_key})\n{jira_context[:3000]}"
        )
    elif ticket_key:
        jira_section = f"## Jira Ticket\nKey: {ticket_key}"

    design_section = ""
    if design_url or design_text:
        design_section = (
            "## Design Context\n"
            f"URL: {design_url or '(see content below)'}\n"
            f"{design_text[:2000] if design_text else ''}"
        )

    task_template = build_task_prompt(
        os.path.join(os.path.dirname(__file__), "..", "android"), "implement"
    )
    if not task_template:
        raise RuntimeError(
            "Missing android agent task prompt template: android/prompts/tasks/implement.md"
        )

    return task_template.format(
        user_text=user_text,
        criteria_text=criteria_text,
        tech_text=tech_text,
        jira_section=jira_section or "Not provided.",
        design_section=design_section or "Not provided.",
        revision_section=revision_section.strip() or "Not a revision task.",
        target_repo_url=target_repo_url or "(detect from context)",
        ticket_key=ticket_key or "none",
        workspace=workspace or "(no shared workspace provided)",
        compass_task_id=compass_task_id or "",
        android_task_id=android_task_id,
    )


# ---------------------------------------------------------------------------
# Android Gradle validation provider
# ---------------------------------------------------------------------------

def _resolve_android_sdk_dir() -> str:
    for key in ("ANDROID_HOME", "ANDROID_SDK_ROOT"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


def _android_gradle_env(build_dir: str | None = None) -> dict[str, str]:
    env = dict(os.environ)
    sdk_dir = _resolve_android_sdk_dir()
    if sdk_dir:
        env.setdefault("ANDROID_HOME", sdk_dir)
        env.setdefault("ANDROID_SDK_ROOT", sdk_dir)
    if build_dir:
        env.setdefault("GRADLE_USER_HOME", os.path.join(build_dir, ".gradle-agent"))
    env["CI"] = "true"
    return env


def _android_gradle_base_args() -> list[str]:
    jvm_args = os.environ.get("ANDROID_GRADLE_JVM_ARGS", "-Xmx2g -Dfile.encoding=UTF-8").strip()
    max_workers = os.environ.get("ANDROID_GRADLE_MAX_WORKERS", "1").strip()
    args = [
        f"--max-workers={max_workers}" if max_workers else "",
        "--no-daemon",
        "--console=plain",
        "-Pkotlin.compiler.execution.strategy=in-process",
        "-Dkotlin.daemon.enabled=false",
        "-Dorg.gradle.vfs.watch=false",
    ]
    args = [a for a in args if a]
    if jvm_args:
        args.append(f"-Dorg.gradle.jvmargs={jvm_args}")
    return args


def _ensure_gradle_wrapper_executable(build_dir: str) -> str:
    wrapper = os.path.join(build_dir, "gradlew")
    if os.path.isfile(wrapper):
        current_mode = os.stat(wrapper).st_mode
        os.chmod(wrapper, current_mode | 0o111)
        return "./gradlew"
    return "gradle"


def _clear_stale_gradle_locks(build_dir: str) -> None:
    gradle_home = _android_gradle_env(build_dir).get(
        "GRADLE_USER_HOME",
        os.path.join(build_dir, ".gradle-agent"),
    )
    lock_path = os.path.join(gradle_home, "caches", "journal-1", "journal-1.lock")
    if os.path.isfile(lock_path):
        try:
            os.remove(lock_path)
        except OSError:
            pass


def _prepare_gradle_user_home_properties(build_dir: str) -> None:
    env = _android_gradle_env(build_dir)
    gradle_home = env.get("GRADLE_USER_HOME", "")
    if not gradle_home:
        return
    jvm_args = os.environ.get("ANDROID_GRADLE_JVM_ARGS", "-Xmx2g -Dfile.encoding=UTF-8").strip()
    try:
        os.makedirs(gradle_home, exist_ok=True)
        props_path = os.path.join(gradle_home, "gradle.properties")
        with open(props_path, "w", encoding="utf-8") as fh:
            fh.write("# Written by Android Agent — overrides project gradle.properties JVM heap\n")
            fh.write(f"org.gradle.jvmargs={jvm_args}\n")
            fh.write("org.gradle.daemon=false\n")
            fh.write("android.dexBuilderWorkerCount=1\n")
    except OSError:
        pass


def _prepare_android_local_properties(build_dir: str) -> None:
    sdk_dir = _resolve_android_sdk_dir()
    if not sdk_dir:
        return
    local_properties_path = os.path.join(build_dir, "local.properties")
    desired_line = f"sdk.dir={sdk_dir}"
    if os.path.isfile(local_properties_path):
        try:
            with open(local_properties_path, encoding="utf-8") as fh:
                existing = fh.read().strip()
            if desired_line in existing.splitlines():
                return
        except OSError:
            pass
    try:
        with open(local_properties_path, "w", encoding="utf-8") as fh:
            fh.write(f"{desired_line}\n")
    except OSError:
        pass


def _terminate_subprocess(process: subprocess.Popen) -> None:  # type: ignore[type-arg]
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _run_streaming_command(
    cmd: list[str],
    *,
    cwd: str,
    env: dict[str, str],
    timeout_seconds: int,
    label: str,
) -> tuple[int, str]:
    """Run a command with live log streaming and quiet-period heartbeats."""
    heartbeat_seconds = max(5, int(os.environ.get("ANDROID_GRADLE_HEARTBEAT_SECONDS", "20")))
    started_at = time.monotonic()
    last_output_at = started_at
    output_chunks: list[str] = []
    process = subprocess.Popen(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    def _record_text(raw_text: str) -> None:
        if not raw_text:
            return
        output_chunks.append(raw_text)
        for line in raw_text.splitlines():
            stripped = line.rstrip()
            if stripped:
                print(f"[android-agent][{label}] {stripped}")

    try:
        stream = process.stdout
        if stream is None:
            return_code = process.wait(timeout=timeout_seconds)
            return return_code, ""

        while True:
            now = time.monotonic()
            elapsed = now - started_at
            if elapsed >= timeout_seconds:
                msg = f"[{label}] timed out after {timeout_seconds}s"
                print(f"[android-agent] {msg}")
                output_chunks.append(msg + "\n")
                _terminate_subprocess(process)
                break

            ready, _, _ = select.select([stream], [], [], 1.0)
            if ready:
                line = stream.readline()
                if line == "":
                    if process.poll() is not None:
                        break
                    continue
                _record_text(line)
                last_output_at = time.monotonic()
                continue

            if process.poll() is not None:
                trailing = stream.read()
                _record_text(trailing)
                break

            quiet_for = time.monotonic() - last_output_at
            if quiet_for >= heartbeat_seconds:
                print(f"[android-agent][{label}] still running after {int(elapsed)}s")
                last_output_at = time.monotonic()

        return_code = process.wait(timeout=10)
        return return_code, "".join(output_chunks).strip()
    finally:
        if process.stdout is not None:
            process.stdout.close()


def _run_gradle_task(build_dir: str, task_name: str, options: dict) -> ValidationResult:
    """Run a single Gradle task and return a structured ValidationResult."""
    gradle_cmd = _ensure_gradle_wrapper_executable(build_dir)
    timeout = int(options.get("timeout") or os.environ.get("ANDROID_GRADLE_STEP_TIMEOUT_SECONDS", "1800"))

    _clear_stale_gradle_locks(build_dir)
    _prepare_gradle_user_home_properties(build_dir)
    _prepare_android_local_properties(build_dir)

    cmd = [gradle_cmd, *_android_gradle_base_args(), task_name]
    env = _android_gradle_env(build_dir)

    return_code, output = _run_streaming_command(
        cmd,
        cwd=build_dir,
        env=env,
        timeout_seconds=timeout,
        label=task_name,
    )
    passed = return_code == 0
    snippet = output[-4000:] if len(output) > 4000 else output
    return ValidationResult(
        passed=passed,
        summary=f"Gradle {task_name} {'succeeded' if passed else 'failed'} (exit {return_code})",
        details=[
            {
                "check_name": task_name,
                "status": "passed" if passed else "failed",
                "output_snippet": snippet,
            }
        ],
        retriable=not passed,
        suggested_fix=(
            "Read the full Gradle error output above, fix the root cause, "
            "clear journal lock files, and re-run the same Gradle task."
            if not passed
            else None
        ),
    )


class AndroidValidationProvider:
    """Gradle-backed validation provider for the Android Agent.

    Registered with common.tools.validation_tools.register_validation_provider()
    so that run_validation_command tool calls are routed to Gradle.
    """

    def run_build(self, workspace_path: str, options: dict) -> ValidationResult:
        return _run_gradle_task(workspace_path, "assembleDebug", options)

    def run_unit_test(self, workspace_path: str, options: dict) -> ValidationResult:
        return _run_gradle_task(workspace_path, "testDebugUnitTest", options)

    def run_integration_test(self, workspace_path: str, options: dict) -> ValidationResult:
        # Instrumentation tests require a connected device; treat as skipped.
        return ValidationResult(
            passed=True,
            summary="Integration/instrumentation tests skipped — no connected device in this environment.",
            details=[
                {
                    "check_name": "android_integration_test",
                    "status": "skipped",
                    "output_snippet": "No emulator or device available.",
                }
            ],
            retriable=False,
        )

    def run_lint(self, workspace_path: str, options: dict) -> ValidationResult:
        return _run_gradle_task(workspace_path, "lint", options)

    def run_e2e(self, workspace_path: str, options: dict) -> ValidationResult:
        return self.run_integration_test(workspace_path, options)


def register_android_validation_provider() -> AndroidValidationProvider:
    """Create and register the Android validation provider globally."""
    provider = AndroidValidationProvider()
    register_validation_provider(provider)
    return provider
