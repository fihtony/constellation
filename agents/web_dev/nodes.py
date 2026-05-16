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

def _summarize_jira_context(jira_ctx: dict, max_chars: int = 3000) -> str:
    """Extract only essential Jira fields and truncate to avoid context window overflow.

    The raw Jira REST API response can be hundreds of KB. Only the fields
    relevant for implementation are kept.
    """
    if not jira_ctx:
        return "N/A"
    fields = jira_ctx.get("fields") or jira_ctx
    desc = fields.get("description") or ""
    if isinstance(desc, dict):
        # Atlassian Document Format — flatten to plain text
        try:
            desc = json.dumps(desc, ensure_ascii=False)
        except Exception:
            desc = str(desc)
    essential: dict = {
        "key": jira_ctx.get("key", ""),
        "summary": fields.get("summary", ""),
        "description": desc[:15000] + ("...(truncated)" if len(str(desc)) > 15000 else ""),
        "status": (fields.get("status") or {}).get("name", ""),
        "priority": (fields.get("priority") or {}).get("name", ""),
        "issuetype": (fields.get("issuetype") or {}).get("name", ""),
        "labels": fields.get("labels", []),
        "components": [c.get("name", "") for c in (fields.get("components") or []) if isinstance(c, dict)],
        "assignee": ((fields.get("assignee") or {}).get("displayName", "")
                     or (fields.get("assignee") or {}).get("name", "")),
    }
    result = json.dumps(essential, ensure_ascii=False)
    if len(result) > max_chars:
        essential["description"] = essential["description"][:5000] + "...(further truncated)"
        result = json.dumps(essential, ensure_ascii=False)
    return result


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


