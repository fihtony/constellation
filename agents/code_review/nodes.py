"""Code Review Agent workflow nodes.

Design pattern — "Graph outside, ReAct inside":
- Graph drives the deterministic review pipeline (load → quality → security → tests → requirements → report).
- Each review phase uses a single-shot LLM call (runtime.run()) — bounded and auditable.
- Nodes degrade gracefully when no runtime is available (unit-test path).
- Checkpoints are saved after load and after the final report for crash recovery.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path as _Path
from typing import Any

from framework.config import load_agent_config as _load_agent_cfg
from framework.devlog import AgentLogger

# Load own agent_id from config.yaml — single source of truth
_AGENT_ID: str = _load_agent_cfg(
    _Path(__file__).parent.name.replace("_", "-")
).get("agent_id", _Path(__file__).parent.name.replace("_", "-"))

# Cross-agent workspace dir reference — loaded from the corresponding config
_TEAM_LEAD_AGENT_ID: str = _load_agent_cfg("team-lead").get("agent_id", "team-lead")

# Cross-agent workspace dir reference — loaded from the corresponding config
_WEB_DEV_AGENT_ID: str = _load_agent_cfg("web-dev").get("agent_id", "web-dev")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_issue_list(text: str) -> list[dict]:
    """Extract a JSON array of issue objects from LLM response text.

    Returns an empty list when parsing fails.
    """
    # Try direct parse first
    try:
        parsed = json.loads(text.strip())
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass

    # Extract the first JSON array from mixed text
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                return parsed
        except json.JSONDecodeError:
            pass

    return []


def _logger(state: dict) -> AgentLogger:
    return AgentLogger(task_id=state.get("_task_id", ""), agent_name=_AGENT_ID)


def _unwrap_artifact_payload(payload: Any) -> Any:
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        return payload["data"]
    return payload


def _load_json_file(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return _unwrap_artifact_payload(json.load(fh))


def _load_text_file(path: str, max_chars: int = 8000) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read(max_chars)


def _record_checked_artifact(checked_artifacts: list[str], workspace_path: str, path: str) -> None:
    if not workspace_path or not path or not os.path.isfile(path):
        return
    relative = os.path.relpath(path, workspace_path).replace(os.sep, "/")
    if relative not in checked_artifacts:
        checked_artifacts.append(relative)


def _review_input_wait_seconds() -> float:
    raw = os.environ.get("CODE_REVIEW_INPUT_WAIT_SECONDS", "300")
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return 300.0


def _review_input_poll_seconds() -> float:
    raw = os.environ.get("CODE_REVIEW_INPUT_POLL_SECONDS", "2")
    try:
        return max(float(raw), 0.1)
    except ValueError:
        return 2.0


def _review_input_attempts() -> int:
    wait_seconds = _review_input_wait_seconds()
    poll_seconds = _review_input_poll_seconds()
    if wait_seconds <= 0:
        return 1
    return max(1, int(wait_seconds / poll_seconds) + 1)


def _child_permissions(state: dict) -> dict[str, Any] | None:
    metadata = state.get("metadata", {})
    permissions = metadata.get("permissions")
    return permissions if isinstance(permissions, dict) else None


def _find_latest_self_assessment(agent_dir: str) -> str:
    if not os.path.isdir(agent_dir):
        return ""
    candidates = []
    for name in os.listdir(agent_dir):
        if name == "self-assessment.json" and os.path.isfile(os.path.join(agent_dir, name)):
            candidates.append((10_000, os.path.join(agent_dir, name)))
            continue
        match = re.fullmatch(r"self-assessment-(\d+)\.json", name)
        if match and os.path.isfile(os.path.join(agent_dir, name)):
            candidates.append((int(match.group(1)), os.path.join(agent_dir, name)))
    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _find_jira_ticket_path(workspace_path: str, jira_context: dict[str, Any]) -> str:
    jira_key = str((jira_context or {}).get("key", "")).strip()
    if jira_key:
        candidate = os.path.join(workspace_path, "jira", jira_key, "ticket.json")
        if os.path.isfile(candidate):
            return candidate
    jira_root = os.path.join(workspace_path, "jira")
    if not os.path.isdir(jira_root):
        return ""
    for entry in sorted(os.listdir(jira_root)):
        candidate = os.path.join(jira_root, entry, "ticket.json")
        if os.path.isfile(candidate):
            return candidate
    return ""


def _parse_pr_number(pr_url: str, pr_number: Any) -> int:
    try:
        number = int(pr_number or 0)
    except (TypeError, ValueError):
        number = 0
    if number:
        return number
    if pr_url and "/pull/" in pr_url:
        try:
            return int(pr_url.rstrip("/").rsplit("/pull/", 1)[1])
        except (TypeError, ValueError, IndexError):
            return 0
    return 0


# ---------------------------------------------------------------------------
# Node implementations
# ---------------------------------------------------------------------------

async def load_pr_context(state: dict) -> dict:
    """Load PR diff, changed files, description, and Jira/design context.

    In a full deployment the SCM adapter fetches PR data.
    For MVP the calling agent (Team Lead) passes it via metadata.
    Also loads Jira and design context for requirements-aware review.
    """
    metadata = state.get("metadata", {})
    log = _logger(state)
    log.node("load_pr_context")

    # PR context
    pr_diff = metadata.get("prDiff") or state.get("pr_diff") or ""
    changed_files = metadata.get("changedFiles") or state.get("changed_files") or []
    pr_description = metadata.get("prDescription") or state.get("pr_description") or ""
    commit_messages = metadata.get("commitMessages") or state.get("commit_messages") or []
    checked_artifacts: list[str] = []

    # If PR diff not provided, try to fetch via scm_get_pr_diff tool
    pr_url = metadata.get("prUrl") or state.get("pr_url") or ""
    repo_url = metadata.get("repoUrl") or state.get("repo_url") or ""
    pr_number = metadata.get("prNumber") or state.get("pr_number") or 0

    # Jira and design context (passed by Team Lead)
    jira_context = metadata.get("jiraContext") or state.get("jira_context") or {}
    design_context = metadata.get("designContext") or state.get("design_context") or {}
    workspace_path = metadata.get("workspacePath") or state.get("workspace_path") or ""
    context_manifest_path = (
        metadata.get("contextManifestPath")
        or state.get("context_manifest_path")
        or ""
    )
    permissions = _child_permissions(state)

    if workspace_path:
        if context_manifest_path:
            manifest_file = context_manifest_path
            if not os.path.isabs(manifest_file):
                manifest_file = os.path.join(workspace_path, manifest_file)
            _record_checked_artifact(checked_artifacts, workspace_path, manifest_file)

        jira_ticket_path = _find_jira_ticket_path(workspace_path, jira_context)
        if jira_ticket_path:
            try:
                jira_context = _load_json_file(jira_ticket_path) or jira_context
                _record_checked_artifact(checked_artifacts, workspace_path, jira_ticket_path)
            except Exception as exc:
                log.warn("failed to load jira ticket artifact", error=str(exc), path=jira_ticket_path)

        ui_design_root = os.path.join(workspace_path, "ui-design")
        design_md_path = os.path.join(ui_design_root, "stitch", "DESIGN.md")
        design_html_path = os.path.join(ui_design_root, "stitch", "code.html")
        design_meta_path = os.path.join(ui_design_root, "stitch", "screen-meta.json")
        if os.path.isfile(design_md_path):
            try:
                design_context = dict(design_context or {})
                design_context["spec_markdown"] = _load_text_file(design_md_path)
                _record_checked_artifact(checked_artifacts, workspace_path, design_md_path)
            except Exception as exc:
                log.warn("failed to load design markdown", error=str(exc), path=design_md_path)
        if os.path.isfile(design_html_path):
            try:
                design_context = dict(design_context or {})
                design_context["design_html"] = _load_text_file(design_html_path)
                _record_checked_artifact(checked_artifacts, workspace_path, design_html_path)
            except Exception as exc:
                log.warn("failed to load design html", error=str(exc), path=design_html_path)
        if os.path.isfile(design_meta_path):
            try:
                design_context = dict(design_context or {})
                design_context["screen_meta"] = _load_json_file(design_meta_path)
                _record_checked_artifact(checked_artifacts, workspace_path, design_meta_path)
            except Exception as exc:
                log.warn("failed to load design metadata", error=str(exc), path=design_meta_path)

        web_dev_dir = os.path.join(workspace_path, _WEB_DEV_AGENT_ID)
        pr_evidence_path = os.path.join(web_dev_dir, "pr-evidence.json")
        if os.path.isfile(pr_evidence_path):
            try:
                pr_evidence = _load_json_file(pr_evidence_path) or {}
                _record_checked_artifact(checked_artifacts, workspace_path, pr_evidence_path)
                pr_url = pr_url or str(pr_evidence.get("pr_url", ""))
                pr_number = pr_number or pr_evidence.get("pr_number") or 0
                changed_files = changed_files or list(pr_evidence.get("changed_files", []) or [])
            except Exception as exc:
                log.warn("failed to load PR evidence", error=str(exc), path=pr_evidence_path)

        self_assessment_path = _find_latest_self_assessment(web_dev_dir)
        if self_assessment_path:
            _record_checked_artifact(checked_artifacts, workspace_path, self_assessment_path)

        if not jira_context:
            team_lead_ticket_path = os.path.join(workspace_path, _TEAM_LEAD_AGENT_ID, "jira-ticket.json")
            if os.path.isfile(team_lead_ticket_path):
                try:
                    jira_context = _load_json_file(team_lead_ticket_path) or jira_context
                    _record_checked_artifact(checked_artifacts, workspace_path, team_lead_ticket_path)
                except Exception as exc:
                    log.warn("failed to load team-lead jira ticket", error=str(exc), path=team_lead_ticket_path)
        if not design_context:
            team_lead_design_path = os.path.join(workspace_path, _TEAM_LEAD_AGENT_ID, "design-spec.json")
            if os.path.isfile(team_lead_design_path):
                try:
                    design_context = _load_json_file(team_lead_design_path) or design_context
                    _record_checked_artifact(checked_artifacts, workspace_path, team_lead_design_path)
                except Exception as exc:
                    log.warn("failed to load team-lead design spec", error=str(exc), path=team_lead_design_path)

    pr_number = _parse_pr_number(pr_url, pr_number)

    if repo_url and pr_number and (not pr_description or not commit_messages):
        try:
            from framework.tools.registry import get_registry
            registry = get_registry()
            info_args = {
                "repo_url": repo_url,
                "pr_number": int(pr_number),
                "task_id": state.get("_task_id", ""),
            }
            if permissions:
                info_args["permissions"] = permissions
            info_result_str = registry.execute_sync("scm_get_pr_info", info_args)
            info_payload = json.loads(info_result_str) if info_result_str else {}
            if info_payload.get("error") == "Tool 'scm_get_pr_info' is not registered":
                from agents.code_review.tools import fetch_pr_info

                info_payload = fetch_pr_info(
                    repo_url,
                    int(pr_number),
                    task_id=state.get("_task_id", ""),
                    permissions=permissions if isinstance(permissions, dict) else None,
                )
            if not info_payload.get("error"):
                pr_description = pr_description or info_payload.get("description", "")
                commit_messages = commit_messages or [
                    commit.get("message", "") for commit in info_payload.get("commits", []) if commit.get("message")
                ]
        except Exception as exc:
            log.warn("scm_get_pr_info fallback failed", error=str(exc))

    review_attempts = _review_input_attempts()
    poll_seconds = _review_input_poll_seconds()
    last_diff_error = ""
    review_input_wait_logged = False
    web_dev_dir = os.path.join(workspace_path, _WEB_DEV_AGENT_ID) if workspace_path else ""

    for attempt in range(review_attempts):
        if web_dev_dir and not os.path.isfile(pr_evidence_path if workspace_path else ""):
            current_pr_evidence_path = os.path.join(web_dev_dir, "pr-evidence.json")
            if os.path.isfile(current_pr_evidence_path):
                try:
                    pr_evidence = _load_json_file(current_pr_evidence_path) or {}
                    _record_checked_artifact(checked_artifacts, workspace_path, current_pr_evidence_path)
                    pr_url = pr_url or str(pr_evidence.get("pr_url", ""))
                    pr_number = pr_number or pr_evidence.get("pr_number") or 0
                    changed_files = changed_files or list(pr_evidence.get("changed_files", []) or [])
                    pr_evidence_path = current_pr_evidence_path
                except Exception as exc:
                    log.warn("failed to load PR evidence", error=str(exc), path=current_pr_evidence_path)

        if web_dev_dir:
            current_self_assessment_path = _find_latest_self_assessment(web_dev_dir)
            if current_self_assessment_path:
                _record_checked_artifact(checked_artifacts, workspace_path, current_self_assessment_path)

        pr_number = _parse_pr_number(pr_url, pr_number)
        if pr_diff or not (pr_url and repo_url and pr_number):
            break

        try:
            from framework.tools.registry import get_registry
            registry = get_registry()
            diff_args = {
                "repo_url": repo_url,
                "pr_number": int(pr_number),
                "task_id": state.get("_task_id", ""),
            }
            if permissions:
                diff_args["permissions"] = permissions
            diff_result_str = registry.execute_sync("scm_get_pr_diff", diff_args)
            diff_payload = json.loads(diff_result_str) if diff_result_str else {}
            if diff_payload.get("error") == "Tool 'scm_get_pr_diff' is not registered":
                from agents.code_review.tools import fetch_pr_diff

                diff_payload = fetch_pr_diff(
                    repo_url,
                    int(pr_number),
                    task_id=state.get("_task_id", ""),
                    permissions=permissions if isinstance(permissions, dict) else None,
                )
            if not diff_payload.get("error"):
                pr_diff = diff_payload.get("diff_text", "")
                changed_files = changed_files or [
                    f.get("filename", "") for f in diff_payload.get("changed_files", [])
                ]
                log.info("fetched PR diff", pr_number=pr_number, diff_chars=len(pr_diff), files=len(changed_files))
                print(f"[{_AGENT_ID}] Fetched PR diff via scm_get_pr_diff: {len(pr_diff)} chars")
                break
            last_diff_error = str(diff_payload.get("error") or diff_payload.get("detail") or "")
        except Exception as exc:
            last_diff_error = str(exc)

        if attempt < review_attempts - 1:
            if not review_input_wait_logged:
                log.info(
                    "waiting for review inputs",
                    pr_url=pr_url,
                    pr_number=pr_number,
                    attempts=review_attempts,
                    poll_seconds=poll_seconds,
                )
                review_input_wait_logged = True
            time.sleep(poll_seconds)

    if last_diff_error and not pr_diff:
        log.warn("scm_get_pr_diff unavailable", error=last_diff_error, pr_number=pr_number)

    review_input_issues: list[dict[str, Any]] = []
    if pr_url and not pr_diff:
        review_input_issues.append({
            "severity": "high",
            "category": "review-input",
            "message": "Unable to load the PR diff, so code review could not validate the submitted code changes.",
            "suggestion": "Ensure code-review receives repoUrl/prNumber metadata and can access web-dev/pr-evidence.json before review dispatch.",
        })
        log.warn("review input incomplete", pr_url=pr_url, repo_url=repo_url, pr_number=pr_number)

    # Extract original requirements from Jira context
    original_requirements = state.get("original_requirements", "")
    if not original_requirements and jira_context:
        fields = jira_context.get("fields", jira_context)
        criteria = fields.get("acceptanceCriteria", [])
        desc = fields.get("description", "")
        if criteria:
            original_requirements = "\n".join(f"- {c}" for c in criteria)
        elif desc:
            original_requirements = desc

    # Write review start log
    if workspace_path:
        review_dir = os.path.join(workspace_path, "code-review")
        checkpoints_dir = os.path.join(review_dir, "review-checkpoints")
        os.makedirs(review_dir, exist_ok=True)
        os.makedirs(checkpoints_dir, exist_ok=True)
        try:
            log_file = os.path.join(review_dir, "task-log.json")
            with open(log_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "code-review",
                        "step": "load_pr_context",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": {
                        "pr_url": metadata.get("prUrl", ""),
                        "changed_files_count": len(changed_files) if isinstance(changed_files, list) else 0,
                        "has_jira_context": bool(jira_context),
                        "has_design_context": bool(design_context),
                        "checked_artifacts": checked_artifacts,
                    },
                }, fh, ensure_ascii=False, indent=2)

            checkpoint_file = os.path.join(checkpoints_dir, "review-start.json")
            with open(checkpoint_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "checkpoint_id": "CP_REVIEW_STARTED",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "agent_id": "code-review",
                    "state": {
                        "pr_url": metadata.get("prUrl", ""),
                        "workspace_path": workspace_path,
                        "context_manifest_path": context_manifest_path,
                        "has_jira_context": bool(jira_context),
                        "has_design_context": bool(design_context),
                        "checked_artifacts": checked_artifacts,
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    log.info(
        "PR context loaded",
        pr_number=pr_number,
        changed_files=len(changed_files) if isinstance(changed_files, list) else 0,
        checked_artifacts=len(checked_artifacts),
        has_diff=bool(pr_diff),
    )

    return {
        "pr_diff": pr_diff,
        "changed_files": changed_files if isinstance(changed_files, list) else [changed_files],
        "pr_description": pr_description,
        "commit_messages": commit_messages,
        "jira_context": jira_context,
        "design_context": design_context,
        "original_requirements": original_requirements,
        "workspace_path": workspace_path,
        "context_manifest_path": context_manifest_path,
        "repo_url": repo_url,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "checked_artifacts": checked_artifacts,
        "review_input_issues": review_input_issues,
    }


async def review_quality(state: dict) -> dict:
    """Check code quality, style, and patterns using a single-shot LLM call."""
    runtime = state.get("_runtime")
    log = _logger(state)
    log.node("review_quality")

    if not runtime or not state.get("pr_diff"):
        log.info("skipping quality review", has_runtime=bool(runtime), has_diff=bool(state.get("pr_diff")))
        return {"quality_issues": []}

    from agents.code_review.prompts import QUALITY_SYSTEM, QUALITY_TEMPLATE

    prompt = QUALITY_TEMPLATE.format(
        pr_description=state.get("pr_description", "N/A"),
        changed_files=", ".join(state.get("changed_files", [])) or "N/A",
        pr_diff=state.get("pr_diff", ""),
    )
    result = runtime.run(prompt, system_prompt=QUALITY_SYSTEM, max_tokens=2048,
                         plugin_manager=state.get("_plugin_manager"))
    issues = _parse_issue_list(result.get("raw_response", ""))
    log.info("quality review complete", issues=len(issues))

    return {"quality_issues": issues}


async def review_security(state: dict) -> dict:
    """Check for security vulnerabilities (OWASP Top 10) using a single-shot LLM call."""
    runtime = state.get("_runtime")
    log = _logger(state)
    log.node("review_security")

    if not runtime or not state.get("pr_diff"):
        log.info("skipping security review", has_runtime=bool(runtime), has_diff=bool(state.get("pr_diff")))
        return {"security_issues": []}

    from agents.code_review.prompts import SECURITY_SYSTEM, SECURITY_TEMPLATE

    prompt = SECURITY_TEMPLATE.format(
        pr_description=state.get("pr_description", "N/A"),
        changed_files=", ".join(state.get("changed_files", [])) or "N/A",
        pr_diff=state.get("pr_diff", ""),
    )
    result = runtime.run(prompt, system_prompt=SECURITY_SYSTEM, max_tokens=2048,
                         plugin_manager=state.get("_plugin_manager"))
    issues = _parse_issue_list(result.get("raw_response", ""))
    log.info("security review complete", issues=len(issues))

    return {"security_issues": issues}


async def review_tests(state: dict) -> dict:
    """Check test coverage and test quality using a single-shot LLM call."""
    runtime = state.get("_runtime")
    log = _logger(state)
    log.node("review_tests")

    if not runtime or not state.get("pr_diff"):
        log.info("skipping test review", has_runtime=bool(runtime), has_diff=bool(state.get("pr_diff")))
        return {"test_issues": []}

    from agents.code_review.prompts import TESTS_SYSTEM, TESTS_TEMPLATE

    prompt = TESTS_TEMPLATE.format(
        pr_description=state.get("pr_description", "N/A"),
        changed_files=", ".join(state.get("changed_files", [])) or "N/A",
        pr_diff=state.get("pr_diff", ""),
    )
    result = runtime.run(prompt, system_prompt=TESTS_SYSTEM, max_tokens=2048,
                         plugin_manager=state.get("_plugin_manager"))
    issues = _parse_issue_list(result.get("raw_response", ""))
    log.info("test review complete", issues=len(issues))

    return {"test_issues": issues}


async def review_requirements(state: dict) -> dict:
    """Check requirements compliance against Jira acceptance criteria."""
    runtime = state.get("_runtime")
    log = _logger(state)
    log.node("review_requirements")

    if not runtime or not state.get("pr_diff"):
        log.info("skipping requirements review", has_runtime=bool(runtime), has_diff=bool(state.get("pr_diff")))
        return {"requirement_gaps": []}

    original_requirements = state.get("original_requirements", "")
    if not original_requirements:
        log.info("skipping requirements review", reason="missing original requirements")
        return {"requirement_gaps": []}

    from agents.code_review.prompts import REQUIREMENTS_SYSTEM, REQUIREMENTS_TEMPLATE

    jira_ctx = state.get("jira_context", {})
    prompt = REQUIREMENTS_TEMPLATE.format(
        original_requirements=original_requirements,
        jira_context=json.dumps(jira_ctx, ensure_ascii=False) if jira_ctx else "N/A",
        pr_description=state.get("pr_description", "N/A"),
        changed_files=", ".join(state.get("changed_files", [])) or "N/A",
        pr_diff=state.get("pr_diff", ""),
    )
    result = runtime.run(prompt, system_prompt=REQUIREMENTS_SYSTEM, max_tokens=2048,
                         plugin_manager=state.get("_plugin_manager"))
    issues = _parse_issue_list(result.get("raw_response", ""))
    log.info("requirements review complete", issues=len(issues))

    return {"requirement_gaps": issues}


async def review_ui_design(state: dict) -> dict:
    """Check UI implementation for design fidelity: icons, typography, colors, layout, spacing.

    This phase is triggered for any task that modifies UI source files (tsx, jsx, css, scss,
    html) or whose PR description mentions UI/design/screen/Stitch.  It is a no-op when
    neither condition is true, so it is safe to run for every review.
    """
    runtime = state.get("_runtime")
    log = _logger(state)
    log.node("review_ui_design")

    if not runtime or not state.get("pr_diff"):
        log.info("skipping UI review", has_runtime=bool(runtime), has_diff=bool(state.get("pr_diff")))
        return {"ui_issues": []}

    # Determine whether this is a UI task — check file extensions and PR description
    changed_files = state.get("changed_files", [])
    pr_description = state.get("pr_description", "")
    _ui_extensions = {".tsx", ".jsx", ".css", ".scss", ".sass", ".html", ".vue", ".svelte"}
    _ui_keywords = ("ui", "design", "screen", "stitch", "figma", "page", "component",
                    "layout", "style", "frontend", "landing")

    is_ui_task = any(
        any(f.lower().endswith(ext) for ext in _ui_extensions)
        for f in changed_files
    ) or any(kw in pr_description.lower() for kw in _ui_keywords)

    if not is_ui_task:
        log.info("skipping UI review", reason="non-UI task")
        return {"ui_issues": []}

    from agents.code_review.prompts import UI_DESIGN_SYSTEM, UI_DESIGN_TEMPLATE

    # Extract design context if available
    design_context = state.get("design_context", {})
    design_spec = ""
    design_html = ""
    if isinstance(design_context, dict):
        design_spec = design_context.get("spec_markdown", "") or design_context.get("design_spec", "")
        design_html = design_context.get("code_reference", "") or design_context.get("design_html", "")

    # If not in state, try loading from workspace
    if not design_spec and state.get("workspace_path"):
        import os as _os
        tl_dir = _os.path.join(state["workspace_path"], _TEAM_LEAD_AGENT_ID)
        for candidate in ("design-spec.md", "design-code.html", "design-code.xml"):
            path = _os.path.join(tl_dir, candidate)
            if _os.path.isfile(path):
                try:
                    with open(path, encoding="utf-8") as fh:
                        content = fh.read()
                    if candidate.endswith(".md"):
                        design_spec = content[:8000]
                    else:
                        design_html = content[:8000]
                except OSError:
                    pass

    if not design_spec and not design_html:
        # No design reference available — skip UI review silently
        return {"ui_issues": []}

    prompt = UI_DESIGN_TEMPLATE.format(
        pr_description=pr_description or "N/A",
        changed_files=", ".join(changed_files) or "N/A",
        design_spec=design_spec[:4000] if design_spec else "N/A",
        design_html=design_html[:4000] if design_html else "N/A",
        pr_diff=state.get("pr_diff", ""),
    )
    result = runtime.run(prompt, system_prompt=UI_DESIGN_SYSTEM, max_tokens=2048,
                         plugin_manager=state.get("_plugin_manager"))
    issues = _parse_issue_list(result.get("raw_response", ""))
    log.info("UI review complete", issues=len(issues), has_design_spec=bool(design_spec), has_design_html=bool(design_html))

    return {"ui_issues": issues}


async def generate_report(state: dict) -> dict:
    """Aggregate all review phases and produce a final verdict.

    Pure Python — no LLM call needed.
    Verdict: "approved" only when there are zero critical or high severity issues.
    Writes review-report.json to the workspace for audit.
    """
    quality = state.get("quality_issues", [])
    security = state.get("security_issues", [])
    tests = state.get("test_issues", [])
    requirements = state.get("requirement_gaps", [])
    ui_issues = state.get("ui_issues", [])
    review_input_issues = state.get("review_input_issues", [])
    log = _logger(state)
    log.node("generate_report")

    all_comments = review_input_issues + quality + security + tests + requirements + ui_issues

    # Count by severity
    critical = sum(1 for c in all_comments if c.get("severity") == "critical")
    high = sum(1 for c in all_comments if c.get("severity") == "high")
    medium = sum(1 for c in all_comments if c.get("severity") == "medium")
    low = sum(1 for c in all_comments if c.get("severity") == "low")

    verdict = "approved" if (critical == 0 and high == 0) else "rejected"

    # Validation gate: verify review report structure
    from framework.validation_gates import validate_review_verdict
    gate_result = validate_review_verdict({
        "verdict": verdict,
        "issues": all_comments,
        "summary": f"Review complete: {len(all_comments)} issue(s) found.",
        "severity_levels": {"critical": critical, "high": high, "medium": medium, "low": low},
    })
    if not gate_result.passed:
        # Gate enforcement: if critical issues found but verdict was wrongly set, override
        if "Critical issues" in gate_result.feedback:
            verdict = "rejected"
            print(f"[{_AGENT_ID}] validate_review_verdict: overriding verdict to 'rejected'")
        else:
            print(f"[{_AGENT_ID}] validate_review_verdict gate warning: {gate_result.feedback}")

    summary_parts = [
        f"Review complete: {len(all_comments)} issue(s) found ({len(ui_issues)} UI design).",
        f"Critical: {critical}, High: {high}, Medium: {medium}, Low: {low}.",
        f"Verdict: {verdict}.",
    ]

    report = {
        "verdict": verdict,
        "all_comments": all_comments,
        "severity_levels": {
            "critical": critical,
            "high": high,
            "medium": medium,
            "low": low,
        },
        "checked_artifacts": list(state.get("checked_artifacts", [])),
    }

    # Write review report to workspace
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        review_dir = os.path.join(workspace_path, "code-review")
        checkpoints_dir = os.path.join(review_dir, "review-checkpoints")
        os.makedirs(review_dir, exist_ok=True)
        os.makedirs(checkpoints_dir, exist_ok=True)
        try:
            report_file = os.path.join(review_dir, "review-report.json")
            with open(report_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "code-review",
                        "step": "generate_report",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": report,
                }, fh, ensure_ascii=False, indent=2)

            checkpoint_file = os.path.join(checkpoints_dir, "review-summary.json")
            with open(checkpoint_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "checkpoint_id": "CP_REVIEW_SUMMARIZED",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    "agent_id": "code-review",
                    "state": {
                        "verdict": verdict,
                        "severity_levels": report["severity_levels"],
                        "checked_artifacts": report["checked_artifacts"],
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass

    log.info("review report generated", verdict=verdict, issues=len(all_comments), checked_artifacts=len(report["checked_artifacts"]))

    return {
        "verdict": verdict,
        "all_comments": all_comments,
        "report_summary": " ".join(summary_parts),
        "severity_levels": report["severity_levels"],
        "checked_artifacts": report["checked_artifacts"],
    }

