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
import subprocess
import sys
from pathlib import Path as _Path
from typing import Any

from framework.config import load_agent_config as _load_agent_cfg
from framework.devlog import AgentLogger

# Load agent_id from config.yaml — single source of truth for identity
_AGENT_ID: str = _load_agent_cfg(
    _Path(__file__).parent.name.replace("_", "-")
).get("agent_id", _Path(__file__).parent.name.replace("_", "-"))


def _logger(state: dict) -> AgentLogger:
    """Return an AgentLogger for this agent using the task_id stored in state."""
    return AgentLogger(state.get("_task_id", ""), _AGENT_ID)


def _boundary_log(state: dict, agent_id: str, message: str, **kwargs: Any) -> None:
    """Deprecated proxy log — kept only to avoid breaking call sites in this file.

    web_dev/nodes.py should pass task_id to boundary tool args instead.
    This function is a no-op: each boundary agent logs to its own directory.
    """


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
    # Try to find JSON object or array in the response first
    match = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    # Fall back to parsing the whole text
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        pass
    # Last resort: try stripping markdown code fences
    stripped = re.sub(r"```(?:json)?\s*", "", text.strip()).strip()
    if stripped.startswith("{") or stripped.startswith("["):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    return fallback


def _run_mandatory_validation(repo_path: str, workspace_path: str, cycle: int) -> dict:
    """Run install, build, and tests through the deterministic validation script."""
    script_path = _Path(__file__).resolve().parent / "scripts" / "validate_project.py"
    output_path = ""
    if workspace_path:
        output_path = os.path.join(
            workspace_path,
            _AGENT_ID,
            "test-results",
            f"validation-run-{cycle}.json",
        )

    command = [sys.executable, str(script_path), repo_path]
    if output_path:
        command.extend(["--output", output_path])

    proc = subprocess.run(
        command,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=int(os.environ.get("WEB_DEV_VALIDATION_TIMEOUT", "2400")),
        check=False,
    )
    data = _safe_json(proc.stdout or "", fallback={}) or {}
    if output_path and os.path.isfile(output_path):
        try:
            with open(output_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            pass
    if proc.returncode != 0:
        data.setdefault("failed", 1)
        data.setdefault("errors", []).append("mandatory validation script failed")
    data.setdefault("output", proc.stdout or "")
    return data


def _tail_text(text: str, limit: int = 600) -> str:
    """Return the last *limit* characters of text for compact logging."""
    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[-limit:]


def _git_worktree_changed_files(repo_path: str) -> list[str]:
    """Return tracked/untracked worktree files from git status."""
    if not repo_path or not os.path.isdir(repo_path):
        return []
    try:
        from framework.env_utils import build_isolated_git_env

        proc = subprocess.run(
            ["git", "status", "--short"],
            cwd=repo_path,
            env=build_isolated_git_env(scope="web-dev-worktree-status"),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    files: list[str] = []
    for line in proc.stdout.splitlines():
        path = line[3:].strip() if len(line) > 3 else line.strip()
        if path:
            files.append(path)
    return sorted(set(files))


def _git_branch_changed_files(repo_path: str, base_ref: str = "main") -> list[str]:
    """Return files changed on the current branch relative to *base_ref*."""
    if not repo_path or not os.path.isdir(repo_path):
        return []
    try:
        from framework.env_utils import build_isolated_git_env

        proc = subprocess.run(
            ["git", "diff", "--name-only", f"{base_ref}..HEAD"],
            cwd=repo_path,
            env=build_isolated_git_env(scope="web-dev-branch-status"),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception:
        return []
    if proc.returncode != 0:
        return []
    return sorted({line.strip() for line in proc.stdout.splitlines() if line.strip()})


def _summarize_validation_commands(data: dict) -> list[dict[str, Any]]:
    """Return compact validation command summaries for agent.log."""
    summaries: list[dict[str, Any]] = []
    for command in data.get("commands") or []:
        parts = command.get("command") or []
        summaries.append(
            {
                "command": " ".join(str(part) for part in parts),
                "returncode": command.get("returncode"),
                "duration_seconds": command.get("duration_seconds"),
            }
        )
    return summaries


def _call_boundary_tool(state: dict, tool_name: str, args: dict) -> dict:
    """Call a boundary agent tool via the global ToolRegistry.

    Returns the parsed JSON payload or an error dict.
    """
    from framework.tools.registry import get_registry

    registry = get_registry()
    tool_args = dict(args)
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    permissions = metadata.get("permissions")
    if isinstance(permissions, dict) and not isinstance(tool_args.get("permissions"), dict):
        tool_args["permissions"] = permissions
    try:
        result_str = registry.execute_sync(tool_name, tool_args)
        return json.loads(result_str) if result_str else {}
    except Exception as exc:
        print(f"[{_AGENT_ID}] Tool {tool_name} failed: {exc}")
        return {"error": str(exc)}


def _is_screenshot_required(state: dict) -> bool:
    """Return whether this task must produce PNG implementation screenshots."""
    definition_of_done = state.get("definition_of_done") or {}
    if isinstance(definition_of_done, dict) and "screenshot_required" in definition_of_done:
        return bool(definition_of_done.get("screenshot_required"))

    if state.get("design_context") or state.get("design_spec"):
        return True
    if state.get("stitch_screen_id") or state.get("stitch_screen_name"):
        return True

    task_signals = " ".join(
        str(state.get(key, "")).lower()
        for key in ("task_type", "classification", "work_type")
    )
    return any(token in task_signals for token in ("ui", "frontend", "front-end", "visual", "design"))


def _rendered_page_has_content(metrics: dict[str, Any]) -> bool:
    """Return True when the browser page shows enough evidence of real rendering."""
    root_children = int(metrics.get("rootChildren") or 0)
    body_children = int(metrics.get("bodyChildren") or 0)
    visible_text_chars = int(metrics.get("visibleTextChars") or 0)
    body_width = int(metrics.get("bodyWidth") or 0)
    body_height = int(metrics.get("bodyHeight") or 0)
    return (
        (root_children > 0 or body_children > 1 or visible_text_chars >= 20)
        and body_width > 0
        and body_height > 0
    )


_ICON_LIGATURE_TOKENS = (
    "arrow_forward",
    "arrow_back",
    "arrow_upward",
    "arrow_downward",
    "chevron_right",
    "chevron_left",
    "navigate_next",
    "navigate_before",
    "expand_more",
    "expand_less",
    "close",
    "menu",
    "search",
)


def _detect_fragile_icon_font_usage(repo_path: str) -> dict[str, Any]:
    """Detect icon-font ligature patterns that render unreliably in containers."""
    findings: dict[str, Any] = {
        "issues": [],
        "files": [],
        "icon_tokens": [],
        "uses_material_icon_class": False,
        "uses_remote_material_font": False,
    }
    if not repo_path or not os.path.isdir(repo_path):
        return findings

    text_exts = {".html", ".css", ".scss", ".sass", ".less", ".js", ".jsx", ".ts", ".tsx"}
    ignored_dirs = {".git", "node_modules", "dist", "build", ".next", "coverage"}
    risky_files: set[str] = set()
    icon_tokens: set[str] = set()

    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [name for name in dirs if name not in ignored_dirs]
        for filename in files:
            if os.path.splitext(filename)[1].lower() not in text_exts:
                continue
            full_path = os.path.join(root, filename)
            rel_path = os.path.relpath(full_path, repo_path)
            try:
                with open(full_path, encoding="utf-8", errors="ignore") as fh:
                    content = fh.read()
            except OSError:
                continue
            lowered = content.lower()

            if "material-symbols" in lowered or "material-icons" in lowered:
                findings["uses_material_icon_class"] = True
                risky_files.add(rel_path)

            if (
                "fonts.googleapis.com" in lowered
                and ("material+symbols" in lowered or "material+icons" in lowered or "icon?family=material+icons" in lowered)
            ):
                findings["uses_remote_material_font"] = True
                risky_files.add(rel_path)

            for token in _ICON_LIGATURE_TOKENS:
                if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", lowered):
                    icon_tokens.add(token)
                    risky_files.add(rel_path)

    findings["files"] = sorted(risky_files)
    findings["icon_tokens"] = sorted(icon_tokens)

    if findings["uses_material_icon_class"] or findings["icon_tokens"]:
        preview = ", ".join(findings["icon_tokens"][:3]) or "material icon ligatures"
        findings["issues"].append(
            "Fragile icon font usage detected "
            f"({preview}) in {', '.join(findings['files'][:4])}. "
            "Replace icon-font ligatures with inline SVG or a local React icon component so container screenshots never show icon names as text."
        )
    if findings["uses_material_icon_class"] and not findings["uses_remote_material_font"]:
        findings["issues"].append(
            "Material icon classes are present without a matching icon font stylesheet. "
            "The page can render icon names as plain text."
        )

    return findings


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
                print(f"[{_AGENT_ID}] committed {len(staged_files)} pending file(s): {staged_files[:8]}")
            else:
                print(f"[{_AGENT_ID}] commit failed: {r.stderr.strip()[:200]}")
            return staged_files
        else:
            # Confirm there is at least one commit on the branch, and list its changed files
            log_result = subprocess.run(
                ["git", "log", "--oneline", "-1"],
                cwd=repo_path, env=git_env,
                capture_output=True, text=True, timeout=10,
            )
            print(f"[{_AGENT_ID}] no pending changes; last commit: {log_result.stdout.strip()[:120]!r}")
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
        print(f"[{_AGENT_ID}] _git_commit_all_pending error (non-fatal): {exc}")
        return []


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------

async def prepare_jira(state: dict) -> dict:
    """Update Jira before implementation starts."""
    log = _logger(state)
    log.node("prepare_jira")
    jira_context = state.get("jira_context", {})
    jira_key = (
        jira_context.get("key")
        or jira_context.get("ticket_key")
        or state.get("jira_key", "")
    )
    log.debug("prepare_jira", jira_key=jira_key)
    print(f"[{_AGENT_ID}] prepare_jira: jira_key={jira_key!r}")

    if not jira_key:
        log.info("prepare_jira skipped — no jira_key")
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
    token_user_account_id = ""
    task_id = state.get("_task_id", "")
    token_user_result = _call_boundary_tool(state, "jira_get_token_user", {"task_id": task_id})
    if not token_user_result.get("error"):
        user_data = token_user_result.get("user", {})
        token_user = user_data.get("emailAddress", user_data.get("displayName", ""))
        token_user_account_id = user_data.get("accountId", "")

    # Transition to "In Progress" if not already
    if original_status.lower() not in ("in progress", "in development", "in dev"):
        transitions_result = _call_boundary_tool(
            state, "jira_list_transitions", {"ticket_key": jira_key, "task_id": task_id}
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
                {"ticket_key": jira_key, "transition_name": in_progress_match["name"],
                 "task_id": task_id},
            )
        else:
            avail = [t.get("name") for t in transitions if isinstance(t, dict)]
            print(f"[{_AGENT_ID}] Cannot transition {jira_key} to In Progress; available: {avail}")

    # Update assignee to token user (use accountId for Jira Cloud)
    if token_user_account_id:
        log.info("assigning jira ticket to token user in prepare", jira_key=jira_key,
                 account_id=token_user_account_id)
        _call_boundary_tool(
            state, "jira_update",
            {"ticket_key": jira_key,
             "fields": {"assignee": {"accountId": token_user_account_id}},
             "task_id": task_id},
        )
    elif token_user and token_user != original_assignee:
        # Fallback for Jira Server (uses emailAddress)
        _call_boundary_tool(
            state, "jira_update",
            {"ticket_key": jira_key, "fields": {"assignee": {"emailAddress": token_user}},
             "task_id": task_id},
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
            "task_id": task_id,
        },
    )
    log.info("prepare_jira complete", jira_key=jira_key, token_user=token_user)

    # Write jira-prepare-log.json
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        import time as _time
        agent_dir = os.path.join(workspace_path, _AGENT_ID)
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
    """Create a working branch in the cloned repository."""
    runtime = state.get("_runtime")
    log = _logger(state)
    log.node("setup_workspace")
    repo_url = state.get("repo_url", "")
    repo_path = state.get("repo_path", "")
    workspace_path = state.get("workspace_path", "")
    branch_name = state.get("branch_name", "")
    task_id = state.get("_task_id", "unknown")
    log.debug("setup_workspace", repo_path=repo_path)
    print(f"[{_AGENT_ID}] setup_workspace: repo_path={repo_path!r} workspace_path={workspace_path!r}")

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
            f"[{_AGENT_ID}] Repo not found at {repo_path!r}. "
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

    local_branch_exists = False
    if repo_path and os.path.isdir(repo_path) and branch_name:
        import subprocess
        from framework.env_utils import build_isolated_git_env

        git_env = build_isolated_git_env("web-dev-setup-local-branch")
        exists = subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
            cwd=repo_path,
            env=git_env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        local_branch_exists = exists.returncode == 0

    # -- Check remote branches and open PR source branches for conflicts; add _<n> suffix when taken --
    # Must not delete or alter existing remote branches or PRs.
    if branch_name and repo_url and not local_branch_exists:
        remote_result = _call_boundary_tool(state, "scm_list_branches", {"repo_url": repo_url})
        remote_branch_names = {
            candidate
            for b in remote_result.get("branches", [])
            for candidate in [
                b.get("displayId", ""),
                b.get("name", ""),
                str(b.get("id", "")).replace("refs/heads/", ""),
            ]
            if candidate
        }
        pr_result = _call_boundary_tool(state, "scm_list_prs", {"repo_url": repo_url, "state": "open"})
        reserved_pr_branches = {
            str(pr.get("fromBranch") or pr.get("fromRef") or pr.get("sourceBranch") or "").strip()
            for pr in pr_result.get("prs", [])
            if str(pr.get("fromBranch") or pr.get("fromRef") or pr.get("sourceBranch") or "").strip()
        }
        reserved_names = remote_branch_names | reserved_pr_branches
        if branch_name in reserved_names:
            base_name = branch_name
            n = 2
            while f"{base_name}_{n}" in reserved_names:
                n += 1
            new_name = f"{base_name}_{n}"
            print(
                f"[{_AGENT_ID}] setup_workspace: branch {branch_name!r} is already reserved "
                f"by a remote branch or open PR, using {new_name!r} to avoid conflict"
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
            print(f"[{_AGENT_ID}] setup_workspace: created branch {branch_name!r}")
        else:
            # Branch might already exist — try switching to it
            r2 = subprocess.run(
                ["git", "checkout", branch_name],
                cwd=repo_path, env=git_env,
                capture_output=True, text=True, timeout=30,
            )
            if r2.returncode == 0:
                branch_created = True
                print(f"[{_AGENT_ID}] setup_workspace: switched to existing branch {branch_name!r}")
            else:
                print(f"[{_AGENT_ID}] setup_workspace: git checkout failed: {r2.stderr.strip()[:200]}")
                raise RuntimeError(
                    f"[{_AGENT_ID}] Failed to create/switch branch {branch_name!r}: {r2.stderr.strip()[:200]}"
                )

    # Write git setup log
    if workspace_path:
        agent_dir = os.path.join(workspace_path, _AGENT_ID)
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
    """Understand requirements and produce an implementation plan."""
    import time as _time
    log = _logger(state)
    log.node("analyze_task")
    print(f"[{_AGENT_ID}] analyze_task: building implementation plan")

    workspace_path = state.get("workspace_path", "")
    analysis = state.get("analysis") or state.get("user_request", "")

    # Try to load Team Lead's delivery-plan.json for structured plan data
    delivery_plan: dict = {}
    if workspace_path:
        plan_path = os.path.join(workspace_path, "team-lead", "delivery-plan.json")
        try:
            with open(plan_path, encoding="utf-8") as fh:
                doc = json.load(fh)
                delivery_plan = doc.get("data", doc)
                print(f"[{_AGENT_ID}] Loaded delivery-plan.json from {plan_path}")
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
        jira_path = os.path.join(workspace_path, "team-lead", "jira-ticket.json")
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
    structured_plan = {
        "implementation_steps": [],
        "test_plan": [],
        "risks": [],
    }
    if isinstance(delivery_plan, dict) and delivery_plan.get("steps"):
        structured_plan["implementation_steps"] = [
            str(step.get("action") or step.get("description") or step)
            for step in delivery_plan.get("steps", [])
            if isinstance(step, dict) or step
        ]
    if not structured_plan["implementation_steps"] and plan:
        structured_plan["implementation_steps"] = [plan]
    structured_plan["test_plan"] = [
        "Run deterministic install, build, and test validation before PR creation."
    ]
    structured_plan["risks"] = [
        "External service credentials or repository baseline health may affect validation."
    ]

    from framework.validation_gates import validate_implementation_plan
    gate_result = validate_implementation_plan(structured_plan)
    if not gate_result.passed:
        raise RuntimeError(f"Implementation plan gate failed: {gate_result.feedback}")

    # Write implementation-plan.json to workspace for auditability
    if workspace_path:
        agent_dir = os.path.join(workspace_path, _AGENT_ID)
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
                        "structured_plan": structured_plan,
                        "delivery_plan_loaded": bool(delivery_plan),
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    return {
        "implementation_plan": plan,
        "implementation_plan_details": structured_plan,
    }


async def implement_changes(state: dict) -> dict:
    """Write code based on the implementation plan."""
    runtime = state.get("_runtime")
    log = _logger(state)
    log.node("implement_changes", repo_path=state.get("repo_path", ""),
             branch=state.get("branch_name", ""))
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
    # Prefer ui-design/stitch/code.html, fallback to team-lead/design-code.html
    if not _design_code_path and _workspace_path:
        _stitch_code = os.path.join(_workspace_path, "ui-design", "stitch", "code.html")
        _legacy_code = os.path.join(_workspace_path, "team-lead", "design-code.html")
        _design_code_path = _stitch_code if os.path.isfile(_stitch_code) else _legacy_code
    if _design_code_path and os.path.isfile(_design_code_path):
        try:
            with open(_design_code_path, encoding="utf-8") as _f:
                _design_code_ref = _f.read()
        except Exception:
            pass

    # Load design spec markdown (typography/colors/spacing) for reference
    _design_spec_md = "N/A"
    if _workspace_path:
        _stitch_md_path = os.path.join(_workspace_path, "ui-design", "stitch", "DESIGN.md")
        _legacy_md_path = os.path.join(_workspace_path, "team-lead", "design-spec.md")
        _design_spec_md_path = state.get("design_md_path", "") or (
            _stitch_md_path if os.path.isfile(_stitch_md_path) else _legacy_md_path
        )
        if os.path.isfile(_design_spec_md_path):
            try:
                with open(_design_spec_md_path, encoding="utf-8") as _f:
                    _design_spec_md = _f.read()
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
        design_spec_markdown=_design_spec_md,
        skill_context=state.get("skill_context", ""),
        memory_context=state.get("memory_context", ""),
    )

    # Use Claude Code native tools (Bash, Read, Write, Glob, Grep) — no constellation
    # MCP bridge needed.  With cwd=repo_path, all relative paths resolve correctly.
    repo_path = state.get("repo_path", "")
    branch_name = state.get("branch_name", "")
    changed_before = set(_git_branch_changed_files(repo_path)) | set(_git_worktree_changed_files(repo_path))
    log.info(
        "implement_changes started",
        repo_path=repo_path,
        branch=branch_name,
        jira_local_folder=state.get("jira_local_folder", ""),
        design_local_folder=state.get("design_local_folder", ""),
    )
    print(f"[{_AGENT_ID}] implement_changes: repo_path={state.get('repo_path', '')!r} (native tools)")
    result = runtime.run_agentic(
        task=prompt,
        system_prompt=IMPLEMENT_SYSTEM,
        cwd=state.get("repo_path") or None,
        tools=None,
        max_turns=50,
        timeout=1800,
        plugin_manager=state.get("_plugin_manager"),
    )
    changed_after = set(_git_branch_changed_files(repo_path)) | set(_git_worktree_changed_files(repo_path))
    changed_files = sorted(changed_after)
    new_files = sorted(changed_after - changed_before)
    log.info(
        "implement_changes result",
        success=result.success,
        turns=result.turns_used,
        files_changed=len(changed_files),
        new_files=len(new_files),
        files=changed_files[:12],
    )
    if new_files:
        log.debug("implement_changes new files", files=new_files[:20])
    if result.summary:
        log.debug("implement_changes summary", summary=result.summary[:500])
    print(f"[{_AGENT_ID}] implement_changes done: success={result.success} turns={result.turns_used} summary={result.summary[:300]!r}")

    if not result.success:
        # Before failing, check if claude committed code despite the error/timeout.
        # Claude often commits changes then continues with build/test verification which
        # may fail or time out — we should not discard committed work in that case.
        _commits_exist = False
        try:
            import subprocess as _sp
            from framework.env_utils import build_isolated_git_env as _bge
            _ge = _bge(scope="web-dev-impl-check")
            _diff = _sp.run(
                ["git", "diff", "--name-only", "main..HEAD"],
                cwd=state.get("repo_path", ""), capture_output=True, text=True,
                timeout=10, env=_ge,
            )
            _commits_exist = _diff.returncode == 0 and bool(_diff.stdout.strip())
        except Exception:
            pass
        if _commits_exist:
            print(f"[{_AGENT_ID}] implement_changes: agentic error ({result.summary[:200]!r}) "
                  f"but commits found on branch — proceeding with partial implementation")
            impl_summary = f"Partial implementation (stopped early). Commits present. Error: {result.summary[:200]}"
        else:
            raise RuntimeError(
                f"implement_changes failed — claude-code returned error: {result.summary[:500]}"
            )
    else:
        impl_summary = result.summary

    # With native tools, we can't track individual file writes from tool_calls.
    # changes_made is populated from git diff in create_pr via _git_commit_all_pending.

    # Validation gate: ensure at least some files were changed
    from framework.validation_gates import validate_files_changed
    gate_result = validate_files_changed(state.get("repo_path", ""))
    if not gate_result.passed and "No file changes detected" in gate_result.feedback:
        log.error("validate_files_changed gate failed", feedback=gate_result.feedback)
        raise RuntimeError(f"Implementation produced no file changes: {gate_result.feedback}")
    elif not gate_result.passed:
        log.warn("validate_files_changed gate inconclusive", feedback=gate_result.feedback)

    return {
        "changes_made": [],
        "implementation_summary": impl_summary,
        "agentic_success": result.success,
    }


async def run_tests(state: dict) -> dict:
    """Run project tests and evaluate results."""
    log = _logger(state)
    log.node("run_tests")
    runtime = state.get("_runtime")
    test_cycles = state.get("test_cycles", 0) + 1
    max_test_cycles = state.get("max_test_cycles") or int(
        os.environ.get("WEB_DEV_MAX_TEST_CYCLES", "3")
    )
    log.info("run_tests started", cycle=test_cycles, max_cycles=max_test_cycles)

    if not runtime:
        log.info("run_tests skipped — no runtime (test mode)")
        return {
            "test_results": {"passed": 1, "failed": 0, "output": ""},
            "test_cycles": test_cycles,
            "test_status": "pass",
            "route": "pass",
        }

    repo_path = state.get("repo_path", "")
    workspace_path = state.get("workspace_path", "")
    if not repo_path or not os.path.isdir(repo_path):
        raise RuntimeError("Mandatory validation cannot run because repo_path is missing")

    log.debug("run_tests running build+test", repo_path=repo_path)
    print(f"[{_AGENT_ID}] run_tests: cycle={test_cycles}/{max_test_cycles} repo_path={repo_path!r}")

    data = _run_mandatory_validation(repo_path, workspace_path, test_cycles)
    failed = data.get("failed", 0)
    install_ok = data.get("install_ok", True)
    build_ok = data.get("build_ok", False)
    test_ok = data.get("test_ok", False)
    test_passed = int(failed) == 0 and install_ok and build_ok and test_ok
    command_summaries = _summarize_validation_commands(data)
    log.info("run_tests result", passed=data.get("passed", 0), failed=failed,
             install_ok=install_ok, build_ok=build_ok, test_ok=test_ok,
             test_passed=test_passed, cycle=test_cycles)
    if command_summaries:
        log.info("run_tests commands", commands=command_summaries)
    if data.get("errors"):
        log.warn("run_tests errors", errors=data.get("errors", []), output_tail=_tail_text(data.get("output", ""), 1200))
    else:
        log.debug("run_tests output tail", output_tail=_tail_text(data.get("output", ""), 500))

    # Write per-cycle test results for auditability
    if workspace_path:
        import time as _time
        results_dir = os.path.join(workspace_path, _AGENT_ID, "test-results")
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
            "test_output": data.get("output", ""),
            "test_cycles": test_cycles,
            "test_status": "pass",
            "route": "pass",
        }

    if test_cycles >= max_test_cycles:
        print(f"[{_AGENT_ID}] run_tests: max cycles reached ({test_cycles}/{max_test_cycles}); failing task")
        raise RuntimeError(
            "Mandatory validation failed after max cycles; Web Dev cannot proceed to self-assessment or PR"
        )

    return {
        "test_results": data,
        "test_output": data.get("output", ""),
        "test_cycles": test_cycles,
        "test_status": "fail",
        "route": "fail",
    }


async def fix_tests(state: dict) -> dict:
    """Fix failing tests based on test output."""
    log = _logger(state)
    log.node("fix_tests")
    runtime = state.get("_runtime")

    if not runtime:
        log.info("fix_tests skipped — no runtime")
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

    # Validation gate: ensure fix actually changed files
    from framework.validation_gates import validate_files_changed
    gate_result = validate_files_changed(state.get("repo_path", ""))
    if not gate_result.passed and "No file changes detected" in gate_result.feedback:
        log.warn("fix_tests produced no file changes", feedback=gate_result.feedback)

    return {
        "fix_attempted": True,
        "fix_summary": result.summary,
        "agentic_success": result.success,
    }


async def self_assess(state: dict) -> dict:
    """Run requirement-aware and design-aware self assessment."""
    log = _logger(state)
    log.node("self_assess")
    runtime = state.get("_runtime")
    assess_cycles = state.get("assess_cycles", 0) + 1
    max_assess_cycles = 3
    log.info("self_assess started", cycle=assess_cycles, max_cycles=max_assess_cycles)

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
        design_spec_path = os.path.join(workspace_path, "team-lead", "design-spec.json")
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
    # Prefer ui-design/stitch/code.html, fallback to team-lead/design-code.html
    if not design_code_path and workspace_path:
        _stitch_code = os.path.join(workspace_path, "ui-design", "stitch", "code.html")
        _legacy_code = os.path.join(workspace_path, "team-lead", "design-code.html")
        design_code_path = _stitch_code if os.path.isfile(_stitch_code) else _legacy_code
    if design_code_path and os.path.isfile(design_code_path):
        try:
            with open(design_code_path, encoding="utf-8") as _f:
                design_html = _f.read()
            design_code_snippet = design_html
        except Exception:
            pass

    # Load design spec markdown (typography/colors/spacing) for component comparison
    design_spec_markdown = ""
    if workspace_path:
        _stitch_md = os.path.join(workspace_path, "ui-design", "stitch", "DESIGN.md")
        _legacy_md = os.path.join(workspace_path, "team-lead", "design-spec.md")
        design_spec_md_path = state.get("design_md_path", "") or (
            _stitch_md if os.path.isfile(_stitch_md) else _legacy_md
        )
        if os.path.isfile(design_spec_md_path):
            try:
                with open(design_spec_md_path, encoding="utf-8") as _f:
                    design_spec_markdown = _f.read()
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

    # Derive changed files from the actual cloned repo's git status when
    # changes_made is empty (native tool runs don't track individual writes).
    changed_files_list = state.get("changes_made", [])
    if not changed_files_list:
        repo_path = state.get("repo_path", "")
        if repo_path and os.path.isdir(repo_path):
            try:
                import subprocess as _sp
                _st = _sp.run(
                    ["git", "status", "--short"],
                    capture_output=True, text=True, cwd=repo_path, timeout=10,
                )
                for _line in _st.stdout.splitlines():
                    _name = _line[3:].strip()
                    if _name:
                        changed_files_list.append(_name)
            except Exception:
                pass

    prompt = SELF_ASSESS_TEMPLATE.format(
        acceptance_criteria=ac_str,
        design_context=json.dumps(design_ctx, ensure_ascii=False)[:800] if design_ctx else "N/A (not a UI task)",
        design_code_snippet=design_code_snippet or "N/A (no design HTML available)",
        design_spec_markdown=design_spec_markdown or "N/A (no design spec available)",
        implementation_summary=str(state.get("implementation_summary", ""))[:1000],
        test_results=json.dumps(state.get("test_results", {}), ensure_ascii=False)[:500],
        changed_files="\n".join(changed_files_list) or "unknown",
    )

    result = runtime.run(
        prompt, system_prompt=SELF_ASSESS_SYSTEM,
        max_tokens=4096,
        plugin_manager=state.get("_plugin_manager"),
        cwd=state.get("repo_path") or None,
    )

    raw_response = result.get("raw_response", "")
    print(f"[{_AGENT_ID}] self_assess raw_response (first 500 chars): {raw_response[:500]!r}")
    data = _safe_json(raw_response, fallback={})
    if not data:
        print(f"[{_AGENT_ID}] self_assess _safe_json returned empty — raw_response type={type(raw_response).__name__}, len={len(raw_response) if raw_response else 0}")

    if not isinstance(data, dict):
        data = {}
    data.setdefault("criteria_checks", [])
    data.setdefault("component_checks", [])
    data.setdefault("gaps", [])
    data.setdefault("summary", "")

    deterministic_gaps: list[str] = []
    if _is_screenshot_required(state):
        icon_validation = _detect_fragile_icon_font_usage(state.get("repo_path", ""))
        deterministic_gaps.extend(icon_validation.get("issues") or [])
        if icon_validation.get("issues"):
            data["component_checks"].append(
                {
                    "component": "Icon rendering",
                    "status": "incomplete",
                    "notes": icon_validation["issues"][0],
                }
            )

    if deterministic_gaps:
        merged_gaps: list[str] = []
        for gap in [*(data.get("gaps") or []), *deterministic_gaps]:
            text = str(gap).strip()
            if text and text not in merged_gaps:
                merged_gaps.append(text)
        data["gaps"] = merged_gaps
        data["verdict"] = "fail"
        data["score"] = min(float(data.get("score", 0) or 0), 0.89)
        summary = str(data.get("summary", "")).strip()
        if deterministic_gaps[0] not in summary:
            data["summary"] = (summary + " " if summary else "") + deterministic_gaps[0]

    score = float(data.get("score", 0) or 0)
    verdict = data.get("verdict", "fail")
    gaps = data.get("gaps", [])

    # Validation gate: structural check on self-assessment output
    from framework.validation_gates import validate_self_assessment
    acceptance_criteria_count = len(acceptance_criteria) if isinstance(acceptance_criteria, list) else 0
    gate_result = validate_self_assessment(data, acceptance_criteria_count)
    if not gate_result.passed:
        log.warn("validate_self_assessment gate", feedback=gate_result.feedback)

    print(f"[{_AGENT_ID}] self_assess result: score={score} verdict={verdict} gaps={len(gaps)}")

    # Write self-assessment.json to workspace
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        import time as _time
        agent_dir = os.path.join(workspace_path, _AGENT_ID)
        os.makedirs(agent_dir, exist_ok=True)
        try:
            sa_file = os.path.join(agent_dir, f"self-assessment-{assess_cycles}.json")
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
        route = "pass"
    elif assess_cycles >= max_assess_cycles:
        failure_summary = "; ".join(str(gap) for gap in gaps[:4]) or str(data.get("summary", "self-assessment failed"))
        log.warn("self_assess exhausted retries", cycle=assess_cycles, failure_summary=failure_summary[:400])
        raise RuntimeError(f"self_assess failed after {max_assess_cycles} cycles: {failure_summary[:400]}")
    else:
        route = "fail"

    log.info(
        "self_assess result",
        score=score,
        verdict=verdict,
        gaps=len(gaps),
        route=route,
        cycle=assess_cycles,
    )
    if gaps:
        log.warn("self_assess gaps", gaps=gaps[:10], summary=str(data.get("summary", ""))[:300])

    if route == "pass":
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
    log = _logger(state)
    log.node("fix_gaps")
    runtime = state.get("_runtime")

    if not runtime:
        log.info("fix_gaps skipped — no runtime")
        return {"fix_gaps_attempted": True}

    from agents.web_dev.prompts import FIX_GAPS_SYSTEM, FIX_GAPS_TEMPLATE

    assessment = state.get("self_assessment", {})
    gaps = assessment.get("gaps", [])
    changed_files = state.get("changes_made", [])
    log.info("fix_gaps started", gaps=len(gaps), files_changed=len(changed_files))

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
    log.info("fix_gaps result", success=result.success, summary=result.summary[:300])

    return {
        "fix_gaps_attempted": True,
        "fix_gaps_summary": result.summary,
        "agentic_success": result.success,
    }


async def capture_screenshot(state: dict) -> dict:
    """Capture implementation screenshots from the production build.

    Screenshot strategy:
    - Server:   ``vite preview`` serving the production ``dist/`` build (primary).
                Falls back to ``vite dev`` when no ``dist/`` exists.
    - Browser:  Playwright Chromium (bundled, works in containers).
                Falls back to system Chrome/Chromium binary.
    - Last resort: HTML page snapshot.

    Using the production build ensures screenshots reflect the *final committed
    state* of the implementation — the same artefacts that will be deployed.
    External font requests (Google Fonts, etc.) are intercepted and aborted so
    that Playwright's ``load`` event fires quickly; React falls back to the
    system font stack for the screenshot while all layout / colour / spacing
    from Tailwind/CSS is fully applied.

    Screenshots are saved to the agent workspace directory only — they are NOT
    committed to the repository.  ``create_pr`` uploads them to GitHub via the
    release-assets CDN and embeds them in the PR description.
    """
    import subprocess
    import shutil
    import socket

    screenshot_required = _is_screenshot_required(state)
    log = _logger(state)

    if not screenshot_required:
        log.info("screenshot skipped", reason="not_required")
        return {"screenshot_captured": False, "screenshots": []}

    repo_path = state.get("repo_path", "")
    workspace_path = state.get("workspace_path", "")
    screenshot_dir = os.path.join(workspace_path, _AGENT_ID, "screenshots")
    screenshots = []

    if not repo_path or not os.path.isdir(repo_path):
        log.warn("capture_screenshot skipped — repo_path missing", repo_path=repo_path)
        if screenshot_required:
            raise RuntimeError("Required UI screenshot capture failed: repository path is missing")
        return {"screenshot_captured": False, "screenshots": []}

    try:
        os.makedirs(screenshot_dir, exist_ok=True)
    except OSError:
        pass

    log.step("capture_screenshot", screenshot_dir=screenshot_dir)
    print(f"[{_AGENT_ID}] capture_screenshot: repo_path={repo_path!r} screenshot_dir={screenshot_dir!r}")

    # --- Detect the correct URL path(s) to screenshot ---
    # Parse the app's router file to find implemented routes. This is needed
    # because a feature page is often served at a non-root path (e.g. /lessons),
    # and navigating to "/" would show a blank screen for SPA apps that define
    # only feature routes.
    def _detect_app_routes(repo_root: str, task_hint: str = "") -> list[tuple[str, str]]:
        """Return [(url_path, slug)] for the most relevant screenshot targets.

        Reads the app's main router file, extracts all non-root path definitions,
        and returns the best match(es) for the current task.  Falls back to
        [("/", "home")] when no routes can be detected.

        Args:
            repo_root: Absolute path to the repository root.
            task_hint: Jira summary / task description used to score routes.
        Returns:
            List of (url_path, slug) tuples, e.g. [("/lessons", "lessons")].
            slug is used as the filename prefix, e.g. "lessons-desktop.png".
        """
        import re as _re

        router_candidates = [
            "src/App.tsx", "src/App.jsx", "src/App.ts", "src/App.js",
            "src/app.tsx", "src/app.jsx",
            "src/router.tsx", "src/router.jsx", "src/Router.tsx", "src/Router.jsx",
            "src/routes.tsx", "src/routes.jsx", "src/Routes.tsx",
            "src/routing.tsx", "src/routing.jsx",
            "src/main.tsx", "src/main.jsx",
            "app/layout.tsx",       # Next.js App Router root
            "app/page.tsx",         # Next.js App Router index
            "pages/index.tsx",      # Next.js Pages Router
        ]

        found_routes: list[str] = []
        for candidate in router_candidates:
            fpath = os.path.join(repo_root, candidate)
            if not os.path.isfile(fpath):
                continue
            try:
                content = open(fpath, encoding="utf-8").read()
                # Extract path="..." / path='...' from Route JSX (<Route path="...">)
                paths_jsx = _re.findall(r'path=["\']([^"\'*?{}]+)["\']', content)
                # Extract string literals in router config objects: { path: "..." }
                paths_obj = _re.findall(r'path:\s*["\']([^"\'*?{}]+)["\']', content)
                for p in paths_jsx + paths_obj:
                    p = p.strip()
                    if p and p != "/" and not p.startswith("*") and p not in found_routes:
                        if not p.startswith("/"):
                            p = "/" + p
                        found_routes.append(p)
            except Exception:
                continue

        # Next.js App Router: infer routes from directory structure
        if not found_routes:
            app_dir = os.path.join(repo_root, "app")
            if os.path.isdir(app_dir):
                for entry in os.listdir(app_dir):
                    entry_path = os.path.join(app_dir, entry)
                    if os.path.isdir(entry_path) and not entry.startswith("(") and not entry.startswith("_"):
                        page_file = os.path.join(entry_path, "page.tsx")
                        if not os.path.isfile(page_file):
                            page_file = os.path.join(entry_path, "page.jsx")
                        if os.path.isfile(page_file):
                            found_routes.append("/" + entry)

        if not found_routes:
            # No feature routes detected — use root with generic "app" slug
            return [("/", "app")]

        # Score routes against task_hint to find best match
        task_lower = (task_hint or "").lower()
        if task_lower and len(found_routes) > 1:
            def _score(route: str) -> int:
                slug = route.strip("/").lower().replace("-", " ").replace("_", " ")
                if not slug:
                    return 0
                # Award 1 point per matching word
                return sum(1 for word in slug.split() if word and word in task_lower)

            scored = sorted(found_routes, key=_score, reverse=True)
            best_score = _score(scored[0])
            if best_score > 0:
                found_routes = [r for r in scored if _score(r) == best_score]
                if not found_routes:
                    found_routes = scored[:1]

        # Build (url_path, slug) tuples — at most 2 routes to keep PR concise
        result: list[tuple[str, str]] = []
        for route in found_routes[:2]:
            slug = route.strip("/").replace("/", "-") or "app"
            # Sanitize slug: keep alphanumeric, hyphens, underscores
            slug = _re.sub(r"[^a-z0-9\-_]", "-", slug.lower())
            result.append((route, slug))
        return result

    # Gather task context to help route scoring
    _jira_ctx = state.get("jira_context", {})
    _jira_summary = (
        _jira_ctx.get("summary")
        or (_jira_ctx.get("fields") or {}).get("summary", "")
        or state.get("stitch_screen_name", "")
        or state.get("user_request", "")
    )
    _detected_routes = _detect_app_routes(repo_path, _jira_summary)
    print(f"[{_AGENT_ID}] detected screenshot routes: {_detected_routes!r} (hint={_jira_summary[:60]!r})")

    # Use first detected route for desktop/mobile pair
    _primary_route, _feature_slug = _detected_routes[0] if _detected_routes else ("/", "app")
    desktop_png = os.path.join(screenshot_dir, f"{_feature_slug}-desktop.png")
    mobile_png = os.path.join(screenshot_dir, f"{_feature_slug}-mobile.png")

    # Pick an ephemeral port to avoid conflicts with any server started by run_tests.
    def _free_port(preferred: int = 5179) -> int:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", 0))
                return s.getsockname()[1]
        except Exception:
            return preferred

    PORT = _free_port()
    dev_proc = None

    try:
        import time as _time
        import urllib.request

        # --- Step 1: Kill any leftover process on chosen port (safety net) ---
        subprocess.run(
            ["bash", "-c", f"lsof -ti:{PORT} | xargs kill -9 2>/dev/null || true"],
            timeout=5, capture_output=True,
        )

        # --- Step 2: Decide server mode: vite preview (prod build) or vite dev ---
        dist_dir = os.path.join(repo_path, "dist")
        use_preview = os.path.isdir(dist_dir)

        if not use_preview:
            # run_tests should have built dist/; if missing, try to build now.
            print(f"[{_AGENT_ID}] dist/ not found — running npm run build before screenshot")
            node_modules = os.path.join(repo_path, "node_modules")
            if not os.path.isdir(node_modules):
                subprocess.run(
                    ["npm", "install", "--prefer-offline"],
                    cwd=repo_path, timeout=120, capture_output=True,
                )
            build_result = subprocess.run(
                ["npm", "run", "build"],
                cwd=repo_path, timeout=300, capture_output=True, text=True,
            )
            use_preview = build_result.returncode == 0 and os.path.isdir(dist_dir)
            if not use_preview:
                print(f"[{_AGENT_ID}] build failed (rc={build_result.returncode}) — "
                      f"falling back to vite dev")
                # Ensure deps are present for dev server
                node_modules = os.path.join(repo_path, "node_modules")
                if not os.path.isdir(node_modules):
                    subprocess.run(
                        ["npm", "install", "--prefer-offline"],
                        cwd=repo_path, timeout=120, capture_output=True,
                    )
        else:
            print(f"[{_AGENT_ID}] dist/ found — using vite preview for production-accurate screenshot")

        # --- Step 3: Start server (preview preferred, dev fallback) ---
        if use_preview:
            # vite preview serves the production dist/ build
            server_cmd = ["npx", "vite", "preview", "--port", str(PORT), "--host", "0.0.0.0"]
        else:
            server_cmd = ["npm", "run", "dev", "--", "--port", str(PORT), "--host", "0.0.0.0"]

        dev_proc = subprocess.Popen(
            server_cmd,
            cwd=repo_path,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        server_type = "preview" if use_preview else "dev"
        print(f"[{_AGENT_ID}] {server_type} server started (pid={dev_proc.pid}) on port {PORT}")

        # --- Step 4: Wait for server ready (up to 60s) ---
        server_ready = False
        for _ in range(30):
            _time.sleep(2)
            try:
                conn = urllib.request.urlopen(f"http://localhost:{PORT}", timeout=3)
                if conn.status < 500:
                    server_ready = True
                    conn.close()
                    break
                conn.close()
            except Exception:
                pass
        print(f"[{_AGENT_ID}] Server ready={server_ready} (type={server_type})")

        if not server_ready:
            print(f"[{_AGENT_ID}] Server not ready in 60s — skipping screenshot")
        else:
            # --- Step 5: Take screenshots (playwright primary, Chrome fallback) ---
            playwright_ok = False
            try:
                from playwright.async_api import async_playwright
                print(f"[{_AGENT_ID}] Taking screenshots with playwright "
                      f"(wait_until=load, fonts with timeout)...")
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(args=[
                        "--no-sandbox",
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                    ])

                    async def _take_page_screenshot(
                        browser_inst,
                        viewport: dict,
                        out_path: str,
                        url_path: str = "/",
                        is_mobile: bool = False,
                    ) -> bool:
                        """Navigate to url_path, wait for render, screenshot; return True on success."""
                        ctx = await browser_inst.new_context(
                            viewport=viewport,
                            is_mobile=is_mobile,
                        )
                        pg = await ctx.new_page()
                        console_errors: list[str] = []
                        page_errors: list[str] = []

                        def _capture_console(msg):
                            try:
                                if msg.type in {"error", "warning"}:
                                    console_errors.append(msg.text)
                            except Exception:
                                pass

                        def _capture_page_error(exc):
                            try:
                                page_errors.append(str(exc))
                            except Exception:
                                pass

                        pg.on("console", _capture_console)
                        pg.on("pageerror", _capture_page_error)
                        try:
                            # No font blocking — allow all requests through so the browser
                            # loads Google Fonts normally (Work Sans, Newsreader, Material
                            # Symbols). Wait for the Font Loading API as well so screenshot
                            # evidence reflects the real rendered icon glyphs instead of the
                            # raw icon token text.

                            # Navigate to the detected feature URL (not just root).
                            # For React Router SPAs, vite preview serves all routes
                            # via the same index.html; the client-side router handles
                            # the path.  For vite dev server, --host already enables this.
                            target_url = f"http://localhost:{PORT}{url_path}"
                            print(f"[{_AGENT_ID}] Playwright navigating to {target_url!r}")
                            # Use 'load' (not 'networkidle') — safe with blocked CDNs.
                            await pg.goto(
                                target_url,
                                wait_until="load",
                                timeout=30000,
                            )
                            try:
                                await pg.evaluate(
                                    """async () => {
                                        if (document.fonts && document.fonts.ready) {
                                            await document.fonts.ready;
                                        }
                                    }"""
                                )
                            except Exception:
                                pass
                            # Wait for React to hydrate and CSS animations to settle.
                            # Use a longer wait for production builds via vite preview
                            # since the bundled JS needs to parse + execute + render.
                            await pg.wait_for_timeout(5000)
                            # Best-effort: verify the React root has rendered content.
                            # IMPORTANT: wait_for_selector with state="visible" returns
                            # immediately if the element exists but is empty. We must
                            # actively check for non-empty content.
                            root = pg.locator("#root")
                            try:
                                await root.wait_for(state="attached", timeout=5000)
                            except Exception:
                                pass

                            metrics = await pg.evaluate(
                                """() => {
                                    const root = document.querySelector('#root');
                                    const body = document.body;
                                    const rect = body ? body.getBoundingClientRect() : { width: 0, height: 0 };
                                    const visibleText = ((body && body.innerText) || '').replace(/\\s+/g, ' ').trim();
                                    return {
                                        rootChildren: root ? root.children.length : 0,
                                        bodyChildren: body ? body.children.length : 0,
                                        visibleTextChars: visibleText.length,
                                        bodyWidth: Math.round(rect.width || 0),
                                        bodyHeight: Math.round(rect.height || 0),
                                        title: document.title || '',
                                        readyState: document.readyState || '',
                                    };
                                }"""
                            )
                            if not _rendered_page_has_content(metrics):
                                await pg.wait_for_timeout(4000)
                                metrics = await pg.evaluate(
                                    """() => {
                                        const root = document.querySelector('#root');
                                        const body = document.body;
                                        const rect = body ? body.getBoundingClientRect() : { width: 0, height: 0 };
                                        const visibleText = ((body && body.innerText) || '').replace(/\\s+/g, ' ').trim();
                                        return {
                                            rootChildren: root ? root.children.length : 0,
                                            bodyChildren: body ? body.children.length : 0,
                                            visibleTextChars: visibleText.length,
                                            bodyWidth: Math.round(rect.width || 0),
                                            bodyHeight: Math.round(rect.height || 0),
                                            title: document.title || '',
                                            readyState: document.readyState || '',
                                        };
                                    }"""
                                )

                            if not _rendered_page_has_content(metrics):
                                print(
                                    f"[{_AGENT_ID}] Screenshot rejected due to blank/unrendered page: "
                                    f"route={url_path!r} metrics={metrics!r} console_errors={console_errors[-5:]} "
                                    f"page_errors={page_errors[-5:]}"
                                )
                                return False

                            await pg.screenshot(path=out_path, full_page=False)
                        finally:
                            await ctx.close()
                        return os.path.isfile(out_path) and os.path.getsize(out_path) > 0

                    # Desktop
                    d_ok = await _take_page_screenshot(
                        browser,
                        {"width": 1280, "height": 900},
                        desktop_png,
                        url_path=_primary_route,
                    )
                    if d_ok:
                        screenshots.append(desktop_png)
                        print(f"[{_AGENT_ID}] Playwright desktop: "
                              f"{os.path.getsize(desktop_png)} bytes")
                        playwright_ok = True

                    # Mobile
                    m_ok = await _take_page_screenshot(
                        browser,
                        {"width": 375, "height": 812},
                        mobile_png,
                        url_path=_primary_route,
                        is_mobile=True,
                    )
                    if m_ok:
                        screenshots.append(mobile_png)
                        print(f"[{_AGENT_ID}] Playwright mobile: "
                              f"{os.path.getsize(mobile_png)} bytes")

                    await browser.close()

            except ImportError:
                print(f"[{_AGENT_ID}] playwright not available — trying system Chrome")
            except Exception as pw_exc:
                print(f"[{_AGENT_ID}] playwright failed: {pw_exc} — trying system Chrome")

            # --- Chrome fallback (host machine only — not available in all containers) ---
            if not playwright_ok:
                _CHROME_CANDIDATES = [
                    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
                    "/Applications/Chromium.app/Contents/MacOS/Chromium",
                    "google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                    "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
                ]
                chrome_bin = None
                for candidate in _CHROME_CANDIDATES:
                    if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                        chrome_bin = candidate
                        break
                    if "/" not in candidate and shutil.which(candidate):
                        chrome_bin = shutil.which(candidate)
                        break
                print(f"[{_AGENT_ID}] Chrome binary: {chrome_bin!r}")

                if chrome_bin:
                    # Give more time for the production build to fully render.
                    # The vite preview server is faster to render than dev, so 12s
                    # is sufficient for React hydration + Tailwind CSS application.
                    print(f"[{_AGENT_ID}] Waiting 12s for full render (Chrome fallback, "
                          f"{server_type} server)...")
                    _time.sleep(12)

                    _chrome_flags = [
                        "--headless=new", "--no-sandbox", "--disable-gpu",
                        "--disable-dev-shm-usage",
                        "--run-all-compositor-stages-before-draw",
                        # Keep Google Fonts/Material Symbols reachable so UI screenshots
                        # reflect the same icon/font rendering that the real page uses.
                        # Only block unrelated font CDNs that sometimes hang in CI.
                        "--host-rules=MAP use.typekit.net 127.0.0.1",
                    ]
                    for _out_path, _size in [(desktop_png, "1280,900"), (mobile_png, "375,812")]:
                        chrome_result = subprocess.run(
                            [chrome_bin] + _chrome_flags + [
                                f"--screenshot={_out_path}",
                                f"--window-size={_size}",
                                f"http://localhost:{PORT}{_primary_route}",
                            ],
                            capture_output=True, timeout=60,
                        )
                        if os.path.isfile(_out_path) and os.path.getsize(_out_path) > 0:
                            screenshots.append(_out_path)
                            print(f"[{_AGENT_ID}] Chrome screenshot saved ({_size}): "
                                  f"{os.path.getsize(_out_path)} bytes")
                        else:
                            print(f"[{_AGENT_ID}] Chrome screenshot failed "
                                  f"(rc={chrome_result.returncode})")
                else:
                    print(f"[{_AGENT_ID}] No Chrome/Chromium found — falling back to HTML snapshot")

        # --- HTML fallback if no screenshots and server is up ---
        if not screenshots and server_ready:
            try:
                with urllib.request.urlopen(f"http://localhost:{PORT}{_primary_route}", timeout=5) as r:
                    html_bytes = r.read()
                html_fallback = os.path.join(screenshot_dir, f"{_feature_slug}-page.html")
                with open(html_fallback, "wb") as fh:
                    fh.write(html_bytes)
                screenshots.append(html_fallback)
                print(f"[{_AGENT_ID}] HTML fallback saved: {len(html_bytes)} bytes")
            except Exception as exc:
                print(f"[{_AGENT_ID}] HTML fallback also failed: {exc}")

    except Exception as exc:
        print(f"[{_AGENT_ID}] capture_screenshot error (non-fatal): {exc}")
    finally:
        # Always stop the server
        if dev_proc and dev_proc.poll() is None:
            dev_proc.terminate()
            try:
                dev_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                dev_proc.kill()
        subprocess.run(
            ["bash", "-c", f"lsof -ti:{PORT} | xargs kill -9 2>/dev/null || true"],
            timeout=5, capture_output=True,
        )

    captured = bool(screenshots) and any(s.endswith(".png") for s in screenshots)
    log.info("screenshot result", captured=captured, count=len(screenshots),
             server_type=locals().get("server_type", "unknown"))
    if screenshot_required and not captured:
        raise RuntimeError(
            "Required UI screenshot capture failed: no PNG screenshots were produced"
        )
    return {
        "screenshot_captured": captured,
        "screenshots": screenshots,
    }


async def update_jira(state: dict) -> dict:
    """Update Jira after development is complete."""
    log = _logger(state)
    log.node("update_jira")
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

    # --- Assign the ticket to the token owner ---
    task_id = state.get("_task_id", "")
    try:
        user_result = _call_boundary_tool(
            state, "jira_get_token_user", {"task_id": task_id}
        )
        user = user_result.get("user", {})
        account_id = user.get("accountId", "") or user.get("account_id", "")
        if account_id:
            log.info("assigning jira ticket to token user", jira_key=jira_key, account_id=account_id)
            _call_boundary_tool(
                state, "jira_update",
                {
                    "ticket_key": jira_key,
                    "fields": {"assignee": {"accountId": account_id}},
                    "task_id": task_id,
                },
            )
            log.info("jira assignee updated", jira_key=jira_key)
        else:
            log.warn("jira_get_token_user returned no accountId", user=user)
    except Exception as exc:
        log.warn("jira assignee update skipped", error=str(exc))

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

    # Build the comment using inline-markdown syntax.
    # The Jira client converts this to proper ADF (bold, code, hyperlinks)
    # so it renders visually in Jira Cloud — no raw asterisks or brackets shown.
    pr_link = f"[PR: {pr_url}]({pr_url})" if pr_url and pr_url != "N/A" else "N/A"

    comment_text = (
        f"✅ Development completed by web-dev agent.\n"
        f"\n"
        f"**Task ID:** {_task_id}\n"
        f"**PR:** {pr_link}\n"
        f"**Branch:** `{branch}`\n"
        f"**Test results:** {test_summary}\n"
        f"**Self-assessment:** {score_str} ({verdict})\n"
        f"**Files changed:** {len(changes)}"
    )

    # Idempotency: check if comment with PR URL already exists
    log.debug("checking existing comments for idempotency", jira_key=jira_key)
    existing = _call_boundary_tool(
        state, "jira_list_comments", {"ticket_key": jira_key, "task_id": task_id}
    )
    already_commented = False
    for c in existing.get("comments", []):
        body = ""
        if isinstance(c, dict):
            body = c.get("body", "")
            if isinstance(body, dict):
                body = json.dumps(body)
        if pr_url and pr_url != "N/A" and pr_url in str(body):
            already_commented = True
            break

    if not already_commented:
        log.info("adding jira completion comment", jira_key=jira_key, pr_url=pr_url)
        _call_boundary_tool(
            state, "jira_comment",
            {"ticket_key": jira_key, "comment": comment_text, "task_id": task_id},
        )
        log.debug("jira comment added", jira_key=jira_key)
    else:
        log.info("jira comment already exists, skipped")

    # Transition to "In Review"
    log.debug("listing jira transitions for in-review", jira_key=jira_key)
    transitions_result = _call_boundary_tool(
        state, "jira_list_transitions", {"ticket_key": jira_key, "task_id": task_id}
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
            {"ticket_key": jira_key, "transition_name": in_review_match["name"],
             "task_id": task_id},
        )
        log.info("jira transitioned to in review", jira_key=jira_key)
    else:
        log.warn("no in-review transition available", jira_key=jira_key)

    # Write jira-update-log.json to workspace
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        import time as _time
        agent_dir = os.path.join(workspace_path, _AGENT_ID)
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


def _load_pr_description_template() -> str:
    template_path = os.path.join(os.path.dirname(__file__), "templates", "pr_description.md")
    try:
        with open(template_path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


async def create_pr(state: dict) -> dict:
    """Generate a PR description and create the pull request via SCM tools."""
    runtime = state.get("_runtime")
    log = _logger(state)
    log.node("create_pr", branch=state.get("branch_name", ""))

    if not runtime:
        return {
            "pr_url": "",
            "pr_number": 0,
            "pr_title": "Implement changes",
            "commit_hash": "",
        }

    screenshot_required = _is_screenshot_required(state)
    if screenshot_required and not state.get("screenshot_captured"):
        raise RuntimeError("Cannot create PR for a UI task without captured PNG screenshots")

    from agents.web_dev.prompts import PR_DESCRIPTION_SYSTEM, PR_DESCRIPTION_TEMPLATE

    jira_ctx = state.get("jira_context", {})
    jira_key = (
        jira_ctx.get("key") or jira_ctx.get("ticket_key") or ""
        if isinstance(jira_ctx, dict) else ""
    )
    task_id = state.get("_task_id", "")

    # Step 1: Commit any pending files and resolve the full changeset FIRST.
    repo_path = state.get("repo_path", "")
    repo_url = state.get("repo_url", "")
    branch_name = state.get("branch_name", "feature/task")

    committed_files = _git_commit_all_pending(repo_path, jira_key or "task")
    existing_changes = state.get("changes_made", [])
    all_changes = sorted(set(existing_changes) | set(committed_files))

    if not all_changes:
        raise RuntimeError(
            f"[{_AGENT_ID}] create_pr: No file changes detected on branch {branch_name!r}. "
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
            jira_base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
            _jira_url = _jira_ctx.get("url", "") or (f"{jira_base_url}/browse/{jira_key}" if jira_base_url else "")
    pr_template = _load_pr_description_template()
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
        pr_description_template=pr_template or "Use the standard Constellation PR sections.",
    )
    desc_result = runtime.run(desc_prompt, system_prompt=PR_DESCRIPTION_SYSTEM,
                              plugin_manager=state.get("_plugin_manager"))
    pr_meta = _safe_json(desc_result.get("raw_response", ""), fallback={})
    pr_title = pr_meta.get("title", "Implement task changes")
    pr_description = pr_meta.get("description", state.get("implementation_summary", ""))

    # Step 2.5: Prepare screenshot artifacts. Screenshots must be hosted outside
    # the PR branch and injected into the PR description only after PR creation.
    _screenshots = state.get("screenshots", [])
    _png_screenshots = [s for s in _screenshots if s.endswith(".png") and os.path.isfile(s)]
    _screenshot_uploaded = False
    _screenshot_section = ""
    if screenshot_required and not _png_screenshots:
        raise RuntimeError("Cannot create PR for a UI task because PNG screenshots are missing")

    pr_description = pr_description.rstrip()

    # Step 3: Push branch then create PR via SCM boundary tools (not open agentic).
    task_id = state.get("_task_id", "")

    log.info("pushing branch to remote", branch=branch_name)
    push_payload = _call_boundary_tool(
        state, "scm_push", {"repo_path": repo_path, "branch": branch_name, "task_id": task_id}
    )
    if push_payload.get("error"):
        log.error("scm_push failed", error=push_payload["error"])
        print(f"[{_AGENT_ID}] scm_push failed: {push_payload['error']}")
    else:
        log.debug("scm_push ok", branch=branch_name)
        print(f"[{_AGENT_ID}] scm_push OK: branch={branch_name!r}")

    log.info("creating PR", source_branch=branch_name, target="main", title=pr_title[:80])
    pr_payload = _call_boundary_tool(
        state, "scm_create_pr",
        {
            "repo_url": repo_url,
            "source_branch": branch_name,
            "target_branch": "main",
            "title": pr_title,
            "description": pr_description,
            "task_id": task_id,
        },
    )
    pr_url = pr_payload.get("prUrl") or pr_payload.get("pr_url", "")
    pr_number = pr_payload.get("prNumber") or pr_payload.get("pr_number", 0)
    if not pr_number and isinstance(pr_payload.get("pr"), dict):
        pr_number = pr_payload["pr"].get("number") or pr_payload["pr"].get("id") or 0
    if not pr_number and pr_url and "/pull/" in pr_url:
        try:
            pr_number = int(pr_url.rstrip("/").rsplit("/pull/", 1)[1])
        except (TypeError, ValueError):
            pr_number = 0
    commit_hash = pr_payload.get("commitHash") or pr_payload.get("commit_hash", "")
    pr_status = pr_payload.get("status", "")
    pr_error = pr_payload.get("error", "")
    if not pr_url and (pr_error or (pr_status and pr_status != "ok")):
        log.error("PR creation failed", status=pr_status, error=pr_error)
        print(f"[{_AGENT_ID}] create_pr FAILED: status={pr_status!r} error={pr_error!r} payload={pr_payload}")
    else:
        log.info("PR created", pr_url=pr_url, branch=branch_name)
        print(f"[{_AGENT_ID}] create_pr done: prUrl={pr_url!r} prNumber={pr_number} status={pr_status!r}")

    from framework.validation_gates import validate_pr_created, validate_screenshot_upload
    pr_gate = validate_pr_created(pr_url, int(pr_number or 0) if pr_number else None)
    if not pr_gate.passed:
        raise RuntimeError(f"PR creation gate failed: {pr_gate.feedback}")

    # Step 4: Upload screenshots to GitHub CDN and PATCH the PR description.
    _first_screenshot_url = ""
    if _png_screenshots:
        log.info("uploading screenshots to CDN", screenshots=len(_png_screenshots), pr_number=pr_number)
        _screenshot_entries: list[tuple[str, str]] = []
        for _png in _png_screenshots:
            _fname = os.path.basename(_png)
            _label = "Desktop (1280×900)" if "desktop" in _png.lower() else "Mobile (375×812)"
            _upload_result = _call_boundary_tool(
                state,
                "scm_upload_pr_image",
                {
                    "repo_url": repo_url,
                    "pr_number": int(pr_number or 0),
                    "image_path": _png,
                    "task_id": task_id,
                },
            )
            _cdn_url = _upload_result.get("image_url", "")
            if _cdn_url:
                _screenshot_entries.append((_label, _cdn_url))
                _first_screenshot_url = _first_screenshot_url or _cdn_url
                continue

            log.error("screenshot upload failed", screenshot=_fname, error=_upload_result.get("error", ""))
            print(f"[{_AGENT_ID}] CDN upload failed for {_fname}: "
                  f"{_upload_result.get('error', '(no error detail)')}")

        if _screenshot_entries:
            _section_parts = [
                f"**{_lbl}**\n\n![]({_url})" for _lbl, _url in _screenshot_entries
            ]
            _screenshot_section = "\n\n## Screenshots\n\n" + "\n\n".join(_section_parts)
            updated_description = pr_description + _screenshot_section
            update_payload = _call_boundary_tool(
                state,
                "scm_update_pr",
                {
                    "repo_url": repo_url,
                    "pr_number": int(pr_number or 0),
                    "description": updated_description,
                    "title": pr_title,
                    "task_id": task_id,
                },
            )
            if update_payload.get("error") or update_payload.get("status") not in ("ok", "no_changes", ""):
                log.error("scm_update_pr failed", error=update_payload.get("error", ""), status=update_payload.get("status", ""))
                raise RuntimeError("Cannot finalize PR for a UI task because screenshot URLs could not be added to the PR description")

            pr_description = updated_description
            _screenshot_uploaded = True
            log.info("screenshot PR description updated", screenshots=len(_screenshot_entries), pr_number=pr_number)
            print(f"[{_AGENT_ID}] Screenshots uploaded to GitHub CDN — "
                  f"{len(_screenshot_entries)} image(s) embedded in PR description")
        elif screenshot_required:
            raise RuntimeError("Cannot finalize PR for a UI task because screenshot upload did not return CDN URLs")

        print(f"[{_AGENT_ID}] {len(_png_screenshots)} screenshot(s) processed for PR description")

    screenshot_gate = validate_screenshot_upload(
        screenshot_required=screenshot_required,
        screenshot_uploaded=_screenshot_uploaded,
        screenshot_url=_first_screenshot_url,
    )
    if not screenshot_gate.passed:
        raise RuntimeError(f"Screenshot upload gate failed: {screenshot_gate.feedback}")

    # Write pr-evidence.json to workspace
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        import time as _time
        agent_dir = os.path.join(workspace_path, _AGENT_ID)
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
                        "screenshot_uploaded": _screenshot_uploaded,
                        "screenshots": _screenshots,
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
        "screenshot_uploaded": _screenshot_uploaded,
    }


async def report_result(state: dict) -> dict:
    """Return final result summary."""
    log = _logger(state)
    log.node("report_result")
    pr_url = state.get("pr_url", "N/A")
    branch_name = state.get("branch_name", "N/A")
    changes = state.get("changes_made", [])
    pr_title = state.get("pr_title", "")
    test_status = state.get("test_status", "unknown")
    log.info("report_result", pr_url=pr_url, branch=branch_name,
             test_status=test_status, files_changed=len(changes))
    print(f"[{_AGENT_ID}] report_result: prUrl={pr_url!r} branch={branch_name!r} test_status={test_status!r} changes={len(changes)}")

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
        "pr_number": state.get("pr_number", 0),
        "branch_name": branch_name,
        "pr_title": pr_title,
        "pr_description": state.get("pr_description", ""),
        "changes_made": changes,
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