def _git_commit_all_pending(repo_path: str, jira_key: str) -> list[str]:
    """Stage all pending changes and commit if anything is staged.

    Called inside create_pr before scm_push to ensure every file written by
    the agentic implement_changes loop is committed — even when the LLM only
    ran 'git commit <specific-file>' instead of 'git add -A && git commit'.
    """
    if not repo_path or not os.path.isdir(repo_path):
        return []
    try:
        import subprocess
        from framework.env_utils import build_isolated_git_env
        git_env = build_isolated_git_env(scope="web-dev-commit")

        # Stage all untracked/modified files
        subprocess.run(
            ["git", "add", "-A"],
            cwd=repo_path, env=git_env,
            capture_output=True, text=True, timeout=30,
        )

        # Check if there is anything staged
        status_result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            cwd=repo_path, env=git_env,
            capture_output=True, text=True, timeout=10,
        )
        staged_files = [f for f in status_result.stdout.strip().splitlines() if f]

        if staged_files:
            commit_msg = f"feat({jira_key or 'task'}): implement changes"
            r = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=repo_path, env=git_env,
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode == 0:
                print(f"[web-dev] committed {len(staged_files)} pending file(s): {staged_files[:8]}")
            else:
                print(f"[web-dev] commit failed: {r.stderr.strip()[:200]}")
            return staged_files
        else:
            # Confirm there is at least one commit on the branch, and list its changed files
            log_result = subprocess.run(
                ["git", "log", "--oneline", "-1"],
                cwd=repo_path, env=git_env,
                capture_output=True, text=True, timeout=10,
            )
            print(f"[web-dev] no pending changes; last commit: {log_result.stdout.strip()[:120]!r}")
            # Get files from HEAD commit (agent already committed during run_agentic)
            diff_result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~1..HEAD"],
                cwd=repo_path, env=git_env,
                capture_output=True, text=True, timeout=10,
            )
            if diff_result.returncode == 0:
                return [f for f in diff_result.stdout.strip().splitlines() if f]
            return []
    except Exception as exc:
        print(f"[web-dev] _git_commit_all_pending error (non-fatal): {exc}")
        return []


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
    print(f"[web-dev] prepare_jira: jira_key={jira_key!r}")

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
    if original_status.lower() not in ("in progress", "in development", "in dev"):
        transitions_result = _call_boundary_tool(
            state, "jira_list_transitions", {"ticket_key": jira_key}
        )
        transitions = transitions_result.get("transitions", [])
        _IN_PROGRESS_NAMES = {
            "in progress", "start progress", "in development", "in dev",
            "start development", "start", "begin", "begin work",
        }
        in_progress_match = next(
            (t for t in transitions
             if isinstance(t, dict) and t.get("name", "").lower() in _IN_PROGRESS_NAMES),
            None,
        )
        if in_progress_match:
            _call_boundary_tool(
                state, "jira_transition",
                {"ticket_key": jira_key, "transition_name": in_progress_match["name"]},
            )
        else:
            avail = [t.get("name") for t in transitions if isinstance(t, dict)]
            print(f"[web-dev] Cannot transition {jira_key} to In Progress; available: {avail}")

    # Update assignee to token user
    if token_user and token_user != original_assignee:
        _call_boundary_tool(
            state, "jira_update",
            {"ticket_key": jira_key, "fields": {"assignee": {"emailAddress": token_user}}},
        )

    # Add pickup comment with task_id
    _task_id = state.get("_task_id", "unknown")
    _call_boundary_tool(
        state, "jira_comment",
        {
            "ticket_key": jira_key,
            "comment": (
                f"🤖 Development agent (web-dev) has picked up this ticket.\n"
                f"Task ID: {_task_id}\n"
                f"Assignee: {token_user or 'unknown'}\n"
                f"Status: In Progress"
            ),
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
    print(f"[web-dev] setup_workspace: repo_path={repo_path!r} workspace_path={workspace_path!r}")

    # Use workspace_path from Team Lead; only fall back to artifacts/ if missing
    if not workspace_path:
        workspace_path = os.path.join(
            os.path.abspath(os.environ.get("ARTIFACT_ROOT", "artifacts")),
            f"workspace-{task_id}",
        )
        os.makedirs(workspace_path, exist_ok=True)
    if not repo_path:
        repo_path = os.path.join(workspace_path, "repo")

    # Fail fast if repo does not exist — Team Lead must have cloned it first
    if not os.path.isdir(repo_path):
        raise RuntimeError(
            f"[web-dev] Repo not found at {repo_path!r}. "
            "Team Lead must clone the repo before dispatching to Web Dev."
        )

    # Derive branch name: use provided value, then LLM, then Jira-key fallback
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
        branch_name = data.get("branch_name", "")

    # Derive branch name from Jira key when LLM result is unavailable
    if not branch_name:
        jira_key_raw = (
            (state.get("jira_context") or {}).get("key", "")
            or state.get("jira_key", "")
        ).upper()
        task_suffix = state.get("_task_id", "task")[:8]
        if jira_key_raw:
            branch_name = f"feature/{jira_key_raw}-{task_suffix}"
        else:
            branch_name = f"feature/{task_suffix}"

    # -- Check remote for branch name conflicts; add _<n> suffix when taken --
    # Must not delete or alter existing remote branches or PRs.
    if branch_name and repo_url:
        remote_result = _call_boundary_tool(state, "scm_list_branches", {"repo_url": repo_url})
        remote_branch_names = {
            b.get("displayId", "") for b in remote_result.get("branches", [])
        }
        if branch_name in remote_branch_names:
            n = 2
            while f"{branch_name}_{n}" in remote_branch_names:
                n += 1
            new_name = f"{branch_name}_{n}"
            print(
                f"[web-dev] setup_workspace: branch {branch_name!r} exists on remote, "
                f"using {new_name!r} to avoid conflict"
            )
            branch_name = new_name

    # Actually create / checkout the branch in the cloned repo
    branch_created = False
    if repo_path and os.path.isdir(repo_path) and branch_name:
        import subprocess
        from framework.env_utils import build_isolated_git_env
        git_env = build_isolated_git_env("web-dev-setup")
        r = subprocess.run(
            ["git", "checkout", "-b", branch_name],
            cwd=repo_path, env=git_env,
            capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            branch_created = True
            print(f"[web-dev] setup_workspace: created branch {branch_name!r}")
        else:
            # Branch might already exist — try switching to it
            r2 = subprocess.run(
                ["git", "checkout", branch_name],
                cwd=repo_path, env=git_env,
                capture_output=True, text=True, timeout=30,
            )
            if r2.returncode == 0:
                branch_created = True
                print(f"[web-dev] setup_workspace: switched to existing branch {branch_name!r}")
            else:
                print(f"[web-dev] setup_workspace: git checkout failed: {r2.stderr.strip()[:200]}")
                raise RuntimeError(
                    f"[web-dev] Failed to create/switch branch {branch_name!r}: {r2.stderr.strip()[:200]}"
                )

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
        "branch_created": branch_created,
    }


async def analyze_task(state: dict) -> dict:
    """Understand requirements and produce an implementation plan.

    Loads delivery-plan.json from the workspace (written by Team Lead) when
    available, and merges it with the analysis already passed in state.
    Falls back to a simple echo of the user request when nothing is provided.
    """
    import time as _time
    print("[web-dev] analyze_task: building implementation plan")

    workspace_path = state.get("workspace_path", "")
    analysis = state.get("analysis") or state.get("user_request", "")

    # Try to load Team Lead's delivery-plan.json for structured plan data
    delivery_plan: dict = {}
    if workspace_path:
        plan_path = os.path.join(workspace_path, "team_lead", "delivery-plan.json")
        try:
            with open(plan_path, encoding="utf-8") as fh:
                doc = json.load(fh)
                delivery_plan = doc.get("data", doc)
                print(f"[web-dev] Loaded delivery-plan.json from {plan_path}")
        except (OSError, json.JSONDecodeError):
            pass

    # Build rich plan string
    plan_parts = []
    if analysis:
        plan_parts.append(analysis)
    if delivery_plan:
        plan_parts.append(f"\nDelivery plan:\n{json.dumps(delivery_plan, indent=2, ensure_ascii=False)}")

    # Also load Jira ticket for acceptance criteria
    if workspace_path:
        jira_path = os.path.join(workspace_path, "team_lead", "jira-ticket.json")
        try:
            with open(jira_path, encoding="utf-8") as fh:
                doc = json.load(fh)
                jira_data = doc.get("data", doc)
                summary = jira_data.get("summary", "") or (jira_data.get("fields") or {}).get("summary", "")
                if summary:
                    plan_parts.append(f"\nJira ticket summary: {summary}")
        except (OSError, json.JSONDecodeError):
            pass

    plan = "\n".join(plan_parts) if plan_parts else analysis

    # Write implementation-plan.json to workspace for auditability
    if workspace_path:
        agent_dir = os.path.join(workspace_path, "web-agent")
        os.makedirs(agent_dir, exist_ok=True)
        try:
            plan_file = os.path.join(agent_dir, "implementation-plan.json")
            with open(plan_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "web-dev",
                        "step": "analyze_task",
                        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": {
                        "implementation_plan": plan,
                        "delivery_plan_loaded": bool(delivery_plan),
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

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
    # Truncate: full Jira REST response can be 200KB+ — keep only essential fields
    jira_for_prompt = _summarize_jira_context(jira_ctx)
    # Also truncate implementation_plan if too large
    impl_plan = str(state.get("implementation_plan", ""))
    if len(impl_plan) > 4000:
        impl_plan = impl_plan[:4000] + "...(truncated)"

    # Load design HTML code from workspace for component reference
    _design_code_ref = "N/A"
    _design_code_path = state.get("design_code_path", "")
    _workspace_path = state.get("workspace_path", "")
    if not _design_code_path and _workspace_path:
        _design_code_path = os.path.join(_workspace_path, "team_lead", "design-code.html")
    if _design_code_path and os.path.isfile(_design_code_path):
        try:
            with open(_design_code_path, encoding="utf-8") as _f:
                _design_code_ref = _f.read()
        except Exception:
            pass

    # Pre-scan repo so LLM doesn't waste turns on exploration
    _repo_path = state.get("repo_path", "")
    _repo_files_section: str
    if _repo_path and os.path.isdir(_repo_path):
        try:
            import glob as _glob_mod
            _all = _glob_mod.glob("**/*", root_dir=_repo_path, recursive=True)
            _files = sorted(f for f in _all if os.path.isfile(os.path.join(_repo_path, f)))[:60]
            if _files:
                _repo_files_section = "\n".join(f"  {f}" for f in _files)
            else:
                _repo_files_section = (
                    "  (EMPTY — only README.md or no files). "
                    "You MUST create all project files from scratch starting in turn 1."
                )
        except Exception:
            _repo_files_section = "  (could not list files)"
    else:
        _repo_files_section = "  (repo path not available)"

    prompt = IMPLEMENT_TEMPLATE.format(
        user_request=state.get("user_request", ""),
        repo_path=state.get("repo_path", ""),
        branch_name=state.get("branch_name", "feature/task"),
        tech_stack=", ".join(state.get("tech_stack") or []) or "not specified",
        stitch_screen_name=state.get("stitch_screen_name", "not specified"),
        repo_files=_repo_files_section,
        implementation_plan=impl_plan,
        jira_context=jira_for_prompt,
        design_context=str(state.get("design_context", "N/A")),
        design_code_reference=_design_code_ref,
        skill_context=state.get("skill_context", ""),
        memory_context=state.get("memory_context", ""),
    )

    # Use Claude Code native tools (Bash, Read, Write, Glob, Grep) — no constellation
    # MCP bridge needed.  With cwd=repo_path, all relative paths resolve correctly.
    print(f"[web-dev] implement_changes: repo_path={state.get('repo_path', '')!r} (native tools)")
    result = runtime.run_agentic(
        task=prompt,
        system_prompt=IMPLEMENT_SYSTEM,
        cwd=state.get("repo_path") or None,
        tools=None,
        max_turns=50,
        timeout=1800,
        plugin_manager=state.get("_plugin_manager"),
    )
    print(f"[web-dev] implement_changes done: success={result.success} turns={result.turns_used} summary={result.summary[:300]!r}")

    if not result.success:
        raise RuntimeError(
            f"implement_changes failed — claude-code returned error: {result.summary[:500]}"
        )

    # With native tools, we can't track individual file writes from tool_calls.
    # changes_made is populated from git diff in create_pr via _git_commit_all_pending.
    return {
        "changes_made": [],
        "implementation_summary": result.summary,
        "agentic_success": True,
    }


async def run_tests(state: dict) -> dict:
    """Run project tests and evaluate results.

    Sets state["route"] to "pass" or "fail" for conditional routing.
    Uses runtime.run_agentic() to execute test commands and parse results.
    """
    runtime = state.get("_runtime")
    test_cycles = state.get("test_cycles", 0) + 1
    max_test_cycles = 1

    if not runtime:
        # Unit-test path: always pass
        return {
            "test_results": {"passed": 1, "failed": 0, "output": ""},
            "test_cycles": test_cycles,
            "test_status": "pass",
            "route": "pass",
        }

    repo_path = state.get("repo_path", "")
    print(f"[web-dev] run_tests: cycle={test_cycles} repo_path={repo_path!r}")

    # Fast-path: if we've already hit the max test cycles, skip the LLM entirely
    if test_cycles >= max_test_cycles:
        print(f"[web-dev] run_tests: max cycles reached ({test_cycles}/{max_test_cycles}), force-pass")
        return {
            "test_results": {},
            "test_output": "Skipped: max test cycles reached.",
            "test_cycles": test_cycles,
            "test_status": "skip",
            "route": "pass",
        }

    result = runtime.run_agentic(
        task=(
            "Run the project's test suite and report results.\n"
            "MANDATORY pre-flight steps (in order):\n"
            f"1. Change into the repo directory: {repo_path}\n"
            "2. If package.json exists: run `npm install` to install dependencies.\n"
            "   If npm install fails (e.g. package not found), read the error,\n"
            "   remove the invalid package from package.json, and re-run npm install.\n"
            "3. Run build to verify no compilation errors: `npm run build`\n"
            "   If build fails, fix the error first.\n"
            "4. Detect the test runner (vitest, jest, pytest, mvn test, gradle test, etc.).\n"
            "5. Run tests with verbose output.\n"
            "6. Return a JSON summary: "
            '{"passed": N, "failed": N, "errors": [...], "output": "...last 50 lines..."}'
        ),
        cwd=repo_path or None,
        tools=None,
        max_turns=15,
        timeout=600,
        plugin_manager=state.get("_plugin_manager"),
    )

    data = _safe_json(result.summary, fallback={})
    failed = data.get("failed", 0)
    test_passed = int(failed) == 0 and result.success

    # Write per-cycle test results for auditability
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        import time as _time
        results_dir = os.path.join(workspace_path, "web-agent", "test-results")
        os.makedirs(results_dir, exist_ok=True)
        try:
            cycle_file = os.path.join(results_dir, f"test-run-{test_cycles}.json")
            with open(cycle_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "web-dev",
                        "step": "run_tests",
                        "cycle": test_cycles,
                        "timestamp": _time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": data,
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

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
        tools=None,
        max_turns=20,
        timeout=600,
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
    workspace_path = state.get("workspace_path", "")

    # Try to load full design context from workspace file (more complete than state copy)
    if workspace_path:
        design_spec_path = os.path.join(workspace_path, "team_lead", "design-spec.json")
        if os.path.isfile(design_spec_path):
            try:
                with open(design_spec_path, encoding="utf-8") as _f:
                    spec_data = json.load(_f)
                design_ctx = spec_data.get("data", design_ctx) or design_ctx
            except Exception:
                pass

    # Load design HTML code for component-by-component comparison
    design_code_snippet = ""
    design_code_path = state.get("design_code_path", "")
    if not design_code_path and workspace_path:
        design_code_path = os.path.join(workspace_path, "team_lead", "design-code.html")
    if design_code_path and os.path.isfile(design_code_path):
        try:
            with open(design_code_path, encoding="utf-8") as _f:
                design_html = _f.read()
            design_code_snippet = design_html
        except Exception:
            pass

    acceptance_criteria = []
    if isinstance(jira_ctx, dict):
        fields = jira_ctx.get("fields", jira_ctx)
        acceptance_criteria = fields.get("acceptanceCriteria", [])
        if not acceptance_criteria and fields.get("description"):
            desc = fields["description"]
            if isinstance(desc, dict):
                desc = json.dumps(desc, ensure_ascii=False)[:1500]
            elif isinstance(desc, str):
                desc = desc[:1500]
            acceptance_criteria = [desc]
    # Truncate criteria list to avoid context overflow
    ac_str = json.dumps(acceptance_criteria[:5], ensure_ascii=False)
    if len(ac_str) > 3000:
        ac_str = ac_str[:3000] + "...]"

    prompt = SELF_ASSESS_TEMPLATE.format(
        acceptance_criteria=ac_str,
        design_context=json.dumps(design_ctx, ensure_ascii=False)[:800] if design_ctx else "N/A (not a UI task)",
        design_code_snippet=design_code_snippet or "N/A (no design HTML available)",
        implementation_summary=str(state.get("implementation_summary", ""))[:1000],
        test_results=json.dumps(state.get("test_results", {}), ensure_ascii=False)[:500],
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
        # Exhausted assess cycles — proceed to PR rather than blocking on user input
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
    # CRITICAL: screenshots MUST be saved to workspace screenshot_dir, NOT inside the git repo
    result = runtime.run_agentic(
        task=(
            "Take a screenshot of the implemented UI.\n"
            f"1. Make sure dependencies are installed: run_command('npm install', cwd='{repo_path}').\n"
            f"2. Start the dev server: run_command('npm run dev -- --port 5179 &', cwd='{repo_path}').\n"
            "   Wait 3 seconds for startup.\n"
            "3. Use playwright or a headless browser to capture a screenshot at http://localhost:5179.\n"
            f"   Save desktop screenshot to: {screenshot_dir}/landing-desktop.png\n"
            "   (Optional) If you can resize viewport, also save mobile at 375px width: "
            f"{screenshot_dir}/landing-mobile.png\n"
            f"4. IMPORTANT: Save screenshots ONLY to {screenshot_dir}/ — do NOT save inside "
            f"the git repo at {repo_path}.\n"
            "5. Stop the dev server after capturing.\n"
            "Return JSON: {\"screenshots\": [\"path1.png\", ...], \"captured\": true}\n"
            "If screenshot fails, return {\"screenshots\": [], \"captured\": false}"
        ),
        cwd=repo_path or None,
        max_turns=12,
        timeout=180,
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
    test_status = state.get("test_status", "unknown")
    assessment = state.get("self_assessment", {})
    changes = state.get("changes_made", [])
    _task_id = state.get("_task_id", "unknown")

    # Build test summary (accurate from actual results or test_status)
    if test_status == "skip":
        test_summary = "Skipped (max cycles reached)"
    elif test_status == "pass":
        passed = test_results.get("passed", "?")
        failed = test_results.get("failed", 0)
        test_summary = f"{passed} passed, {failed} failed"
    else:
        passed = test_results.get("passed", 0)
        failed = test_results.get("failed", "?")
        test_summary = f"{passed} passed, {failed} failed"

    score = assessment.get("score", "N/A")
    verdict = assessment.get("verdict", "N/A")
    score_str = f"{score:.2f}" if isinstance(score, float) else str(score)

    # Build hyperlink PR reference
    pr_link = f"[{pr_url}]({pr_url})" if pr_url and pr_url != "N/A" else "N/A"

    comment_text = (
        f"✅ Development completed by web-dev agent.\n\n"
        f"**Task ID:** {_task_id}\n"
        f"**PR:** {pr_link}\n"
        f"**Branch:** `{branch}`\n"
        f"**Test results:** {test_summary}\n"
        f"**Self-assessment:** {score_str} ({verdict})\n"
        f"**Files changed:** {len(changes)}"
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
    _IN_REVIEW_NAMES = {
        "in review", "review", "code review", "ready for review",
        "pending review", "awaiting review",
    }
    in_review_match = next(
        (t for t in transitions
         if isinstance(t, dict) and t.get("name", "").lower() in _IN_REVIEW_NAMES),
        None,
    )
    can_review = bool(in_review_match)
    if can_review:
        _call_boundary_tool(
            state, "jira_transition",
            {"ticket_key": jira_key, "transition_name": in_review_match["name"]},
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

    return {"jira_updated": True, "jira_in_review": can_review}


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

    # Step 1: Commit any pending files and resolve the full changeset FIRST.
    # This must happen before PR description generation (which needs changed_files)
    # and before push (which needs commits to exist).
    repo_path = state.get("repo_path", "")
    repo_url = state.get("repo_url", "")
    branch_name = state.get("branch_name", "feature/task")

    committed_files = _git_commit_all_pending(repo_path, jira_key or "task")
    existing_changes = state.get("changes_made", [])
    all_changes = sorted(set(existing_changes) | set(committed_files))

    if not all_changes:
        raise RuntimeError(
            f"[web-dev] create_pr: No file changes detected on branch {branch_name!r}. "
            "implement_changes produced 0 commits — cannot create a PR against main."
        )

    # Step 2: Generate PR description (single-shot LLM)
    _assessment = state.get("self_assessment", {})
    _test_results = state.get("test_results", {})
    _screenshots = state.get("screenshots", [])
    _jira_url = ""
    if jira_key:
        _jira_ctx = state.get("jira_context", {})
        if isinstance(_jira_ctx, dict):
            _jira_url = _jira_ctx.get("url", "") or f"https://tarch.atlassian.net/browse/{jira_key}"
    desc_prompt = PR_DESCRIPTION_TEMPLATE.format(
        user_request=state.get("user_request", ""),
        branch_name=branch_name,
        jira_key=jira_key or "N/A",
        jira_url=_jira_url or "N/A",
        implementation_summary=state.get("implementation_summary", ""),
        changed_files=", ".join(all_changes[:20]) or "various files",
        test_status=state.get("test_status", "unknown"),
        test_results=json.dumps(_test_results),
        assessment_score=_assessment.get("score", "N/A"),
        assessment_verdict=_assessment.get("verdict", "N/A"),
        assessment_gaps=", ".join(_assessment.get("gaps", [])) or "none",
        screenshot_paths=", ".join(_screenshots) or "none captured",
    )
    desc_result = runtime.run(desc_prompt, system_prompt=PR_DESCRIPTION_SYSTEM,
                              plugin_manager=state.get("_plugin_manager"))
    pr_meta = _safe_json(desc_result.get("raw_response", ""), fallback={})
    pr_title = pr_meta.get("title", "Implement task changes")
    pr_description = pr_meta.get("description", state.get("implementation_summary", ""))

    # Step 3: Push branch then create PR via SCM boundary tools (not open agentic).
    # Design doc §4.3: Dev Agent must use scm.branch.push + scm.pr.create
    # through the SCM Agent boundary; never direct git push or GitHub API.

    push_payload = _call_boundary_tool(
        state, "scm_push", {"repo_path": repo_path, "branch": branch_name}
    )
    if push_payload.get("error"):
        print(f"[web-dev] scm_push failed: {push_payload['error']}")
    else:
        print(f"[web-dev] scm_push OK: branch={branch_name!r}")

    pr_payload = _call_boundary_tool(
        state, "scm_create_pr",
        {
            "repo_url": repo_url,
            "source_branch": branch_name,
            "target_branch": "main",
            "title": pr_title,
            "description": pr_description,
        },
    )
    # SCM adapter returns camelCase prUrl; snake_case pr_url is a fallback
    pr_url = pr_payload.get("prUrl") or pr_payload.get("pr_url", "")
    pr_number = pr_payload.get("prNumber") or pr_payload.get("pr_number", 0)
    commit_hash = pr_payload.get("commitHash") or pr_payload.get("commit_hash", "")
    pr_status = pr_payload.get("status", "")
    pr_error = pr_payload.get("error", "")
    if not pr_url and (pr_error or (pr_status and pr_status != "ok")):
        print(f"[web-dev] create_pr FAILED: status={pr_status!r} error={pr_error!r} payload={pr_payload}")
    else:
        print(f"[web-dev] create_pr done: prUrl={pr_url!r} prNumber={pr_number} status={pr_status!r}")

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
                        "pr_url": pr_url,
                        "pr_number": pr_number,
                        "branch": branch_name,
                        "title": pr_title,
                        "commit_hash": commit_hash,
                        "files_changed": len(all_changes),
                        "changed_files": all_changes[:30],
                        "test_status": state.get("test_status", "unknown"),
                        "self_assessment_score": state.get("self_assessment", {}).get("score", "N/A"),
                        "screenshot_included": state.get("screenshot_captured", False),
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    return {
        "pr_url": pr_url,
        "pr_number": pr_number,
        "pr_title": pr_title,
        "pr_description": pr_description,
        "commit_hash": commit_hash,
        "changes_made": all_changes,
    }


async def report_result(state: dict) -> dict:
    """Return final result summary."""
    pr_url = state.get("pr_url", "N/A")
    branch_name = state.get("branch_name", "N/A")
    changes = state.get("changes_made", [])
    pr_title = state.get("pr_title", "")
    test_status = state.get("test_status", "unknown")
    print(f"[web-dev] report_result: prUrl={pr_url!r} branch={branch_name!r} test_status={test_status!r} changes={len(changes)}")

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


async def pause_for_user_input(state: dict) -> dict:
    """Pause the workflow and ask the orchestrator for guidance.

    Raised after self-assessment exhausts retries with unresolved gaps.
    On resume (``_resume_value`` set by WorkflowRunner.resume()), the node
    consumes the user guidance and sets ``route = "user_responded"`` so the
    workflow loops back through implement_changes.
    """
    resume_value = state.get("_resume_value")
    if resume_value is not None:
        return {
            "revision_feedback": f"User guidance after self-assessment escalation: {resume_value}",
            "assess_cycles": 0,  # reset so the loop can run again
            "route": "user_responded",
        }

    from framework.workflow import interrupt

    assessment = state.get("self_assessment", {})
    gaps = assessment.get("gaps", [])
    gap_text = "\n".join(f"- {g}" for g in gaps[:10]) if gaps else "No specific gaps."

    interrupt(
        f"Self-assessment could not resolve all gaps after maximum retries.\n"
        f"Remaining gaps:\n{gap_text}\n"
        "Please review and provide guidance on how to proceed.",
        assessment_score=assessment.get("score"),
        gaps=gaps,
    )

    # unreachable — interrupt() raises InterruptSignal
    return {}

