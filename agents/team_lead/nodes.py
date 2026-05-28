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


async def _ack_and_cleanup_dev_agent(state: dict) -> dict[str, Any]:
    """Acknowledge the current Web Dev child task and tear down any per-task instance."""
    session = state.get("dev_agent_session") or {}
    if not isinstance(session, dict):
        session = {}

    child_task_id = str(session.get("task_id") or "").strip()
    child_service_url = str(session.get("service_url") or "").strip()
    child_container_name = str(session.get("container_name") or "").strip()
    child_agent_id = str(session.get("agent_id") or "web-dev").strip() or "web-dev"
    if not child_task_id and not child_container_name:
        return {}

    log = _logger(state)
    acknowledged = False
    cleaned_up = False

    if child_task_id and child_service_url:
        try:
            from framework.a2a.client import A2AClient

            await A2AClient(timeout=10).send_ack(child_service_url, child_task_id)
            acknowledged = True
            log.a2a("→", child_agent_id, action="ack", child_task_id=child_task_id)
        except Exception as exc:
            log.warn("dev agent ack failed", error=str(exc), child_task_id=child_task_id)

    if child_container_name:
        try:
            from framework.launcher import get_launcher

            get_launcher().destroy_instance(child_agent_id, child_container_name)
            cleaned_up = True
            log.info("dev agent instance destroyed", agent_id=child_agent_id, container_name=child_container_name)
        except Exception as exc:
            log.warn("dev agent cleanup failed", error=str(exc), container_name=child_container_name)

    return {
        "dev_agent_acknowledged": acknowledged,
        "dev_agent_cleaned_up": cleaned_up,
        "dev_agent_session": {},
    }


def _safe_json(text: str, fallback: Any = None) -> Any:
    """Extract and parse the first JSON object/array from *text*.

    Handles LLM responses wrapped in markdown code fences (```json...```).
    Returns *fallback* when *text* is None/empty or no valid JSON is found.
    """
    if not text:
        return fallback
    # Strip markdown code fences if present
    stripped = re.sub(r"^```(?:json)?\s*\n?", "", text.strip(), flags=re.IGNORECASE)
    stripped = re.sub(r"\n?```$", "", stripped.strip())
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, TypeError):
        pass
    # Try extracting a JSON object or array
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


def _is_success_status(status: Any) -> bool:
    text = str(status or "").strip().lower()
    if not text:
        return False
    return text in {"ok", "success", "fetched", "200", "201"} or text.startswith("2")


def _validate_jira_payload(payload: dict, jira_key: str) -> dict:
    """Return a Jira ticket dict or raise when the fetch result is unusable."""
    if not isinstance(payload, dict):
        raise RuntimeError(f"Jira fetch failed for {jira_key}: invalid response payload")
    if payload.get("error"):
        raise RuntimeError(f"Jira fetch failed for {jira_key}: {payload['error']}")

    status = payload.get("status", "")
    ticket = payload.get("ticket", payload)
    if not _is_success_status(status):
        raise RuntimeError(f"Jira fetch failed for {jira_key}: status={status or 'unknown'}")
    if not isinstance(ticket, dict) or not ticket.get("key"):
        raise RuntimeError(f"Jira fetch failed for {jira_key}: ticket payload missing key")
    if jira_key and str(ticket.get("key", "")).upper() != jira_key.upper():
        raise RuntimeError(
            f"Jira fetch failed for {jira_key}: fetched ticket key {ticket.get('key')!r} does not match"
        )
    return ticket


