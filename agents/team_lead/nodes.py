"""Team Lead Agent workflow nodes.

Architecture: **Graph outside, ReAct inside**.

Each node is an async function that receives the workflow state dict and returns
a dict of state updates.  Nodes that need open-ended reasoning use the runtime
for single-shot LLM calls or bounded ReAct; the graph controls the macro flow.
"""
from __future__ import annotations

import json
import os
import time
from typing import Any


async def receive_task(state: dict) -> dict:
    """Parse and validate the incoming task request."""
    user_request = state.get("user_request", "")
    return {
        "task_received": True,
        "jira_key": state.get("jira_key", ""),
        "repo_url": state.get("repo_url", ""),
        "figma_url": state.get("figma_url", ""),
        "stitch_project_id": state.get("stitch_project_id", ""),
        "revision_count": 0,
        "max_revisions": 3,
    }


async def analyze_requirements(state: dict) -> dict:
    """Analyze the incoming task using LLM (single-shot ReAct-inside-node)."""
    runtime = state.get("_runtime")
    user_request = state.get("user_request", "")

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

    jira_files = []
    design_files = []
    design_code_path = ""

    # Fetch Jira ticket if key provided and not already present
    jira_key = state.get("jira_key", "")
    if jira_key and not jira_context:
        try:
            result_str = registry.execute_sync(
                "fetch_jira_ticket", {"ticket_key": jira_key}
            )
            payload = json.loads(result_str) if result_str else {}
            if payload.get("error"):
                print(f"[team-lead] Jira fetch warning: {payload['error']} (continuing without Jira context)")
            else:
                # Normalize: Jira adapter returns {"ticket": {...}, "status": 200}.
                # Downstream consumers expect a flat ticket object with top-level "key".
                jira_context = payload.get("ticket", payload)
        except Exception as exc:
            print(f"[team-lead] Jira fetch failed: {exc} (continuing without Jira context)")

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
    figma_url = state.get("figma_url", "")
    stitch_id = state.get("stitch_project_id", "")
    stitch_screen_id = state.get("stitch_screen_id", "")
    if (figma_url or stitch_id) and not design_context:
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
                print(f"[team-lead] Design fetch warning: {payload['error']} (continuing without design context)")
            else:
                design_context = payload
        except Exception as exc:
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

    # Derive repo name from URL
    repo_url = state.get("repo_url", "")
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
        try:
            clone_result_str = registry.execute_sync(
                "clone_repo",
                {"repo_url": repo_url, "target_path": repo_path},
            )
            clone_payload = json.loads(clone_result_str) if clone_result_str else {}
            if clone_payload.get("error"):
                detail = clone_payload.get("detail", "")
                detail_msg = f" | git: {detail}" if detail else ""
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
                print(f"[team-lead] Repo cloned: {repo_name} → {repo_path}")
        except RuntimeError:
            raise  # propagate clone failures — they are fatal for the workflow
        except Exception as exc:
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

    prompt = PLANNING_TEMPLATE.format(
        analysis=state.get("analysis_summary", ""),
        jira_context=json.dumps(state.get("jira_context", {}), ensure_ascii=False),
        task_type=state.get("task_type", "general"),
        complexity=state.get("complexity", "medium"),
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
        print(f"[team-lead] Dev dispatch failed: {exc}")
        payload = {"status": "error", "message": str(exc)}

    pr_url = payload.get("prUrl", "")
    branch_name = payload.get("branch", "")
    jira_in_review = payload.get("jiraInReview", False)
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



