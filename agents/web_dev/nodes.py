"""Web Dev Agent workflow nodes.

Each node receives the full workflow state dict and returns a partial dict
that is merged back into state by the WorkflowRunner.

Design pattern — "Graph outside, ReAct inside":
- Macro lifecycle (node order, branching, looping) is the graph's job.
- Open-ended implementation work is delegated to runtime.run_agentic().
- Bounded single-shot decisions (branch name, PR title) use runtime.run().
- Nodes degrade gracefully when no runtime is available (unit-test path).
"""
from __future__ import annotations

import json
import os
import re
from typing import Any


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_json(text: str, fallback: Any = None) -> Any:
    """Extract and parse the first JSON object/array from *text*.

    Returns *fallback* when *text* is None/empty or no valid JSON is found.
    """
    if not text:
        return fallback
    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return fallback


def _call_boundary_tool(state: dict, tool_name: str, args: dict) -> dict:
    """Call a boundary agent tool via the global ToolRegistry.

    Returns the parsed JSON payload or an error dict.
    """
    from framework.tools.registry import get_registry

    registry = get_registry()
    try:
        result_str = registry.execute_sync(tool_name, args)
        return json.loads(result_str) if result_str else {}
    except Exception as exc:
        print(f"[web-dev] Tool {tool_name} failed: {exc}")
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------