def _require_repo_url(repo_url: str, jira_key: str) -> None:
    if not repo_url:
        source = f"Jira ticket {jira_key}" if jira_key else "task context"
        raise RuntimeError(f"No SCM repository URL was found in {source}; cannot dispatch development agent")

    scm_hosts = ("github.com", "bitbucket.org", "gitlab.com", "dev.azure.com")
    if not any(host in repo_url for host in scm_hosts):
        raise RuntimeError(f"Repository URL is not a supported SCM URL: {repo_url!r}")


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
            print(f"[{_AGENT_ID}] Extracted jira_key={jira_key} from URL in user_request")
        else:
            key_match = _re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", user_request)
            if key_match:
                jira_key = key_match.group(1)
                print(f"[{_AGENT_ID}] Extracted jira_key={jira_key} from user_request text")

    # Initialize workspace log
    log = _logger(state)
    log.node("receive_task", jira_key=jira_key, request=user_request[:200])

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
    log.node("analyze_requirements")

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
        analysis = _safe_json(raw, fallback=None)
        if not isinstance(analysis, dict):
            analysis = {
                "task_type": "general",
                "complexity": "medium",
                "skills": [],
                "summary": raw or user_request,
            }

    log.info("analysis complete",
             task_type=analysis.get("task_type"),
             complexity=analysis.get("complexity"))

    from framework.validation_gates import validate_analysis_schema
    analysis_gate = validate_analysis_schema(analysis)
    if not analysis_gate.passed:
        log.warn("validate_analysis_schema gate failed", feedback=analysis_gate.feedback)
        analysis.setdefault("task_type", "general")
        analysis.setdefault("complexity", "medium")
        analysis.setdefault("skills", [])

    # Write analysis.json to workspace
    workspace_path = state.get("workspace_path", "")
    if workspace_path:
        tl_dir = os.path.join(workspace_path, _AGENT_ID)
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
            print(f"[{_AGENT_ID}] Failed to write analysis.json: {exc}")

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
    log.node("gather_context")

    jira_files = []
    design_files = []
    design_code_path = ""

    # Fetch Jira ticket if key provided and not already present
    jira_key = state.get("jira_key", "")
    task_id = state.get("_task_id", "")
    jira_local_folder = ""
    if jira_key and not jira_context:
        log.info("fetching jira ticket", jira_key=jira_key)
        log.a2a("→", "jira", capability="fetch_jira_ticket", jira_key=jira_key,
                workspace_path=workspace_path or "(not set)")
        try:
            result_str = registry.execute_sync(
                "fetch_jira_ticket",
                {"ticket_key": jira_key, "task_id": task_id, "workspace_path": workspace_path}
            )
            payload = json.loads(result_str) if result_str else {}
            jira_context = _validate_jira_payload(payload, jira_key)
            jira_local_folder = payload.get("local_folder", "")
            returned_files = payload.get("files", [])
            if returned_files:
                jira_files.extend(returned_files)
            log.info("jira fetch ok", jira_key=jira_key, local_folder=jira_local_folder,
                     files=returned_files)
            log.a2a("←", "jira", capability="fetch_jira_ticket", jira_key=jira_key,
                    local_folder=jira_local_folder, files_count=len(returned_files))
        except Exception as exc:
            log.error("jira fetch failed", error=str(exc))
            print(f"[{_AGENT_ID}] Jira fetch failed: {exc}")
            raise

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
            print(f"[{_AGENT_ID}] Extracted repo_url from Jira ticket: {repo_url}")
        if not figma_url and extracted.get("figma_url"):
            figma_url = extracted["figma_url"]
            print(f"[{_AGENT_ID}] Extracted figma_url from Jira ticket: {figma_url}")
        if not stitch_id and extracted.get("stitch_project_id"):
            stitch_id = extracted["stitch_project_id"]
            print(f"[{_AGENT_ID}] Extracted stitch_project_id from Jira ticket: {stitch_id}")
        if not stitch_screen_id and extracted.get("stitch_screen_id"):
            stitch_screen_id = extracted["stitch_screen_id"]
            print(f"[{_AGENT_ID}] Extracted stitch_screen_id from Jira ticket: {stitch_screen_id}")
        if not stitch_screen_name and extracted.get("stitch_screen_name"):
            stitch_screen_name = extracted["stitch_screen_name"]
            print(f"[{_AGENT_ID}] Extracted stitch_screen_name from Jira ticket: {stitch_screen_name}")
        if not tech_stack and extracted.get("tech_stack"):
            tech_stack = extracted["tech_stack"]
            print(f"[{_AGENT_ID}] Extracted tech_stack from Jira ticket: {tech_stack}")

    # Save LLM-extracted context to workspace for traceability
    if extracted_context and workspace_path:
        tl_dir = os.path.join(workspace_path, _AGENT_ID)
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
            print(f"[{_AGENT_ID}] Saved jira-context-extracted.json to {extraction_file}")
        except OSError as exc:
            print(f"[{_AGENT_ID}] Failed to write jira-context-extracted.json: {exc}")

    # Env var fallbacks for design endpoints only. Repository routing must come
    # from the request or fetched Jira context so invalid tickets cannot drift to
    # a default repository.
    if not stitch_id:
        stitch_id = os.environ.get("STITCH_PROJECT_ID", "")
    if not stitch_screen_id:
        stitch_screen_id = os.environ.get("STITCH_SCREEN_ID", "")
    if not figma_url:
        figma_url = os.environ.get("FIGMA_FILE_URL", "")

    # Write Jira ticket to workspace
    if jira_context and workspace_path:
        tl_dir = os.path.join(workspace_path, _AGENT_ID)
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
            jira_files.append("team-lead/jira-ticket.json")
        except OSError as exc:
            print(f"[{_AGENT_ID}] Failed to write jira-ticket.json: {exc}")

    # Fetch design context if URL provided and not already present
    design_local_folder = ""
    design_code_path_from_agent = ""
    design_md_path_from_agent = ""
    design_screen_path_from_agent = ""
    returned_design_files: list[str] = []
    if (figma_url or stitch_id) and not design_context:
        log.info("fetching design context",
                 figma_url=figma_url, stitch_id=stitch_id, screen_id=stitch_screen_id,
                 workspace_path=workspace_path or "(not set)")
        log.a2a("→", "ui-design", capability="fetch_design",
                stitch_id=stitch_id, screen_id=stitch_screen_id,
                workspace_path=workspace_path or "(not set)")
        try:
            args: dict[str, str] = {"task_id": task_id, "workspace_path": workspace_path}
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
                print(f"[{_AGENT_ID}] Design fetch warning: {payload['error']} (continuing without design context)")
            else:
                design_context = payload
                design_local_folder = payload.get("local_folder", "")
                design_code_path_from_agent = payload.get("design_code_path", "")
                design_md_path_from_agent = payload.get("design_md_path", "")
                design_screen_path_from_agent = payload.get("design_screen_path", "")
                returned_design_files = payload.get("files", [])
                if returned_design_files:
                    design_files.extend(returned_design_files)
                log.info("design fetch ok", local_folder=design_local_folder,
                         files=returned_design_files,
                         code_path=design_code_path_from_agent,
                         md_path=design_md_path_from_agent)
                log.a2a("←", "ui-design", capability="fetch_design",
                        local_folder=design_local_folder,
                        files_count=len(returned_design_files),
                        code_path=design_code_path_from_agent)
                print(f"[{_AGENT_ID}] Design fetch ok: folder={design_local_folder!r} files={returned_design_files}")
        except Exception as exc:
            log.error("design fetch failed", error=str(exc))
            print(f"[{_AGENT_ID}] Design fetch failed: {exc} (continuing without design context)")


    # Write design context JSON to team-lead workspace (for audit/fallback)
    if design_context and workspace_path:
        tl_dir = os.path.join(workspace_path, _AGENT_ID)
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
            if "team-lead/design-spec.json" not in design_files:
                design_files.append("team-lead/design-spec.json")
        except OSError as exc:
            print(f"[{_AGENT_ID}] Failed to write design-spec.json: {exc}")

    # Use design file paths from the UI Design agent when available.
    design_code_path = design_code_path_from_agent
    design_md_path = design_md_path_from_agent
    design_screen_path = design_screen_path_from_agent

    if workspace_path and stitch_id and design_context:
        expected_folder = os.path.join(workspace_path, "ui-design", "stitch")
        missing_design_outputs: list[str] = []
        if not design_local_folder or not os.path.isdir(design_local_folder):
            missing_design_outputs.append(expected_folder)
        if not design_code_path or not os.path.isfile(design_code_path):
            missing_design_outputs.append(os.path.join(expected_folder, "code.html"))
        if not design_md_path or not os.path.isfile(design_md_path):
            missing_design_outputs.append(os.path.join(expected_folder, "DESIGN.md"))
        if missing_design_outputs:
            raise RuntimeError(
                "UI Design files missing from workspace: " + ", ".join(missing_design_outputs)
            )

    if not workspace_path and not design_code_path and design_context:
        # Legacy fallback: extract and save to team-lead/ directory
        tl_dir = os.path.join(workspace_path, _AGENT_ID)
        os.makedirs(tl_dir, exist_ok=True)
        try:
            html_content = ""
            design_md_content = ""

            stitch_screen_data = design_context.get("screen", {}) if isinstance(design_context, dict) else {}
            stitch_text = stitch_screen_data.get("text", "")

            if stitch_text and stitch_text.strip().startswith("{"):
                from urllib.request import Request as _Req, urlopen as _urlopen
                screen_meta = json.loads(stitch_text)
                html_download_url = (screen_meta.get("htmlCode") or {}).get("downloadUrl", "")
                if html_download_url:
                    req = _Req(html_download_url, headers={"User-Agent": "constellation-team-lead/1.0"})
                    with _urlopen(req, timeout=30) as resp:
                        html_content = resp.read().decode("utf-8", errors="replace")
                    print(f"[{_AGENT_ID}] Fallback: Downloaded HTML from htmlCode.downloadUrl: {len(html_content)} chars")
                title = screen_meta.get("title", "Design Screen")
                width = screen_meta.get("width", "")
                height = screen_meta.get("height", "")
                device = screen_meta.get("deviceType", "")
                design_md_content = (
                    f"# {title}\n\nScreen: {width}x{height} ({device})\n"
                    f"Project: {stitch_screen_data.get('projectId', '')} "
                    f"Screen: {stitch_screen_data.get('screenId', '')}\n"
                )
            elif stitch_text:
                html_marker = "<!DOCTYPE html"
                if html_marker in stitch_text or "<html" in stitch_text:
                    idx_html = stitch_text.find(html_marker)
                    if idx_html < 0:
                        idx_html = stitch_text.find("<html")
                    html_and_after = stitch_text[idx_html:]
                    html_end_idx = html_and_after.rfind("</html>")
                    html_content = html_and_after[:html_end_idx + 7] if html_end_idx >= 0 else html_and_after
                    design_md_content = stitch_text[:idx_html].strip()
                else:
                    design_md_content = stitch_text

            if html_content:
                code_file = os.path.join(tl_dir, "design-code.html")
                with open(code_file, "w", encoding="utf-8") as fh:
                    fh.write(html_content)
                design_files.append(f"{_AGENT_ID}/design-code.html")
                design_code_path = code_file
            if design_md_content:
                md_file = os.path.join(tl_dir, "design-spec.md")
                with open(md_file, "w", encoding="utf-8") as fh:
                    fh.write(design_md_content)
                design_files.append(f"{_AGENT_ID}/design-spec.md")
                design_md_path = md_file
        except Exception as exc:
            print(f"[{_AGENT_ID}] Design content extraction fallback failed (non-fatal): {exc}")

    # Derive repo name from URL — validate it is a real SCM URL first.
    _require_repo_url(repo_url, jira_key)
    repo_name = ""
    if repo_url:
        parts = [p for p in repo_url.rstrip("/").split("/") if p]
        # Strip /browse suffix for Bitbucket
        if parts and parts[-1] == "browse":
            parts.pop()
        repo_name = parts[-1] if parts else "repo"
    # Clone repo under scm/<repo_name>/ — the SCM agent owns its folder
    repo_path = os.path.join(workspace_path, "scm", repo_name) if repo_name else ""

    # Clone repo via SCM Agent (A2A)
    repo_cloned = False
    if repo_url and repo_path:
        log.info("cloning repository", repo_url=repo_url, target=repo_path)
        log.a2a("→", "scm", capability="clone_repo", repo_url=repo_url, local_target=repo_path)
        try:
            clone_result_str = registry.execute_sync(
                "clone_repo",
                {"repo_url": repo_url, "target_path": repo_path, "task_id": task_id},
            )
            clone_payload = json.loads(clone_result_str) if clone_result_str else {}
            if clone_payload.get("error"):
                detail = clone_payload.get("detail", "")
                detail_msg = f" | git: {detail}" if detail else ""
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
                log.info("repo clone ok", repo_name=repo_name, local_path=repo_path)
                log.a2a("←", "scm", capability="clone_repo", local_path=repo_path, repo_name=repo_name)
                print(f"[{_AGENT_ID}] Repo cloned: {repo_name} → {repo_path}")
        except RuntimeError:
            raise  # propagate clone failures — they are fatal for the workflow
        except Exception as exc:
            log.error("repo clone unexpected error", error=str(exc))
            raise RuntimeError(f"Repo clone raised unexpected error for {repo_url!r}: {exc}") from exc


    # Write context manifest
    context_manifest_path = ""
    if workspace_path:
        tl_dir = os.path.join(workspace_path, _AGENT_ID)
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
                "jira_local_folder": jira_local_folder,
                "design_files": design_files,
                "design_local_folder": design_local_folder,
                "design_code_path": design_code_path,
                "design_md_path": design_md_path,
                "design_screen_path": design_screen_path,
                "repo_path": repo_path,
                "repo_name": repo_name,
                "repo_cloned": repo_cloned,
            },
        }
        manifest_file = os.path.join(tl_dir, "context-manifest.json")
        try:
            with open(manifest_file, "w", encoding="utf-8") as fh:
                json.dump(manifest, fh, ensure_ascii=False, indent=2)
            context_manifest_path = "team-lead/context-manifest.json"
        except OSError as exc:
            print(f"[{_AGENT_ID}] Failed to write context-manifest.json: {exc}")

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
        "jira_local_folder": jira_local_folder,
        "design_files": design_files,
        "design_local_folder": design_local_folder,
        "design_code_path": design_code_path,
        "design_md_path": design_md_path if "design_md_path" in dir() else "",
        "context_manifest_path": context_manifest_path,
    }


