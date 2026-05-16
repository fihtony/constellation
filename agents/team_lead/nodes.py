"""Team Lead Agent workflow nodes.

Architecture: **Graph outside, ReAct inside**.

Each node is an async function that receives the workflow state dict and returns
a dict of state updates.  Nodes that need open-ended reasoning use the runtime
for single-shot LLM calls or bounded ReAct; the graph controls the macro flow.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

from framework.devlog import WorkspaceLogger


def _logger(state: dict, agent_id: str = "team-lead") -> WorkspaceLogger:
    """Return a WorkspaceLogger for the given agent_id using state workspace_path."""
    return WorkspaceLogger(state.get("workspace_path", ""), agent_id)


def _boundary_log(state: dict, boundary_agent: str, message: str, **kwargs: Any) -> None:
    """Write a log entry to a boundary agent's workspace folder (proxy logging).

    This ensures agents like jira, scm, and ui-design have workspace folders
    and log files even though they are called as in-process tools rather than
    standalone services.
    """
    WorkspaceLogger(state.get("workspace_path", ""), boundary_agent).info(message, **kwargs)


async def receive_task(state: dict) -> dict:
    """Parse and validate the incoming task request."""
    import re as _re
    user_request = state.get("user_request", "")

    jira_key = state.get("jira_key", "")
    jira_ticket_url = state.get("jira_ticket_url", "")

    # If jira_key not in metadata, extract it from the user_request text.
    if not jira_key:
        url_match = _re.search(
            r"(https?://[^\s]+/browse/([A-Z][A-Z0-9]+-\d+))", user_request
        )
        if url_match:
            jira_ticket_url = jira_ticket_url or url_match.group(1)
            jira_key = url_match.group(2)
            print(f"[team-lead] Extracted jira_key={jira_key} from URL in user_request")
        else:
            key_match = _re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", user_request)
            if key_match:
                jira_key = key_match.group(1)
                print(f"[team-lead] Extracted jira_key={jira_key} from user_request text")

    # Initialize workspace log and create boundary-agent placeholder folders
    log = _logger(state)
    log.step("receive_task", jira_key=jira_key, request=user_request[:200])
    # Create folders for boundary agents that will be called during this task
    # so they always appear in the workspace tree even before their first call.
    for boundary_agent in ("jira", "scm", "ui-design"):
        _boundary_log(state, boundary_agent, "task started", jira_key=jira_key)

    return {
        "task_received": True,
        "jira_key": jira_key,
        "jira_ticket_url": jira_ticket_url,
        "repo_url": state.get("repo_url", ""),
        "figma_url": state.get("figma_url", ""),
        "stitch_project_id": state.get("stitch_project_id", ""),
        "stitch_screen_id": state.get("stitch_screen_id", ""),
        "stitch_screen_name": state.get("stitch_screen_name", ""),
        "tech_stack": state.get("tech_stack") or [],
        "revision_count": 0,
        "max_revisions": 3,
    }


async def analyze_requirements(state: dict) -> dict:
    """Analyze the incoming task using LLM (single-shot ReAct-inside-node)."""
    runtime = state.get("_runtime")
    user_request = state.get("user_request", "")
    log = _logger(state)
    log.step("analyze_requirements")

    if not runtime:
        analysis = {
            "task_type": "general",
            "complexity": "medium",
            "skills": [],
            "summary": user_request,
        }
    else:
        from agents.team_lead.prompts.analysis import ANALYSIS_SYSTEM, ANALYSIS_TEMPLATE

        prompt = ANALYSIS_TEMPLATE.format(
            user_request=user_request,
            jira_key=state.get("jira_key", "N/A"),
        )
        result = runtime.run(
            prompt=prompt,
            system_prompt=ANALYSIS_SYSTEM,
            max_tokens=2048,
            plugin_manager=state.get("_plugin_manager"),
        )

        raw = result.get("raw_response", "")
        try:
            analysis = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            analysis = {
                "task_type": "general",
                "complexity": "medium",
                "skills": [],
                "summary": raw or user_request,
            }

    log.info("analysis complete",
             task_type=analysis.get("task_type"),
             complexity=analysis.get("complexity"))

    # Write analysis.json to workspace
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        tl_dir = os.path.join(workspace_path, "team_lead")
        os.makedirs(tl_dir, exist_ok=True)
        try:
            analysis_file = os.path.join(tl_dir, "analysis.json")
            with open(analysis_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "team-lead",
                        "step": "analyze_requirements",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": analysis,
                }, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            print(f"[team-lead] Failed to write analysis.json: {exc}")

    return {
        "task_type": analysis.get("task_type", "general"),
        "complexity": analysis.get("complexity", "medium"),
        "required_skills": analysis.get("skills", []),
        "analysis_summary": analysis.get("summary", user_request),
    }


async def gather_context(state: dict) -> dict:
    """Gather Jira ticket + design context via boundary agent tools.

    Uses the registered tools (fetch_jira_ticket, fetch_design) to call
    boundary agents via A2A dispatch.

    Writes context-manifest.json to the workspace with file paths for
    downstream agents.
    """
    from framework.tools.registry import get_registry

    registry = get_registry()
    jira_context = state.get("jira_context") or {}
    design_context = state.get("design_context")
    workspace_path = state.get("workspace_path", "")

    log = _logger(state)
    log.step("gather_context")

    jira_files = []
    design_files = []
    design_code_path = ""

    # Fetch Jira ticket if key provided and not already present
    jira_key = state.get("jira_key", "")
    if jira_key and not jira_context:
        log.info("fetching jira ticket", jira_key=jira_key)
        _boundary_log(state, "jira", "fetch_ticket called", jira_key=jira_key)
        try:
            result_str = registry.execute_sync(
                "fetch_jira_ticket", {"ticket_key": jira_key}
            )
            payload = json.loads(result_str) if result_str else {}
            if payload.get("error"):
                log.warn("jira fetch warning", error=payload["error"])
                _boundary_log(state, "jira", "fetch_ticket warning", error=payload["error"])
                print(f"[team-lead] Jira fetch warning: {payload['error']} (continuing without Jira context)")
            else:
                jira_context = payload.get("ticket", payload)
                _boundary_log(state, "jira", "fetch_ticket ok", jira_key=jira_key)
        except Exception as exc:
            log.error("jira fetch failed", error=str(exc))
            _boundary_log(state, "jira", "fetch_ticket failed", error=str(exc))
            print(f"[team-lead] Jira fetch failed: {exc} (continuing without Jira context)")

    # Extract embedded URLs / IDs from Jira ticket content using LLM, falling back to regex.
    figma_url = state.get("figma_url", "")
    stitch_id = state.get("stitch_project_id", "")
    stitch_screen_id = state.get("stitch_screen_id", "")
    stitch_screen_name = state.get("stitch_screen_name", "")
    tech_stack: list = state.get("tech_stack") or []
    repo_url = state.get("repo_url", "")
    extracted_context: dict = {}
    if jira_context:
        runtime = state.get("_runtime")
        extracted = _extract_context_with_llm(jira_context, runtime)
        extracted_context = extracted
        if not repo_url and extracted.get("repo_url"):
            repo_url = extracted["repo_url"]
            print(f"[team-lead] Extracted repo_url from Jira ticket: {repo_url}")
        if not figma_url and extracted.get("figma_url"):
            figma_url = extracted["figma_url"]
            print(f"[team-lead] Extracted figma_url from Jira ticket: {figma_url}")
        if not stitch_id and extracted.get("stitch_project_id"):
            stitch_id = extracted["stitch_project_id"]
            print(f"[team-lead] Extracted stitch_project_id from Jira ticket: {stitch_id}")
        if not stitch_screen_id and extracted.get("stitch_screen_id"):
            stitch_screen_id = extracted["stitch_screen_id"]
            print(f"[team-lead] Extracted stitch_screen_id from Jira ticket: {stitch_screen_id}")
        if not stitch_screen_name and extracted.get("stitch_screen_name"):
            stitch_screen_name = extracted["stitch_screen_name"]
            print(f"[team-lead] Extracted stitch_screen_name from Jira ticket: {stitch_screen_name}")
        if not tech_stack and extracted.get("tech_stack"):
            tech_stack = extracted["tech_stack"]
            print(f"[team-lead] Extracted tech_stack from Jira ticket: {tech_stack}")

    # Save LLM-extracted context to workspace for traceability
    if extracted_context and workspace_path:
        tl_dir = os.path.join(workspace_path, "team_lead")
        os.makedirs(tl_dir, exist_ok=True)
        extraction_file = os.path.join(tl_dir, "jira-context-extracted.json")
        try:
            with open(extraction_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "team-lead",
                        "step": "gather_context_extraction",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": extracted_context,
                }, fh, ensure_ascii=False, indent=2)
            print(f"[team-lead] Saved jira-context-extracted.json to {extraction_file}")
        except OSError as exc:
            print(f"[team-lead] Failed to write jira-context-extracted.json: {exc}")

    # Env var fallbacks (applied after extraction, before design fetch)
    if not repo_url:
        repo_url = os.environ.get("SCM_REPO_URL", "")
    if not stitch_id:
        stitch_id = os.environ.get("STITCH_PROJECT_ID", "")
    if not stitch_screen_id:
        stitch_screen_id = os.environ.get("STITCH_SCREEN_ID", "")
    if not figma_url:
        figma_url = os.environ.get("FIGMA_FILE_URL", "")

    # Write Jira ticket to workspace
    if jira_context and workspace_path:
        tl_dir = os.path.join(workspace_path, "team_lead")
        os.makedirs(tl_dir, exist_ok=True)
        jira_file = os.path.join(tl_dir, "jira-ticket.json")
        try:
            with open(jira_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "team-lead",
                        "step": "gather_context",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": jira_context,
                }, fh, ensure_ascii=False, indent=2)
            jira_files.append("team_lead/jira-ticket.json")
        except OSError as exc:
            print(f"[team-lead] Failed to write jira-ticket.json: {exc}")

    # Fetch design context if URL provided and not already present
    if (figma_url or stitch_id) and not design_context:
        log.info("fetching design context",
                 figma_url=figma_url, stitch_id=stitch_id, screen_id=stitch_screen_id)
        _boundary_log(state, "ui-design", "fetch_design called",
                      stitch_id=stitch_id, screen_id=stitch_screen_id)
        try:
            args: dict[str, str] = {}
            if figma_url:
                args["figma_url"] = figma_url
            elif stitch_id:
                args["stitch_project_id"] = stitch_id
                if stitch_screen_id:
                    args["stitch_screen_id"] = stitch_screen_id
            result_str = registry.execute_sync("fetch_design", args)
            payload = json.loads(result_str) if result_str else {}
            if payload.get("error"):
                log.warn("design fetch warning", error=payload["error"])
                _boundary_log(state, "ui-design", "fetch_design warning", error=payload["error"])
                print(f"[team-lead] Design fetch warning: {payload['error']} (continuing without design context)")
            else:
                design_context = payload
                _boundary_log(state, "ui-design", "fetch_design ok")
        except Exception as exc:
            log.error("design fetch failed", error=str(exc))
            _boundary_log(state, "ui-design", "fetch_design failed", error=str(exc))
            print(f"[team-lead] Design fetch failed: {exc} (continuing without design context)")


    # Write design context to workspace
    if design_context and workspace_path:
        tl_dir = os.path.join(workspace_path, "team_lead")
        os.makedirs(tl_dir, exist_ok=True)
        design_file = os.path.join(tl_dir, "design-spec.json")
        try:
            with open(design_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "team-lead",
                        "step": "gather_context",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": design_context,
                }, fh, ensure_ascii=False, indent=2)
            design_files.append("team_lead/design-spec.json")
        except OSError as exc:
            print(f"[team-lead] Failed to write design-spec.json: {exc}")

        # Download design HTML source code when available (Stitch htmlCode.downloadUrl).
        # This gives the Web Dev agent the exact component structure from the design tool.
        try:
            from urllib.request import Request as _Req, urlopen as _urlopen
            design_data = design_context.get("design") or design_context
            html_download_url = (design_data.get("htmlCode") or {}).get("downloadUrl", "")
            if html_download_url:
                req = _Req(
                    html_download_url,
                    headers={"User-Agent": "constellation-team-lead/1.0"},
                )
                with _urlopen(req, timeout=30) as resp:
                    html_content = resp.read().decode("utf-8", errors="replace")
                code_file = os.path.join(tl_dir, "design-code.html")
                with open(code_file, "w", encoding="utf-8") as fh:
                    fh.write(html_content)
                design_files.append("team_lead/design-code.html")
                design_code_path = code_file
                print(f"[team-lead] Design HTML downloaded: {len(html_content)} chars → {code_file}")
        except Exception as exc:
            print(f"[team-lead] Design HTML download failed (non-fatal): {exc}")

    # Derive repo name from URL — validate it's a real SCM URL first
    _scm_hosts = ("github.com", "bitbucket.org", "gitlab.com", "dev.azure.com")
    if repo_url and not any(h in repo_url for h in _scm_hosts):
        print(f"[team-lead] Ignoring non-SCM repo_url: {repo_url!r}; falling back to SCM_REPO_URL env var")
        repo_url = os.environ.get("SCM_REPO_URL", "")
    repo_name = ""
    if repo_url:
        parts = [p for p in repo_url.rstrip("/").split("/") if p]
        # Strip /browse suffix for Bitbucket
        if parts and parts[-1] == "browse":
            parts.pop()
        repo_name = parts[-1] if parts else "repo"
    repo_path = os.path.join(workspace_path, repo_name) if repo_name else ""

    # Clone repo via SCM Agent (A2A)
    repo_cloned = False
    if repo_url and repo_path:
        log.info("cloning repository", repo_url=repo_url, target=repo_path)
        _boundary_log(state, "scm", "clone_repo called", repo_url=repo_url, target=repo_path)
        try:
            clone_result_str = registry.execute_sync(
                "clone_repo",
                {"repo_url": repo_url, "target_path": repo_path},
            )
            clone_payload = json.loads(clone_result_str) if clone_result_str else {}
            if clone_payload.get("error"):
                detail = clone_payload.get("detail", "")
                detail_msg = f" | git: {detail}" if detail else ""
                _boundary_log(state, "scm", "clone_repo failed",
                               error=clone_payload["error"], detail=detail)
                log.error("repo clone failed", error=clone_payload["error"])
                raise RuntimeError(
                    f"Repo clone FAILED for {repo_url!r}: "
                    f"{clone_payload['error']}{detail_msg}"
                )
            else:
                repo_exists = os.path.isdir(repo_path)
                repo_has_files = repo_exists and any(os.scandir(repo_path))
                if not repo_exists or not repo_has_files:
                    raise RuntimeError(
                        f"Repo clone reported success but path is missing or empty: {repo_path!r}"
                    )
                repo_cloned = True
                _boundary_log(state, "scm", "clone_repo ok", repo_path=repo_path)
                log.info("repo cloned ok", repo_name=repo_name)
                print(f"[team-lead] Repo cloned: {repo_name} → {repo_path}")
        except RuntimeError:
            raise  # propagate clone failures — they are fatal for the workflow
        except Exception as exc:
            log.error("repo clone unexpected error", error=str(exc))
            raise RuntimeError(f"Repo clone raised unexpected error for {repo_url!r}: {exc}") from exc


    # Write context manifest
    context_manifest_path = ""
    if workspace_path:
        tl_dir = os.path.join(workspace_path, "team_lead")
        os.makedirs(tl_dir, exist_ok=True)
        manifest = {
            "metadata": {
                "agent_id": "team-lead",
                "step": "gather_context",
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            },
            "data": {
                "workspace_root": workspace_path,
                "jira_files": jira_files,
                "design_files": design_files,
                "design_code_path": design_code_path,
                "repo_path": repo_path,
                "repo_name": repo_name,
                "repo_cloned": repo_cloned,
            },
        }
        manifest_file = os.path.join(tl_dir, "context-manifest.json")
        try:
            with open(manifest_file, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, ensure_ascii=False, indent=2)
            context_manifest_path = "team_lead/context-manifest.json"
        except OSError as exc:
            print(f"[team-lead] Failed to write context-manifest.json: {exc}")

    return {
        "jira_context": jira_context,
        "design_context": design_context,
        "repo_url": repo_url,
        "figma_url": figma_url,
        "stitch_project_id": stitch_id,
        "stitch_screen_id": stitch_screen_id,
        "stitch_screen_name": stitch_screen_name,
        "tech_stack": tech_stack,
        "repo_name": repo_name,
        "repo_path": repo_path,
        "repo_cloned": repo_cloned,
        "jira_files": jira_files,
        "design_files": design_files,
        "design_code_path": design_code_path,
        "context_manifest_path": context_manifest_path,
    }


async def create_plan(state: dict) -> dict:
    """Create a development plan based on analysis and context (LLM single-shot)."""
    runtime = state.get("_runtime")

    if not runtime:
        return {
            "plan": {
                "steps": [
                    {"step": 1, "action": "Clone repository"},
                    {"step": 2, "action": "Implement changes"},
                    {"step": 3, "action": "Run tests"},
                    {"step": 4, "action": "Create PR"},
                ],
            },
        }

    from agents.team_lead.prompts.planning import PLANNING_SYSTEM, PLANNING_TEMPLATE

    _design_ctx = state.get("design_context")
    _design_ctx_str = json.dumps(_design_ctx, ensure_ascii=False)[:800] if _design_ctx else "N/A"
    prompt = PLANNING_TEMPLATE.format(
        analysis=state.get("analysis_summary", ""),
        jira_context=json.dumps(state.get("jira_context", {}), ensure_ascii=False),
        task_type=state.get("task_type", "general"),
        complexity=state.get("complexity", "medium"),
        design_context=_design_ctx_str,
        design_code_path=state.get("design_code_path", "N/A"),
    )
    result = runtime.run(
        prompt=prompt,
        system_prompt=PLANNING_SYSTEM,
        max_tokens=2048,
        plugin_manager=state.get("_plugin_manager"),
    )

    raw = result.get("raw_response", "")
    try:
        plan = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        plan = {"steps": [{"step": 1, "action": raw or "Execute task"}]}

    # Build skill context
    skills_registry = state.get("_skills_registry")
    required = state.get("required_skills", [])
    skill_context = ""
    if skills_registry and required:
        skill_context = skills_registry.build_prompt_context(required)

    # Write delivery-plan.json to workspace
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        tl_dir = os.path.join(workspace_path, "team_lead")
        os.makedirs(tl_dir, exist_ok=True)
        try:
            plan_file = os.path.join(tl_dir, "delivery-plan.json")
            with open(plan_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "team-lead",
                        "step": "create_plan",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": plan,
                }, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            print(f"[team-lead] Failed to write delivery-plan.json: {exc}")

    return {
        "plan": plan,
        "skill_context": skill_context,
    }


async def dispatch_dev_agent(state: dict) -> dict:
    """Dispatch task to a dev agent (Web Dev, Android, etc.) via A2A tool.

    Passes all gathered context including workspace_paths so the dev agent
    does not re-fetch or guess file locations.
    """
    from framework.tools.registry import get_registry

    registry = get_registry()
    log = _logger(state)
    log.step("dispatch_dev_agent")
    revision_feedback = state.get("revision_feedback", "")
    task_description = _build_dev_brief(state)

    try:
        result_str = registry.execute_sync(
            "dispatch_web_dev",
            {
                "task_description": task_description,
                "jira_context": state.get("jira_context", {}),
                "design_context": state.get("design_context"),
                "design_code_path": state.get("design_code_path", ""),
                "repo_url": state.get("repo_url", ""),
                "repo_path": state.get("repo_path", ""),
                "workspace_path": state.get("workspace_path", ""),
                "context_manifest_path": state.get("context_manifest_path", ""),
                "jira_files": state.get("jira_files", []),
                "design_files": state.get("design_files", []),
                "tech_stack": state.get("tech_stack") or [],
                "stitch_screen_name": state.get("stitch_screen_name", ""),
                "revision_feedback": revision_feedback,
                "definition_of_done": state.get("plan", {}).get("definition_of_done", {
                    "build_must_pass": True,
                    "tests_must_pass": True,
                    "self_assessment_required": True,
                    "jira_state_management": True,
                    "pr_required": True,
                    "screenshot_required": state.get("task_type", "") in ("feature", "ui", "frontend"),
                }),
            },
        )
        payload = json.loads(result_str) if result_str else {}
    except Exception as exc:
        log.error("dev dispatch failed", error=str(exc))
        print(f"[team-lead] Dev dispatch failed: {exc}")
        payload = {"status": "error", "message": str(exc)}

    pr_url = payload.get("prUrl", "")
    branch_name = payload.get("branch", "")
    jira_in_review = payload.get("jiraInReview", False)
    log.info("dev dispatch result",
             status=payload.get("status", "?"), pr_url=pr_url,
             branch=branch_name, jira_in_review=jira_in_review)
    print(
        f"[team-lead] Dev dispatch result: status={payload.get('status','?')} "
        f"prUrl={pr_url!r} branch={branch_name!r} jiraInReview={jira_in_review}"
    )
    if payload.get("error"):
        print(f"[team-lead] Dev dispatch error detail: {payload['error']}")

    return {
        "dev_dispatched": True,
        "dev_result": payload,
        "pr_url": pr_url,
        "branch_name": branch_name,
        "jira_in_review": jira_in_review,
    }


async def review_result(state: dict) -> dict:
    """Review the dev agent output via Code Review Agent.

    Passes Jira context, design context, and workspace paths to the
    Code Review Agent for comprehensive review.

    Returns a route:
      - "approved": review passed
      - "needs_revision": review rejected, revision count < max
      - "need_user_input": max revisions reached, escalate
    """
    from framework.tools.registry import get_registry

    registry = get_registry()
    pr_url = state.get("pr_url", "")
    dev_result = state.get("dev_result", {})

    try:
        result_str = registry.execute_sync(
            "dispatch_code_review",
            {
                "pr_url": pr_url,
                "diff_summary": dev_result.get("summary", ""),
                "requirements": state.get("analysis_summary", "") or state.get("user_request", ""),
                "jira_context": state.get("jira_context", {}),
                "design_context": state.get("design_context"),
                "workspace_path": state.get("workspace_path", ""),
                "context_manifest_path": state.get("context_manifest_path", ""),
            },
        )
        payload = json.loads(result_str) if result_str else {}
    except Exception as exc:
        print(f"[team-lead] Code review dispatch failed: {exc}")
        payload = {"verdict": "error", "message": str(exc)}

    verdict = payload.get("verdict", "rejected")
    revision_count = state.get("revision_count", 0)

    if verdict == "approved":
        route = "approved"
    elif revision_count >= state.get("max_revisions", 3):
        route = "need_user_input"
    else:
        route = "needs_revision"

    return {
        "review_result": payload,
        "review_verdict": verdict,
        "route": route,
    }


async def request_revision(state: dict) -> dict:
    """Prepare revision feedback for the dev agent and loop back."""
    review = state.get("review_result", {})
    comments = review.get("comments", [])
    summary = review.get("summary", review.get("message", ""))

    feedback_lines = []
    if summary:
        feedback_lines.append(f"Review summary: {summary}")
    for c in comments[:10]:  # Limit to top 10 comments
        feedback_lines.append(f"- [{c.get('severity', 'info')}] {c.get('message', '')}")

    return {
        "revision_feedback": "\n".join(feedback_lines) or "Code review rejected. Please fix issues.",
        "revision_count": state.get("revision_count", 0) + 1,
    }


async def report_success(state: dict) -> dict:
    """Build final success report."""
    pr_url = state.get("pr_url", "N/A")
    branch = state.get("branch_name", "N/A")
    analysis = state.get("analysis_summary", "")
    verdict = state.get("review_verdict", "approved")
    revision_count = state.get("revision_count", 0)

    report_summary = (
        f"Task completed successfully.\n"
        f"Analysis: {analysis}\n"
        f"PR: {pr_url}\n"
        f"Branch: {branch}\n"
        f"Review verdict: {verdict}\n"
        f"Revisions: {revision_count}"
    )

    # Write final-report.json to workspace
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        tl_dir = os.path.join(workspace_path, "team_lead")
        os.makedirs(tl_dir, exist_ok=True)
        try:
            report_file = os.path.join(tl_dir, "final-report.json")
            with open(report_file, "w", encoding="utf-8") as fh:
                json.dump({
                    "metadata": {
                        "agent_id": "team-lead",
                        "step": "report_success",
                        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                    },
                    "data": {
                        "pr_url": pr_url,
                        "branch": branch,
                        "analysis": analysis,
                        "review_verdict": verdict,
                        "revision_count": revision_count,
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            print(f"[team-lead] Failed to write final-report.json: {exc}")

    return {
        "report_summary": report_summary,
        "success": True,
        "jira_in_review": state.get("jira_in_review", False),
    }


async def escalate_to_user(state: dict) -> dict:
    """Escalate to user after max revision attempts.

    On first entry, raises InterruptSignal so the workflow pauses and the
    orchestrator can forward the question to the user.

    On resume, ``_resume_value`` contains the user's guidance.  The node
    consumes it and returns a route that feeds the user input back into the
    revision loop so ``dispatch_dev_agent`` can apply the feedback.
    """
    # ------------------------------------------------------------------
    # Resume path: _resume_value was set by WorkflowRunner.resume()
    # ------------------------------------------------------------------
    resume_value = state.get("_resume_value")
    if resume_value is not None:
        return {
            "revision_feedback": (
                f"User guidance after escalation: {resume_value}"
            ),
            "revision_count": 0,  # reset so the loop can run again
            "route": "user_responded",
        }

    # ------------------------------------------------------------------
    # First entry: interrupt
    # ------------------------------------------------------------------
    from framework.workflow import interrupt

    revision_count = state.get("revision_count", 0)
    review = state.get("review_result", {})

    question = (
        f"Task requires user intervention after {revision_count} revision attempts.\n"
        f"Last review verdict: {review.get('verdict', 'unknown')}\n"
        f"PR: {state.get('pr_url', 'N/A')}\n"
        f"Please review the remaining issues and provide guidance."
    )

    interrupt(
        question,
        revision_count=revision_count,
        pr_url=state.get("pr_url", ""),
        review_verdict=review.get("verdict", "unknown"),
    )

    # unreachable — interrupt() raises InterruptSignal
    return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_dev_brief(state: dict) -> str:
    """Assemble a comprehensive dev agent brief from all gathered context."""
    parts = [f"Task: {state.get('user_request', '')}"]

    analysis = state.get("analysis_summary", "")
    if analysis:
        parts.append(f"\nAnalysis:\n{analysis}")

    # Extracted context summary (tech stack, screen name)
    tech_stack = state.get("tech_stack") or []
    stitch_screen_name = state.get("stitch_screen_name", "")
    if tech_stack:
        parts.append(f"\nTech stack: {', '.join(tech_stack)}")
    if stitch_screen_name:
        parts.append(f"\nTarget screen name: {stitch_screen_name}")

    plan = state.get("plan", {})
    if plan:
        parts.append(f"\nPlan:\n{json.dumps(plan, indent=2, ensure_ascii=False)}")

    jira = state.get("jira_context", {})
    if jira:
        parts.append(f"\nJira context:\n{json.dumps(jira, indent=2, ensure_ascii=False)}")

    design = state.get("design_context")
    if design:
        parts.append(f"\nDesign context:\n{json.dumps(design, indent=2, ensure_ascii=False)}")

    skill_ctx = state.get("skill_context", "")
    if skill_ctx:
        parts.append(f"\nSkill guidance:\n{skill_ctx}")

    revision = state.get("revision_feedback", "")
    if revision:
        parts.append(f"\nRevision feedback:\n{revision}")

    return "\n".join(parts)


async def wait_for_dev(state: dict) -> dict:
    """Wait for dev agent to complete.

    Sets route to 'completed', 'needs_clarification', or 'failed'.
    """
    dev_result = state.get("dev_result", {})
    dev_state = dev_result.get("state", "")

    if dev_state == "TASK_STATE_COMPLETED":
        return {"route": "completed", "pr_url": dev_result.get("pr_url", "")}
    if dev_state == "TASK_STATE_INPUT_REQUIRED":
        return {"route": "needs_clarification"}

    # Default: failed
    return {"route": "failed", "escalation_reason": "Dev agent did not complete."}


async def handle_question(state: dict) -> dict:
    """Handle a clarification question from the dev agent.

    Tries to answer it from context; escalates to user if unable.
    """
    # MVP: always escalate
    return {"route": "user_needed", "escalation_reason": "Clarification needed from user."}


# ---------------------------------------------------------------------------
# URL extraction helpers
# ---------------------------------------------------------------------------

def _jira_to_text(jira_context: dict) -> str:
    """Flatten Jira ticket dict into a single searchable text blob."""
    parts = []
    for key in ("key", "summary", "url"):
        if jira_context.get(key):
            parts.append(str(jira_context[key]))
    fields = jira_context.get("fields", jira_context)
    for _k, val in fields.items():
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, dict):
            parts.append(json.dumps(val))
        elif isinstance(val, list):
            parts.append(" ".join(str(v) for v in val))
    return "\n".join(parts)


def _extract_urls_from_ticket(jira_context: dict) -> dict:
    """Extract GitHub, Stitch, and Figma URLs from a Jira ticket dict (regex fallback)."""
    text = _jira_to_text(jira_context)
    result: dict = {}

    # GitHub repo URL (stop before /issues, /pulls, /tree, /blob, /commit paths)
    repo_match = re.search(
        r'https?://github\.com/([\w.-]+/[\w.-]+?)(?:/(?:issues|pulls|tree|blob|commit|compare|releases)|\s|$)',
        text,
    )
    if repo_match:
        result["repo_url"] = f"https://github.com/{repo_match.group(1)}".rstrip("/")

    # Figma file/design URL
    figma_match = re.search(
        r'https?://(?:www\.)?figma\.com/(?:file|design)/([\w-]+[^\s]*)',
        text,
    )
    if figma_match:
        result["figma_url"] = figma_match.group(0).rstrip("/")

    # Google Stitch: prefer full URL, fall back to bare project ID in text
    stitch_match = re.search(
        r'https?://stitch\.withgoogle\.com/projects/(\d+)',
        text,
    )
    if stitch_match:
        result["stitch_project_id"] = stitch_match.group(1)
        # Screen ID: 32-char hex string that appears after the project URL
        after_stitch = text[stitch_match.end():]
        screen_match = re.search(r'\b([a-f0-9]{32})\b', after_stitch)
        if screen_match:
            result["stitch_screen_id"] = screen_match.group(1)
    else:
        # Fallback: bare project ID in ticket text (e.g. "ID: 13629074018280446337")
        bare_id_match = re.search(
            r'(?:stitch|project)[^:]*ID[:\s]+(\d{15,20})',
            text, re.IGNORECASE,
        )
        if bare_id_match:
            result["stitch_project_id"] = bare_id_match.group(1)
        # Screen ID: 32-char hex anywhere in the text
        if "stitch_project_id" in result:
            screen_match = re.search(r'\b([a-f0-9]{32})\b', text)
            if screen_match:
                result["stitch_screen_id"] = screen_match.group(1)

    return result


def _extract_context_with_llm(jira_context: dict, runtime) -> dict:
    """Use LLM to extract structured context from a Jira ticket.

    Returns a dict with keys: repo_url, stitch_project_id, stitch_screen_id,
    stitch_screen_name, figma_url, tech_stack, feature_description.

    Falls back to regex extraction if runtime is unavailable or LLM call fails.
    """
    if not runtime:
        print("[team-lead] No runtime available for LLM extraction — using regex fallback")
        return _extract_urls_from_ticket(jira_context)

    from agents.team_lead.prompts.extraction import EXTRACTION_SYSTEM, EXTRACTION_TEMPLATE

    jira_text = _jira_to_text(jira_context)
    prompt = EXTRACTION_TEMPLATE.format(jira_text=jira_text[:4000])

    try:
        result = runtime.run(
            prompt=prompt,
            system_prompt=EXTRACTION_SYSTEM,
            max_tokens=512,
        )
        raw = (result.get("raw_response") or "").strip()
        # Strip markdown code fences if present
        raw = re.sub(r'^```(?:json)?\s*', '', raw)
        raw = re.sub(r'\s*```$', '', raw)
        extracted = json.loads(raw)

        # Normalize: convert null / None to empty defaults
        cleaned = {
            "repo_url": extracted.get("repo_url") or "",
            "stitch_project_id": str(extracted.get("stitch_project_id") or ""),
            "stitch_screen_id": str(extracted.get("stitch_screen_id") or ""),
            "stitch_screen_name": extracted.get("stitch_screen_name") or "",
            "figma_url": extracted.get("figma_url") or "",
            "tech_stack": extracted.get("tech_stack") or [],
            "feature_description": extracted.get("feature_description") or "",
        }
        print(f"[team-lead] LLM extraction result: {json.dumps(cleaned, ensure_ascii=False)}")
        return cleaned
    except Exception as exc:
        print(f"[team-lead] LLM extraction failed ({exc}) — falling back to regex")
        return _extract_urls_from_ticket(jira_context)