async def prepare_jira(state: dict) -> dict:
    """Update Jira before implementation starts.

    Actions:
    1. Discover Jira Agent through Registry (via boundary tools).
    2. Resolve the token user identity.
    3. List available transitions.
    4. Transition the ticket to "In Progress" when reachable.
    5. Set assignee to the token user by default.
    6. Add a pickup comment.

    Idempotency:
    - Skip the transition if the ticket is already in "In Progress".
    - Skip the assignee update if the assignee already matches the token user.
    """
    jira_context = state.get("jira_context", {})
    jira_key = (
        jira_context.get("key")
        or jira_context.get("ticket_key")
        or state.get("jira_key", "")
    )

    if not jira_key:
        return {"jira_prepared": False, "jira_prepare_skipped": "no_jira_key"}

    # Resolve original status for rollback
    original_status = ""
    original_assignee = ""
    if isinstance(jira_context, dict):
        fields = jira_context.get("fields", jira_context)
        original_status = (
            fields.get("status", {}).get("name", "")
            if isinstance(fields.get("status"), dict)
            else str(fields.get("status", ""))
        )
        assignee = fields.get("assignee") or {}
        original_assignee = (
            assignee.get("emailAddress", assignee.get("displayName", ""))
            if isinstance(assignee, dict)
            else str(assignee)
        )

    # Resolve token user
    token_user = ""
    token_user_result = _call_boundary_tool(state, "jira_get_token_user", {})
    if not token_user_result.get("error"):
        user_data = token_user_result.get("user", {})
        token_user = user_data.get("emailAddress", user_data.get("displayName", ""))

    # Transition to "In Progress" if not already
    if original_status.lower() not in ("in progress",):
        transitions_result = _call_boundary_tool(
            state, "jira_list_transitions", {"ticket_key": jira_key}
        )
        transitions = transitions_result.get("transitions", [])
        can_transition = any(
            t.get("name", "").lower() in ("in progress", "start progress")
            for t in transitions
            if isinstance(t, dict)
        )
        if can_transition:
            _call_boundary_tool(
                state, "jira_transition",
                {"ticket_key": jira_key, "transition_name": "In Progress"},
            )
        else:
            print(f"[web-dev] Cannot transition {jira_key} to In Progress; skipping")

    # Update assignee to token user
    if token_user and token_user != original_assignee:
        _call_boundary_tool(
            state, "jira_update",
            {"ticket_key": jira_key, "fields": {"assignee": {"emailAddress": token_user}}},
        )

    # Add pickup comment
    _call_boundary_tool(
        state, "jira_comment",
        {
            "ticket_key": jira_key,
            "comment": f"Development agent (web-dev) has picked up this ticket.",
        },
    )

    # Write jira-prepare-log.json
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        import time as _time
        agent_dir = os.path.join(workspace_path, "web-agent")
        os.makedirs(agent_dir, exist_ok=True)
        try:
            log_file = os.path.join(agent_dir, "jira-prepare-log.json")
            with open(log_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "web-dev",
                        "step": "prepare_jira",
                        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": {
                        "jira_key": jira_key,
                        "jira_original_status": original_status,
                        "jira_original_assignee": original_assignee,
                        "jira_token_user": token_user,
                        "jira_prepared": True,
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    return {
        "jira_prepared": True,
        "jira_original_status": original_status,
        "jira_original_assignee": original_assignee,
        "jira_token_user": token_user,
    }

async def setup_workspace(state: dict) -> dict:
    """Create a working branch in the cloned repository.

    The repo is already cloned by Team Lead (via SCM Agent) into repo_path.
    This node verifies the repo exists and creates or checks out a local
    development branch.

    Uses runtime.run() to derive a deterministic branch name from the task
    description and Jira context.  Falls back to metadata values when no
    runtime is available.
    """
    runtime = state.get("_runtime")
    repo_url = state.get("repo_url", "")
    repo_path = state.get("repo_path", "")
    workspace_path = state.get("workspace_path", "")
    branch_name = state.get("branch_name", "")
    task_id = state.get("_task_id", "unknown")

    # Use workspace_path from Team Lead; only fall back to temp if missing
    if not workspace_path:
        workspace_path = f"/tmp/constellation/{task_id}"
    if not repo_path:
        repo_path = os.path.join(workspace_path, "repo")

    # If branch_name already provided by Team Lead, skip LLM call
    if not branch_name and runtime:
        from agents.web_dev.prompts import SETUP_SYSTEM, SETUP_TEMPLATE
        jira_context = state.get("jira_context", {})
        prompt = SETUP_TEMPLATE.format(
            user_request=state.get("user_request", ""),
            repo_url=repo_url,
            jira_context=json.dumps(jira_context, ensure_ascii=False) if jira_context else "N/A",
        )
        result = runtime.run(prompt, system_prompt=SETUP_SYSTEM,
                             plugin_manager=state.get("_plugin_manager"))
        data = _safe_json(result.get("raw_response", ""), fallback={})
        branch_name = data.get("branch_name", "feature/task")

    # Write git setup log
    if workspace_path:
        agent_dir = os.path.join(workspace_path, "web-agent")
        os.makedirs(agent_dir, exist_ok=True)
        try:
            import time as _time
            log_file = os.path.join(agent_dir, "git-setup-log.json")
            with open(log_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "web-dev",
                        "step": "setup_workspace",
                        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": {
                        "repo_url": repo_url,
                        "repo_path": repo_path,
                        "branch_name": branch_name or "feature/task",
                        "repo_exists": os.path.isdir(repo_path),
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    return {
        "workspace_path": workspace_path,
        "repo_path": repo_path,
        "branch_name": branch_name or "feature/task",
        "branch_created": bool(branch_name),
    }


async def analyze_task(state: dict) -> dict:
    """Understand requirements and produce an implementation plan.

    Reuses the analysis already provided by Team Lead (via state["analysis"]).
    Falls back to a simple echo of the user request when nothing is provided.
    """
    # Team Lead already performed deep analysis — reuse it
    plan = state.get("analysis") or state.get("user_request", "")
    return {
        "implementation_plan": plan,
    }


async def implement_changes(state: dict) -> dict:
    """Write code based on the implementation plan.

    Uses runtime.run_agentic() for open-ended code generation and file editing.
    The agentic loop has access to read_file, write_file, edit_file, search_code,
    and run_command tools registered in the global ToolRegistry.
    """
    runtime = state.get("_runtime")

    if not runtime:
        # Unit-test / no-runtime path
        return {
            "changes_made": [],
            "implementation_summary": "Changes implemented (no runtime — test mode).",
            "agentic_success": True,
        }

    from agents.web_dev.prompts import IMPLEMENT_SYSTEM, IMPLEMENT_TEMPLATE

    jira_ctx = state.get("jira_context", {})
    prompt = IMPLEMENT_TEMPLATE.format(
        user_request=state.get("user_request", ""),
        repo_path=state.get("repo_path", ""),
        branch_name=state.get("branch_name", "feature/task"),
        implementation_plan=state.get("implementation_plan", ""),
        jira_context=json.dumps(jira_ctx, ensure_ascii=False) if jira_ctx else "N/A",
        design_context=str(state.get("design_context", "N/A")),
        skill_context=state.get("skill_context", ""),
        memory_context=state.get("memory_context", ""),
    )

    allowed_tools = state.get("_allowed_tools")  # enforced by PermissionEngine upstream
    result = runtime.run_agentic(
        task=prompt,
        system_prompt=IMPLEMENT_SYSTEM,
        cwd=state.get("repo_path") or None,
        tools=allowed_tools,
        max_turns=20,
        timeout=600,
        plugin_manager=state.get("_plugin_manager"),
    )

    # Extract changed file names from tool_calls log
    changes_made = sorted({
        tc["arguments"] if isinstance(tc["arguments"], str)
        else json.dumps(tc.get("arguments", {}))
        for tc in result.tool_calls
        if tc.get("tool") in {"write_file", "edit_file", "write_local_file", "edit_local_file"}
    })

    return {
        "changes_made": changes_made,
        "implementation_summary": result.summary,
        "agentic_success": result.success,
    }


async def run_tests(state: dict) -> dict:
    """Run project tests and evaluate results.

    Sets state["route"] to "pass" or "fail" for conditional routing.
    Uses runtime.run_agentic() to execute test commands and parse results.
    """
    runtime = state.get("_runtime")
    test_cycles = state.get("test_cycles", 0) + 1
    max_test_cycles = 5

    if not runtime:
        # Unit-test path: always pass
        return {
            "test_results": {"passed": 1, "failed": 0, "output": ""},
            "test_cycles": test_cycles,
            "test_status": "pass",
            "route": "pass",
        }

    repo_path = state.get("repo_path", "")
    result = runtime.run_agentic(
        task=(
            "Run the project's test suite and report results.\n"
            "1. Detect the test runner (pytest, jest, mvn test, gradle test, etc.).\n"
            "2. Run tests with verbose output.\n"
            "3. Return a JSON summary: "
            '{"passed": N, "failed": N, "errors": [...], "output": "...last 50 lines..."}'
        ),
        cwd=repo_path or None,
        max_turns=5,
        timeout=120,
        plugin_manager=state.get("_plugin_manager"),
    )

    data = _safe_json(result.summary, fallback={})
    failed = data.get("failed", 0)
    test_passed = int(failed) == 0 and result.success

    if test_passed:
        return {
            "test_results": data,
            "test_output": data.get("output", result.summary),
            "test_cycles": test_cycles,
            "test_status": "pass",
            "route": "pass",
        }

    if test_cycles >= max_test_cycles:
        return {
            "test_results": data,
            "test_output": data.get("output", result.summary),
            "test_cycles": test_cycles,
            "test_status": "fail",
            "route": "pass",  # proceed to PR despite failures; Team Lead will review
        }

    return {
        "test_results": data,
        "test_output": data.get("output", result.summary),
        "test_cycles": test_cycles,
        "test_status": "fail",
        "route": "fail",
    }


async def fix_tests(state: dict) -> dict:
    """Fix failing tests based on test output.

    Uses runtime.run_agentic() to analyse failures and apply minimal fixes.
    """
    runtime = state.get("_runtime")

    if not runtime:
        return {"fix_attempted": True}

    from agents.web_dev.prompts import FIX_SYSTEM, FIX_TEMPLATE

    changed_files = state.get("changes_made", [])
    prompt = FIX_TEMPLATE.format(
        test_output=state.get("test_output", "No test output available."),
        repo_path=state.get("repo_path", ""),
        changed_files="\n".join(changed_files) if changed_files else "unknown",
    )

    result = runtime.run_agentic(
        task=prompt,
        system_prompt=FIX_SYSTEM,
        cwd=state.get("repo_path") or None,
        max_turns=15,
        timeout=300,
        plugin_manager=state.get("_plugin_manager"),
    )

    return {
        "fix_attempted": True,
        "fix_summary": result.summary,
        "agentic_success": result.success,
    }


async def self_assess(state: dict) -> dict:
    """Run requirement-aware and design-aware self assessment.

    Evaluation dimensions:
    1. Acceptance criteria coverage.
    2. Component-by-component UI design alignment for UI tasks.
    3. Build status.
    4. Test status and newly added test coverage.
    5. Code quality and obvious risk review.

    Pass threshold: score >= 0.9.
    """
    runtime = state.get("_runtime")
    assess_cycles = state.get("assess_cycles", 0) + 1
    max_assess_cycles = 3

    if not runtime:
        return {
            "self_assessment": {
                "score": 0.95,
                "verdict": "pass",
                "gaps": [],
                "component_checks": [],
                "criteria_checks": [],
            },
            "assess_cycles": assess_cycles,
            "route": "pass",
        }

    from agents.web_dev.prompts import SELF_ASSESS_SYSTEM, SELF_ASSESS_TEMPLATE

    jira_ctx = state.get("jira_context", {})
    design_ctx = state.get("design_context") or {}
    acceptance_criteria = []
    if isinstance(jira_ctx, dict):
        fields = jira_ctx.get("fields", jira_ctx)
        acceptance_criteria = fields.get("acceptanceCriteria", [])
        if not acceptance_criteria and fields.get("description"):
            acceptance_criteria = [fields["description"]]

    prompt = SELF_ASSESS_TEMPLATE.format(
        acceptance_criteria=json.dumps(acceptance_criteria, ensure_ascii=False),
        design_context=json.dumps(design_ctx, ensure_ascii=False) if design_ctx else "N/A (not a UI task)",
        implementation_summary=state.get("implementation_summary", ""),
        test_results=json.dumps(state.get("test_results", {}), ensure_ascii=False),
        changed_files="\n".join(state.get("changes_made", [])) or "unknown",
    )

    result = runtime.run(
        prompt, system_prompt=SELF_ASSESS_SYSTEM,
        max_tokens=2048,
        plugin_manager=state.get("_plugin_manager"),
    )

    data = _safe_json(result.get("raw_response", ""), fallback={})
    score = float(data.get("score", 0))
    verdict = data.get("verdict", "fail")
    gaps = data.get("gaps", [])

    # Write self-assessment.json to workspace
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        import time as _time
        agent_dir = os.path.join(workspace_path, "web-agent")
        os.makedirs(agent_dir, exist_ok=True)
        try:
            sa_file = os.path.join(agent_dir, "self-assessment.json")
            with open(sa_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "web-dev",
                        "step": "self_assess",
                        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                        "version": assess_cycles,
                    },
                    "data": data,
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    if score >= 0.9 and verdict != "fail":
        return {
            "self_assessment": data,
            "assess_cycles": assess_cycles,
            "route": "pass",
        }

    if assess_cycles >= max_assess_cycles:
        # Exhausted assess cycles — proceed with last result
        return {
            "self_assessment": data,
            "assess_cycles": assess_cycles,
            "route": "pass",
        }

    return {
        "self_assessment": data,
        "assess_cycles": assess_cycles,
        "route": "fail",
    }


async def fix_gaps(state: dict) -> dict:
    """Fix self-assessment gaps before re-running tests and self-assessment."""
    runtime = state.get("_runtime")

    if not runtime:
        return {"fix_gaps_attempted": True}

    from agents.web_dev.prompts import FIX_GAPS_SYSTEM, FIX_GAPS_TEMPLATE

    assessment = state.get("self_assessment", {})
    gaps = assessment.get("gaps", [])
    changed_files = state.get("changes_made", [])

    prompt = FIX_GAPS_TEMPLATE.format(
        gaps="\n".join(f"- {g}" for g in gaps) if gaps else "No specific gaps listed.",
        repo_path=state.get("repo_path", ""),
        changed_files="\n".join(changed_files) if changed_files else "unknown",
    )

    result = runtime.run_agentic(
        task=prompt,
        system_prompt=FIX_GAPS_SYSTEM,
        cwd=state.get("repo_path") or None,
        max_turns=15,
        timeout=300,
        plugin_manager=state.get("_plugin_manager"),
    )

    return {
        "fix_gaps_attempted": True,
        "fix_gaps_summary": result.summary,
        "agentic_success": result.success,
    }


async def capture_screenshot(state: dict) -> dict:
    """Capture implementation screenshots for human review only.

    No automatic visual diff is required in the current phase.
    Skips if screenshot_required is False in definition_of_done.
    """
    definition_of_done = state.get("definition_of_done", {})
    screenshot_required = definition_of_done.get("screenshot_required", True)

    if not screenshot_required:
        return {"screenshot_captured": False, "screenshots": []}

    runtime = state.get("_runtime")
    if not runtime:
        return {"screenshot_captured": False, "screenshots": []}

    repo_path = state.get("repo_path", "")
    workspace_path = state.get("workspace_path", "")
    screenshot_dir = os.path.join(workspace_path, "web-agent", "screenshots")

    try:
        os.makedirs(screenshot_dir, exist_ok=True)
    except OSError:
        pass

    # Best-effort screenshot via agentic runtime
    result = runtime.run_agentic(
        task=(
            "Take a screenshot of the implemented UI.\n"
            f"1. Start the dev server in {repo_path} (npm run dev or similar).\n"
            "2. Wait for it to be ready.\n"
            "3. Use a headless browser to capture a screenshot.\n"
            f"4. Save screenshots to {screenshot_dir}/\n"
            "Return JSON: {\"screenshots\": [\"path1.png\", ...], \"captured\": true}\n"
            "If screenshot fails, return {\"screenshots\": [], \"captured\": false}"
        ),
        cwd=repo_path or None,
        max_turns=10,
        timeout=120,
        plugin_manager=state.get("_plugin_manager"),
    )

    data = _safe_json(result.summary, fallback={})
    screenshots = data.get("screenshots", [])
    captured = data.get("captured", bool(screenshots))

    return {
        "screenshot_captured": captured,
        "screenshots": screenshots,
    }


async def update_jira(state: dict) -> dict:
    """Update Jira after development is complete.

    Actions:
    1. Add a completion comment with PR URL, test results, self-assessment score.
    2. Transition ticket to "In Review".

    Idempotency:
    - Check if a comment with the PR URL already exists before adding.
    """
    jira_context = state.get("jira_context", {})
    jira_key = (
        jira_context.get("key")
        or jira_context.get("ticket_key")
        or state.get("jira_key", "")
    )

    if not jira_key:
        return {"jira_updated": False, "jira_update_skipped": "no_jira_key"}

    pr_url = state.get("pr_url", "N/A")
    branch = state.get("branch_name", "N/A")
    test_results = state.get("test_results", {})
    assessment = state.get("self_assessment", {})
    changes = state.get("changes_made", [])

    # Build completion comment
    comment_text = (
        f"Development completed by web-dev agent.\n"
        f"PR: {pr_url}\n"
        f"Branch: {branch}\n"
        f"Test results: {test_results.get('passed', 0)} passed, "
        f"{test_results.get('failed', 0)} failed\n"
        f"Self-assessment score: {assessment.get('score', 'N/A')}\n"
        f"Changes: {len(changes)} files modified"
    )

    # Idempotency: check if comment with PR URL already exists
    existing = _call_boundary_tool(
        state, "jira_list_comments", {"ticket_key": jira_key}
    )
    already_commented = False
    for c in existing.get("comments", []):
        body = ""
        if isinstance(c, dict):
            body = c.get("body", "")
            if isinstance(body, dict):
                # ADF body — check rendered text
                body = json.dumps(body)
        if pr_url and pr_url != "N/A" and pr_url in str(body):
            already_commented = True
            break

    if not already_commented:
        _call_boundary_tool(
            state, "jira_comment",
            {"ticket_key": jira_key, "comment": comment_text},
        )

    # Transition to "In Review"
    transitions_result = _call_boundary_tool(
        state, "jira_list_transitions", {"ticket_key": jira_key}
    )
    transitions = transitions_result.get("transitions", [])
    can_review = any(
        t.get("name", "").lower() in ("in review", "review", "code review")
        for t in transitions
        if isinstance(t, dict)
    )
    if can_review:
        _call_boundary_tool(
            state, "jira_transition",
            {"ticket_key": jira_key, "transition_name": "In Review"},
        )

    # Write jira-update-log.json to workspace
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        import time as _time
        agent_dir = os.path.join(workspace_path, "web-agent")
        os.makedirs(agent_dir, exist_ok=True)
        try:
            log_file = os.path.join(agent_dir, "jira-update-log.json")
            with open(log_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "web-dev",
                        "step": "update_jira",
                        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": {
                        "jira_key": jira_key,
                        "pr_url": pr_url,
                        "comment_added": not already_commented,
                        "transition_attempted": can_review,
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    return {"jira_updated": True}


async def create_pr(state: dict) -> dict:
    """Generate a PR description and create the pull request via SCM tools.

    Uses runtime.run() for the PR description and runtime.run_agentic() to
    push the branch and open the PR through available SCM tools.
    """
    runtime = state.get("_runtime")

    if not runtime:
        return {
            "pr_url": "",
            "pr_number": 0,
            "pr_title": "Implement changes",
            "commit_hash": "",
        }

    from agents.web_dev.prompts import PR_DESCRIPTION_SYSTEM, PR_DESCRIPTION_TEMPLATE

    jira_ctx = state.get("jira_context", {})
    jira_key = (
        jira_ctx.get("key") or jira_ctx.get("ticket_key") or ""
        if isinstance(jira_ctx, dict) else ""
    )

    # Step 1: Generate PR description (single-shot LLM)
    desc_prompt = PR_DESCRIPTION_TEMPLATE.format(
        user_request=state.get("user_request", ""),
        branch_name=state.get("branch_name", "feature/task"),
        jira_key=jira_key or "N/A",
        implementation_summary=state.get("implementation_summary", ""),
        changed_files=", ".join(state.get("changes_made", [])) or "various files",
    )
    desc_result = runtime.run(desc_prompt, system_prompt=PR_DESCRIPTION_SYSTEM,
                              plugin_manager=state.get("_plugin_manager"))
    pr_meta = _safe_json(desc_result.get("raw_response", ""), fallback={})
    pr_title = pr_meta.get("title", "Implement task changes")
    pr_description = pr_meta.get("description", state.get("implementation_summary", ""))

    # Step 2: Push branch and create PR (agentic — uses SCM tools)
    repo_path = state.get("repo_path", "")
    repo_url = state.get("repo_url", "")
    branch_name = state.get("branch_name", "feature/task")

    push_result = runtime.run_agentic(
        task=(
            f"Push branch '{branch_name}' to remote and create a pull request.\n"
            f"Repository: {repo_url}\n"
            f"Local path: {repo_path}\n"
            f"PR title: {pr_title}\n"
            f"PR description:\n{pr_description}\n\n"
            "Return a JSON object with keys: pr_url, pr_number, commit_hash."
        ),
        cwd=repo_path or None,
        max_turns=10,
        timeout=120,
        plugin_manager=state.get("_plugin_manager"),
    )

    pr_data = _safe_json(push_result.summary, fallback={})

    # Write pr-evidence.json to workspace
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        import time as _time
        agent_dir = os.path.join(workspace_path, "web-agent")
        os.makedirs(agent_dir, exist_ok=True)
        try:
            evidence_file = os.path.join(agent_dir, "pr-evidence.json")
            with open(evidence_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "web-dev",
                        "step": "create_pr",
                        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": {
                        "pr_url": pr_data.get("pr_url", ""),
                        "pr_number": pr_data.get("pr_number", 0),
                        "branch": branch_name,
                        "title": pr_title,
                        "commit_hash": pr_data.get("commit_hash", ""),
                        "files_changed": len(state.get("changes_made", [])),
                        "test_status": state.get("test_status", "unknown"),
                        "self_assessment_score": state.get("self_assessment", {}).get("score", "N/A"),
                        "screenshot_included": state.get("screenshot_captured", False),
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    return {
        "pr_url": pr_data.get("pr_url", ""),
        "pr_number": pr_data.get("pr_number", 0),
        "pr_title": pr_title,
        "pr_description": pr_description,
        "commit_hash": pr_data.get("commit_hash", ""),
    }


async def report_result(state: dict) -> dict:
    """Return final result summary."""
    pr_url = state.get("pr_url", "N/A")
    branch_name = state.get("branch_name", "N/A")
    changes = state.get("changes_made", [])
    pr_title = state.get("pr_title", "")
    test_status = state.get("test_status", "unknown")

    summary_parts = [
        f"Implementation complete.",
        f"{len(changes)} file(s) changed.",
        f"Test status: {test_status}.",
    ]
    if pr_title:
        summary_parts.append(f"PR: {pr_title}.")
    if pr_url and pr_url != "N/A":
        summary_parts.append(f"URL: {pr_url}")

    return {
        "success": True,
        "state": "TASK_STATE_COMPLETED",
        "summary": " ".join(summary_parts),
        "implementation_summary": " ".join(summary_parts),
        "pr_url": pr_url,
        "branch_name": branch_name,
    }

