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


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------

async def setup_workspace(state: dict) -> dict:
    """Clone repository and create a working branch.

    Uses runtime.run() to derive a deterministic branch name from the task
    description and Jira context.  Falls back to metadata values when no
    runtime is available.
    """
    runtime = state.get("_runtime")
    repo_url = state.get("repo_url", "")
    branch_name = state.get("branch_name", "")
    task_id = state.get("_task_id", "unknown")

    workspace_path = f"/tmp/constellation/{task_id}"
    repo_path = f"{workspace_path}/repo"

    # If branch_name already provided by Team Lead, skip LLM call
    if not branch_name and runtime:
        from agents.web_dev.prompts import SETUP_SYSTEM, SETUP_TEMPLATE
        jira_context = state.get("jira_context", {})
        prompt = SETUP_TEMPLATE.format(
            user_request=state.get("user_request", ""),
            repo_url=repo_url,
            jira_context=json.dumps(jira_context, ensure_ascii=False) if jira_context else "N/A",
        )
        result = runtime.run(prompt, system_prompt=SETUP_SYSTEM)
        data = _safe_json(result.get("raw_response", ""), fallback={})
        branch_name = data.get("branch_name", "feature/task")

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
    max_test_cycles = 3

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
    )

    return {
        "fix_attempted": True,
        "fix_summary": result.summary,
        "agentic_success": result.success,
    }


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
    desc_result = runtime.run(desc_prompt, system_prompt=PR_DESCRIPTION_SYSTEM)
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
    )

    pr_data = _safe_json(push_result.summary, fallback={})

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
    }