async def validate_readiness(state: dict) -> dict:
    """Deterministic gate: verify all prerequisites for planning/dispatch.

    Checks: repo cloned, Jira context present (if key given), repo URL valid.
    Returns route='ready' on success, or a deterministic graph route for
    retry/user input when prerequisites are not complete.
    """
    from framework.validation_gates import validate_readiness as _gate

    log = _logger(state)
    log.node("validate_readiness")

    jira_key = str(state.get("jira_key") or "")
    jira_context = state.get("jira_context") or {}
    repo_path = str(state.get("repo_path") or "")
    repo_cloned = bool(state.get("repo_cloned")) and bool(repo_path) and os.path.isdir(repo_path)
    repo_non_empty = False
    if repo_cloned:
        with os.scandir(repo_path) as entries:
            repo_non_empty = any(entries)
    context_key = ""
    if isinstance(jira_context, dict):
        context_key = str(jira_context.get("key") or jira_context.get("ticket_key") or "")
    is_ui_task = bool(
        state.get("design_context")
        or state.get("figma_url")
        or state.get("stitch_project_id")
        or state.get("stitch_screen_id")
        or state.get("design_code_path")
    )
    design_spec_exists = bool(
        state.get("design_context")
        or (state.get("design_code_path") and os.path.isfile(str(state.get("design_code_path"))))
        or (state.get("design_md_path") and os.path.isfile(str(state.get("design_md_path"))))
    )

    result = _gate(
        jira_downloaded=(not jira_key) or bool(jira_context),
        jira_key_matches=(not jira_key) or (context_key == jira_key),
        repo_cloned=repo_cloned,
        repo_non_empty=repo_non_empty,
        is_ui_task=is_ui_task,
        design_spec_exists=design_spec_exists,
        tech_stack_identified=bool(state.get("tech_stack")),
        requirements_clarified=bool(state.get("analysis_summary") or jira_context),
    )

    if not result.passed:
        attempts = int(state.get("readiness_attempts", 0)) + 1
        failed = set((result.details or {}).get("failed", []))
        retryable = failed <= {"design_spec_exists", "tech_stack_identified"}
        route = "missing_info" if attempts < 3 and retryable else "need_user_input"
        log.warn(
            "readiness gate failed",
            gate=result.gate_name,
            feedback=result.feedback,
            attempts=attempts,
            route=route,
        )
        return {
            "readiness_validated": False,
            "readiness_attempts": attempts,
            "readiness_feedback": result.feedback,
            "route": route,
        }

    log.info("readiness gate passed")
    return {"readiness_validated": True, "route": "ready"}


async def create_plan(state: dict) -> dict:
    """Create a development plan based on analysis and context (LLM single-shot).

    After planning, runs validate_readiness gate to ensure all critical
    prerequisites are available before dispatching a dev agent.
    """
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
    plan = _safe_json(raw, fallback=None)
    if not isinstance(plan, dict):
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
        tl_dir = os.path.join(workspace_path, _AGENT_ID)
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
            print(f"[{_AGENT_ID}] Failed to write delivery-plan.json: {exc}")

    # Validation gate: ensure plan has required structure
    from framework.validation_gates import validate_plan_schema
    gate_result = validate_plan_schema(plan)
    if not gate_result.passed:
        log = _logger(state)
        log.warn("validate_plan_schema gate failed", feedback=gate_result.feedback)
        plan = {"steps": [{"step": 1, "action": raw or state.get("analysis_summary") or "Execute task"}]}

    return {
        "plan": plan,
        "skill_context": skill_context,
    }


async def dispatch_dev_agent(state: dict) -> dict:
    """Dispatch task to a dev agent (Web Dev, Android, etc.) via A2A tool.

    Passes all gathered context including workspace_paths so the dev agent
    does not re-fetch or guess file locations.

    Builds and attaches an ExecutionContract to the dispatch metadata,
    ensuring the child agent receives its allowed_tools and workflow config.
    """
    from framework.tools.registry import get_registry
    from framework.execution_contract import (
        build_execution_contract,
        load_child_profiles,
        permission_snapshot_from_permission_set,
        resolve_execution_contract_permission_set,
    )

    registry = get_registry()

    # Enforce agent launching permission
    perm_engine = registry._permission_engine
    if perm_engine:
        perm_engine.require_agent_launching("web-dev")

    log = _logger(state)
    log.node("dispatch_dev_agent")
    revision_feedback = state.get("revision_feedback", "")
    task_description = _build_dev_brief(state)
    definition_of_done = dict((state.get("plan", {}) or {}).get("definition_of_done", {}) or {})
    if not definition_of_done:
        definition_of_done = {
            "build_must_pass": True,
            "tests_must_pass": True,
            "self_assessment_required": True,
            "jira_state_management": True,
            "pr_required": True,
        }
    if "screenshot_required" not in definition_of_done:
        definition_of_done["screenshot_required"] = state.get("task_type", "") in (
            "feature", "ui", "frontend", "frontend_feature", "ui_feature",
        ) or bool(state.get("design_context") or state.get("design_code_path") or state.get("stitch_screen_id"))

    # Build execution contract for the child dev agent
    execution_contract = None
    child_permissions = None
    try:
        root = _Path(__file__).resolve().parents[2]
        child_profiles = load_child_profiles({
            "web-dev": str(root / "config" / "permissions" / "web-dev.yaml"),
        })
        execution_contract = build_execution_contract(
            profile=child_profiles["web-dev"],
            workflow_ref="config/workflows/development_task.yaml",
            rule_refs=[
                "config/rules/development_standards.yaml",
                "config/rules/code_quality.yaml",
                "config/rules/security.yaml",
            ],
            workspace_root=state.get("workspace_path", ""),
            definition_of_done=definition_of_done,
        )
        if not execution_contract.allowed_tools:
            raise ValueError("web-dev permission profile has no allowed_tools")
        _resolved_contract, child_permission_set = resolve_execution_contract_permission_set(
            "web-dev",
            execution_contract.to_dict(),
        )
        child_permissions = permission_snapshot_from_permission_set(child_permission_set)
        log.info("execution contract built", profile="web-dev",
                 tools_count=len(execution_contract.allowed_tools))
    except Exception as exc:
        log.error("execution contract build failed", error=str(exc))
        raise RuntimeError(f"Cannot dispatch Web Dev without a valid execution contract: {exc}") from exc

    try:
        dispatch_args = {
                "task_description": task_description,
                "jira_context": state.get("jira_context", {}),
                "design_context": state.get("design_context"),
                "design_code_path": state.get("design_code_path", ""),
                "repo_url": state.get("repo_url", ""),
                "repo_path": state.get("repo_path", ""),
                "workspace_path": state.get("workspace_path", ""),
                "context_manifest_path": state.get("context_manifest_path", ""),
                "jira_files": state.get("jira_files", []),
                "jira_local_folder": state.get("jira_local_folder", ""),
                "design_files": state.get("design_files", []),
                "design_local_folder": state.get("design_local_folder", ""),
                "design_md_path": state.get("design_md_path", ""),
                "tech_stack": state.get("tech_stack") or [],
                "stitch_screen_name": state.get("stitch_screen_name", ""),
                "orchestrator_task_id": state.get("_task_id", ""),
                "revision_feedback": revision_feedback,
                "definition_of_done": definition_of_done,
        }
        if execution_contract:
            dispatch_args["execution_contract"] = execution_contract.to_dict()
        if child_permissions:
            dispatch_args["permissions"] = child_permissions
        result_str = registry.execute_sync(
            "dispatch_web_dev",
            dispatch_args,
        )
        payload = json.loads(result_str) if result_str else {}
    except Exception as exc:
        log.error("dev dispatch failed", error=str(exc))
        print(f"[{_AGENT_ID}] Dev dispatch failed: {exc}")
        payload = {"status": "error", "message": str(exc)}

    pr_url = payload.get("prUrl", "")
    branch_name = payload.get("branch", "")
    jira_in_review = payload.get("jiraInReview", False)
    screenshot_included = bool(payload.get("screenshotIncluded") or payload.get("screenshot_included"))
    screenshot_uploaded = bool(payload.get("screenshotUploaded") or payload.get("screenshot_uploaded"))
    status = str(payload.get("status", "")).strip().lower()
    jira_required = bool(definition_of_done.get("jira_state_management")) and bool(state.get("jira_key"))
    missing_evidence: list[str] = []

    if status == "error":
        error_message = payload.get("message") or payload.get("error") or "Web Dev task failed"
        log.error("dev dispatch returned error", detail=error_message)
        raise RuntimeError(error_message)

    if definition_of_done.get("pr_required") and not pr_url:
        missing_evidence.append("prUrl")
    if jira_required and not jira_in_review:
        missing_evidence.append("jiraInReview")
    if definition_of_done.get("screenshot_required") and not screenshot_included:
        missing_evidence.append("screenshotIncluded")

    if missing_evidence:
        detail = ", ".join(missing_evidence)
        log.error("dev dispatch missing delivery evidence", missing=detail)
        raise RuntimeError(f"Web Dev completed without required delivery evidence: {detail}")

    log.info("dev dispatch result",
             status=payload.get("status", "?"), pr_url=pr_url,
             branch=branch_name, jira_in_review=jira_in_review)
    log.a2a("←", "web-dev", status=payload.get("status", "?"), pr_url=pr_url)
    print(
        f"[{_AGENT_ID}] Dev dispatch result: status={payload.get('status','?')} "
        f"prUrl={pr_url!r} branch={branch_name!r} jiraInReview={jira_in_review}"
    )
    if payload.get("error"):
        print(f"[{_AGENT_ID}] Dev dispatch error detail: {payload['error']}")

    return {
        "dev_dispatched": True,
        "dev_result": payload,
        "dev_agent_session": {
            "task_id": str(payload.get("childTaskId") or "").strip(),
            "service_url": str(payload.get("childServiceUrl") or "").strip(),
            "container_name": str(payload.get("childContainerName") or "").strip(),
            "agent_id": str(payload.get("childAgentId") or "web-dev").strip() or "web-dev",
        },
        "pr_url": pr_url,
        "pr_number": payload.get("prNumber") or payload.get("pr_number") or 0,
        "branch_name": branch_name,
        "jira_in_review": jira_in_review,
        "screenshot_included": screenshot_included,
        "screenshot_uploaded": screenshot_uploaded,
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
        from framework.execution_contract import (
            build_execution_contract,
            load_child_profiles,
            permission_snapshot_from_permission_set,
            resolve_execution_contract_permission_set,
        )

        root = _Path(__file__).resolve().parents[2]
        child_profiles = load_child_profiles({
            "code-review": str(root / "config" / "permissions" / "code-review.yaml"),
        })
        review_contract = build_execution_contract(
            profile=child_profiles["code-review"],
            workflow_ref="config/workflows/code_review_task.yaml",
            rule_refs=["config/rules/code_quality.yaml", "config/rules/security.yaml"],
            workspace_root=state.get("workspace_path", ""),
            definition_of_done={"critical_issue_blocks": True},
        )
        if not review_contract.allowed_tools:
            raise ValueError("code-review permission profile has no allowed_tools")
        _resolved_contract, review_permission_set = resolve_execution_contract_permission_set(
            "code-review",
            review_contract.to_dict(),
        )
        review_permissions = permission_snapshot_from_permission_set(review_permission_set)
        review_contract = review_contract.to_dict()
    except Exception as exc:
        raise RuntimeError(f"Cannot dispatch Code Review without a valid execution contract: {exc}") from exc

    try:
        result_str = registry.execute_sync(
            "dispatch_code_review",
            {
                "pr_url": pr_url,
                "pr_number": state.get("pr_number") or dev_result.get("prNumber") or dev_result.get("pr_number") or 0,
                "repo_url": state.get("repo_url", ""),
                "diff_summary": dev_result.get("summary", ""),
                "requirements": state.get("analysis_summary", "") or state.get("user_request", ""),
                "jira_context": state.get("jira_context", {}),
                "design_context": state.get("design_context"),
                "workspace_path": state.get("workspace_path", ""),
                "context_manifest_path": state.get("context_manifest_path", ""),
                "orchestrator_task_id": state.get("_task_id", ""),
                "task_id": state.get("_task_id", ""),
                "execution_contract": review_contract,
                "permissions": review_permissions,
            },
        )
        payload = json.loads(result_str) if result_str else {}
    except Exception as exc:
        print(f"[{_AGENT_ID}] Code review dispatch failed: {exc}")
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

    revision_feedback = "\n".join(feedback_lines) or "Code review rejected. Please fix issues."

    jira_key = str(state.get("jira_key") or (state.get("jira_context") or {}).get("key") or "")
    if jira_key:
        try:
            from framework.tools.registry import get_registry

            get_registry().execute_sync(
                "jira_comment",
                {
                    "ticket_key": jira_key,
                    "comment": "Code review requested a revision.\n\n" + revision_feedback[:3000],
                    "task_id": state.get("_task_id", ""),
                },
            )
        except Exception as exc:
            print(f"[{_AGENT_ID}] Jira review feedback comment failed: {exc}")

    cleanup = await _ack_and_cleanup_dev_agent(state)

    return {
        "revision_feedback": revision_feedback,
        "revision_count": state.get("revision_count", 0) + 1,
        **cleanup,
    }


async def report_success(state: dict) -> dict:
    """Build final success report."""
    pr_url = state.get("pr_url", "N/A")
    branch = state.get("branch_name", "N/A")
    analysis = state.get("analysis_summary", "")
    verdict = state.get("review_verdict", "approved")
    revision_count = state.get("revision_count", 0)
    dev_result = state.get("dev_result", {}) if isinstance(state.get("dev_result", {}), dict) else {}
    screenshot_included = bool(
        state.get("screenshot_included")
        or dev_result.get("screenshotIncluded")
        or dev_result.get("screenshot_included")
    )
    screenshot_uploaded = bool(
        state.get("screenshot_uploaded")
        or dev_result.get("screenshotUploaded")
        or dev_result.get("screenshot_uploaded")
    )

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
        tl_dir = os.path.join(workspace_path, _AGENT_ID)
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
                        "screenshot_included": screenshot_included,
                        "screenshot_uploaded": screenshot_uploaded,
                    },
                }, fh, ensure_ascii=False, indent=2)
        except OSError as exc:
            print(f"[{_AGENT_ID}] Failed to write final-report.json: {exc}")

    jira_key = str(state.get("jira_key") or (state.get("jira_context") or {}).get("key") or "")
    if jira_key:
        try:
            from framework.tools.registry import get_registry

            get_registry().execute_sync(
                "jira_comment",
                {
                    "ticket_key": jira_key,
                    "comment": f"Code review passed. PR is ready for merge: {pr_url}",
                    "task_id": state.get("_task_id", ""),
                },
            )
        except Exception as exc:
            print(f"[{_AGENT_ID}] Jira final review comment failed: {exc}")

    cleanup = await _ack_and_cleanup_dev_agent(state)

    return {
        "report_summary": report_summary,
        "success": True,
        "jira_in_review": state.get("jira_in_review", False),
        "screenshot_included": screenshot_included,
        "screenshot_uploaded": screenshot_uploaded,
        **cleanup,
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

    cleanup = await _ack_and_cleanup_dev_agent(state)
    if cleanup.get("dev_agent_session") == {}:
        state["dev_agent_session"] = {}

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

def _adf_extract_all(node, lines: list) -> None:
    """Recursively extract plain text and inlineCard/blockCard URLs from an ADF node.

    Jira stores embedded URLs (GitHub, Stitch, Figma, etc.) as inlineCard nodes
    rather than plain text.  This walker surfaces those URLs so the LLM can see them.
    """
    if isinstance(node, dict):
        node_type = node.get("type", "")
        if node_type in ("inlineCard", "blockCard", "embedCard"):
            url = node.get("attrs", {}).get("url", "")
            if url:
                lines.append(url)
            return
        if node_type == "text":
            text = node.get("text", "")
            if text.strip():
                lines.append(text.strip())
            return
        if node_type == "hardBreak":
            return
        for child in node.get("content", []):
            _adf_extract_all(child, lines)
    elif isinstance(node, list):
        for item in node:
            _adf_extract_all(item, lines)


def _adf_to_text(adf) -> str:
    """Convert an ADF document (dict or str) to plain readable text with URLs preserved."""
    if isinstance(adf, str):
        return adf
    if not isinstance(adf, dict):
        return ""
    lines: list = []
    _adf_extract_all(adf, lines)
    return " ".join(lines)


# Fields that carry important content for context extraction (checked first)
_IMPORTANT_JIRA_FIELDS = (
    "summary", "description", "acceptance_criteria",
    "customfield_10016",  # Acceptance Criteria (common Jira Cloud field ID)
    "customfield_10014",  # Story Points / Epic Link — often contains references
    "customfield_10058",  # varies by project — sometimes acceptance criteria
)

# Noisy system/metadata fields to skip (they inflate token count with no value)
_SKIP_JIRA_FIELDS = {
    "issuetype", "project", "avatarUrls", "iconUrl", "subtask",
    "statuscategory", "statusCategory", "statusCategoryChangeDate",
    "issuerestriction", "watches", "workratio", "aggregatetimespent",
    "timeestimate", "aggregatetimeoriginalestimate", "timespent",
    "aggregatetimeestimate", "timetracking", "resolutiondate", "lastViewed",
    "created", "updated", "priority", "fixVersions", "versions", "labels",
    "customfield_10019", "customfield_10021", "customfield_10033",
    "customfield_10035", "expand",
}


def _jira_to_text(jira_context: dict) -> str:
    """Flatten Jira ticket dict into a searchable text blob.

    Important fields (key, summary, description, acceptance criteria) are emitted
    FIRST so they fall within the LLM prompt window even when the ticket is large.
    ADF inlineCard/blockCard nodes are unwrapped to expose their embedded URLs.
    Noisy system metadata fields are skipped to reduce token count.
    """
    parts: list[str] = []

    # 1. Top-level identifiers
    for key in ("key", "summary", "url"):
        val = jira_context.get(key)
        if val and isinstance(val, str):
            parts.append(f"{key}: {val}")

    fields = jira_context.get("fields", jira_context)

    # 2. Important fields first — description, acceptance criteria
    for field_name in _IMPORTANT_JIRA_FIELDS:
        val = fields.get(field_name)
        if val is None:
            continue
        if isinstance(val, dict):
            text = _adf_to_text(val)
            if text:
                parts.append(f"{field_name}: {text}")
        elif isinstance(val, str) and val.strip():
            parts.append(f"{field_name}: {val.strip()}")

    # 3. All remaining string / dict / list fields (skip noisy metadata)
    for _k, val in fields.items():
        if _k in _SKIP_JIRA_FIELDS or _k in _IMPORTANT_JIRA_FIELDS:
            continue
        if isinstance(val, str) and val.strip():
            parts.append(val.strip())
        elif isinstance(val, dict):
            # Unwrap ADF if it looks like a doc node, otherwise compact JSON
            if val.get("type") == "doc":
                text = _adf_to_text(val)
                if text:
                    parts.append(text)
            else:
                # Compact JSON but still extract any inlineCard URLs
                url_lines: list = []
                _adf_extract_all(val, url_lines)
                if url_lines:
                    parts.extend(url_lines)
                else:
                    compact = json.dumps(val, ensure_ascii=False)
                    if len(compact) < 300:
                        parts.append(compact)
        elif isinstance(val, list):
            url_lines = []
            _adf_extract_all(val, url_lines)
            if url_lines:
                parts.extend(url_lines)
            else:
                flat = " ".join(str(v) for v in val if v)
                if flat.strip():
                    parts.append(flat)

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
        print(f"[{_AGENT_ID}] No runtime available for LLM extraction — using regex fallback")
        return _extract_urls_from_ticket(jira_context)

    from agents.team_lead.prompts.extraction import EXTRACTION_SYSTEM, EXTRACTION_TEMPLATE

    jira_text = _jira_to_text(jira_context)
    prompt = EXTRACTION_TEMPLATE.format(jira_text=jira_text[:8000])

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
        print(f"[{_AGENT_ID}] LLM extraction result: {json.dumps(cleaned, ensure_ascii=False)}")
        return cleaned
    except Exception as exc:
        print(f"[{_AGENT_ID}] LLM extraction failed ({exc}) — falling back to regex")
        return _extract_urls_from_ticket(jira_context)


