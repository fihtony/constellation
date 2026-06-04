"""Team Lead Agent workflow nodes.

Architecture: **Graph outside, ReAct inside**.

Each node is an async function that receives the workflow state dict and returns
a dict of state updates.  Nodes that need open-ended reasoning use the runtime
for single-shot LLM calls or bounded ReAct; the graph controls the macro flow.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
import re
import threading
import time
from pathlib import Path as _Path
from typing import Any

from framework.config import load_agent_config as _load_agent_cfg
from framework.devlog import AgentLogger
from framework.major_step import (
    LIFECYCLE_DONE,
    LIFECYCLE_FAILED,
    LIFECYCLE_RUNNING,
    LIFECYCLE_WAITING_FOR_USER,
    record_major_step,
)
from framework.audit_log import (
    append_command_log as _append_command_log,
    write_stage_summary as _write_stage_summary,
)

# Load agent_id from config.yaml — single source of truth for identity
_AGENT_ID: str = _load_agent_cfg(
    _Path(__file__).parent.name.replace("_", "-")
).get("agent_id", _Path(__file__).parent.name.replace("_", "-"))

_CHILD_SESSION_CACHE_LOCK = threading.Lock()
_CHILD_SESSION_CACHE: dict[str, dict[str, dict[str, str]]] = {}


def _logger(state: dict) -> AgentLogger:
    """Return an AgentLogger for this agent using the task_id stored in state."""
    return AgentLogger(state.get("_task_id", ""), _AGENT_ID)


def _record_timeline_step(
    state: dict,
    *,
    step_key: str,
    title: str,
    lifecycle_state: str = LIFECYCLE_RUNNING,
    summary_template: str = "",
    summary_facts: dict | None = None,
    round: int = 0,
    conditional: bool = False,
) -> None:
    task_id = state.get("_compass_task_id") or state.get("_task_id") or state.get("task_id") or ""
    if not task_id:
        return
    try:
        record_major_step(
            task_id,
            step_key=step_key,
            title=title,
            agent="team-lead",
            lifecycle_state=lifecycle_state,
            summary_template=summary_template,
            summary_facts=summary_facts,
            round=round,
            conditional=conditional,
            orchestrator_task_id=state.get("_compass_task_id") or task_id,
            progress_sink=state.get("_major_step_progress_sink"),
            task_store=state.get("_task_store"),
        )
    except Exception as exc:  # noqa: BLE001
        _logger(state).debug("major-step write skipped", step_key=step_key, error=str(exc))


def _audit_command(state: dict, action: str, **params: Any) -> None:
    """Append a row to ``<workspace>/team-lead/command-log.txt``.

    Audit-log writes are best-effort and silently ignored when the
    workspace is unset (e.g. during unit tests with in-memory state).
    """
    workspace_path = state.get("workspace_path", "")
    if not workspace_path:
        return
    _append_command_log(
        workspace_path,
        _AGENT_ID,
        action,
        params={k: v for k, v in params.items() if v is not None},
        step_id=state.get("revision_count", 0) or None,
    )


def _audit_stage(
    state: dict,
    stage: str,
    *,
    completed_steps: list[Any] | None = None,
    pending_steps: list[Any] | None = None,
    warnings: list[Any] | None = None,
    errors: list[Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Overwrite ``<workspace>/team-lead/stage-summary.json``."""
    workspace_path = state.get("workspace_path", "")
    if not workspace_path:
        return
    _write_stage_summary(
        workspace_path,
        _AGENT_ID,
        stage,
        completed_steps=completed_steps,
        pending_steps=pending_steps,
        warnings=warnings,
        errors=errors,
        extra=extra,
    )


def _normalize_child_session(session: Any, default_agent_id: str) -> dict[str, str]:
    if not isinstance(session, dict):
        return {}

    normalized = {
        "task_id": str(session.get("task_id") or "").strip(),
        "service_url": str(session.get("service_url") or "").strip(),
        "container_name": str(session.get("container_name") or "").strip(),
        "agent_id": str(session.get("agent_id") or default_agent_id).strip() or default_agent_id,
    }
    return normalized if any(normalized.values()) else {}


def _get_cached_child_session(orchestrator_task_id: str, child_agent_id: str) -> dict[str, str]:
    task_key = str(orchestrator_task_id or "").strip()
    agent_key = str(child_agent_id or "").strip()
    if not task_key or not agent_key:
        return {}
    with _CHILD_SESSION_CACHE_LOCK:
        cached = (_CHILD_SESSION_CACHE.get(task_key) or {}).get(agent_key)
        return dict(cached) if isinstance(cached, dict) else {}


def _cache_child_session(orchestrator_task_id: str, child_agent_id: str, session: Any) -> dict[str, str]:
    task_key = str(orchestrator_task_id or "").strip()
    agent_key = str(child_agent_id or "").strip()
    normalized = _normalize_child_session(session, agent_key or "unknown-agent")
    if not task_key or not agent_key or not normalized:
        return {}
    with _CHILD_SESSION_CACHE_LOCK:
        _CHILD_SESSION_CACHE.setdefault(task_key, {})[agent_key] = dict(normalized)
    return normalized


def _clear_cached_child_sessions(orchestrator_task_id: str, child_agent_id: str = "") -> None:
    task_key = str(orchestrator_task_id or "").strip()
    agent_key = str(child_agent_id or "").strip()
    if not task_key:
        return
    with _CHILD_SESSION_CACHE_LOCK:
        if agent_key:
            agent_sessions = _CHILD_SESSION_CACHE.get(task_key) or {}
            agent_sessions.pop(agent_key, None)
            if not agent_sessions:
                _CHILD_SESSION_CACHE.pop(task_key, None)
            return
        _CHILD_SESSION_CACHE.pop(task_key, None)


def _capability_launch_port(capability: str, default_port: int = 8000) -> int:
    """Return the configured per-task port for a capability."""
    try:
        from framework.registry_client import RegistryClient

        definition = RegistryClient.from_config().get_capability_definition(capability) or {}
        launch_spec = definition.get("launch_spec") or definition.get("launchSpec") or {}
        raw_port = launch_spec.get("port", default_port)
        port = int(raw_port)
        return port if port > 0 else default_port
    except Exception:
        return default_port


# ---------------------------------------------------------------------------
# Dev agent_type resolution
# ---------------------------------------------------------------------------
#
# The team-lead routes implementation work to a "dev agent".  Constellation
# can register multiple dev surfaces (web-dev, android-dev, etc.).  We must:
#   1. Discover which dev capabilities are registered (capability registry is
#      the source of truth — never hard-code agent URLs or capabilities).
#   2. Map the analyzed task_type onto one of those discovered capabilities.
#   3. Fall back to a registered default when the LLM did not specify, or
#      when the suggested agent_type is not registered.
#
# The capability convention is "<agent-id>.task.execute" — discovered by
# scanning the registry-wide capability list for the ".task.execute" suffix
# and excluding non-development executors such as office.
#
# task_type → agent_type hints can be extended freely; the resolver always
# checks the registry before returning the value, so unsupported hints
# silently fall back to the registered default.

# Heuristic mapping from analyzed task_type strings to a preferred dev
# agent identifier.  Only the agent_type is hinted here — the resolver
# verifies the hint against the registry before accepting it.
_DEV_AGENT_TYPE_BY_TASK_TYPE: dict[str, str] = {
    "frontend_feature": "web-dev",
    "frontend": "web-dev",
    "ui": "web-dev",
    "ui_feature": "web-dev",
    "backend_feature": "web-dev",
    "backend": "web-dev",
    "bug_fix": "web-dev",
    "refactor": "web-dev",
    "general": "web-dev",
    "android": "android-dev",
    "android_feature": "android-dev",
    "mobile": "android-dev",
}

# Capabilities advertised by non-development executors that should be
# excluded when scanning registered "*.task.execute" capabilities.
_NON_DEV_TASK_EXECUTE_CAPABILITIES: frozenset[str] = frozenset({
    "office.task.execute",
})


def _registered_dev_agent_types() -> list[str]:
    """Return the dev agent ids registered with a ``<agent>.task.execute`` capability."""
    try:
        from framework.registry_client import RegistryClient

        client = RegistryClient.from_config()
    except Exception:
        return []

    discovered: list[str] = []
    seen: set[str] = set()
    # We scan the small set of dev-capable agents that the team-lead may
    # currently launch.  This is a registry-derived list — never hard-code
    # an agent URL or bypass the registry; only the *names* of the
    # capabilities we look up are static, and any agent advertising them
    # is discovered dynamically.
    for capability in ("web-dev.task.execute", "android-dev.task.execute"):
        try:
            if not client.has_capability(capability):
                continue
            definitions = client.query_capability(capability) or []
        except Exception:
            continue
        for definition in definitions:
            if not isinstance(definition, dict):
                continue
            agent_id = str(
                definition.get("agent_id")
                or definition.get("agentId")
                or ""
            ).strip()
            if not agent_id or agent_id in seen:
                continue
            seen.add(agent_id)
            discovered.append(agent_id)
    return discovered


def _resolve_dev_agent_type(
    *,
    task_type: str,
    suggested: str = "",
) -> str:
    """Resolve the dev agent_type for a development task.

    Resolution order:
      1. LLM-suggested value (from the plan), if it matches a registered
         dev agent.
      2. Heuristic mapping from analyzed task_type → preferred agent,
         if that agent is registered.
      3. First registered dev agent (capability registry is the source of
         truth).
      4. Empty string when nothing is registered — callers must treat this
         as a fatal configuration error.
    """
    registered = [
        agent_id for agent_id in _registered_dev_agent_types()
        if f"{agent_id}.task.execute" not in _NON_DEV_TASK_EXECUTE_CAPABILITIES
    ]
    if not registered:
        return ""

    suggested_clean = (suggested or "").strip().lower()
    if suggested_clean and suggested_clean in registered:
        return suggested_clean

    hint = _DEV_AGENT_TYPE_BY_TASK_TYPE.get(
        (task_type or "").strip().lower(),
        "",
    )
    if hint and hint in registered:
        return hint

    return registered[0]


def _reuse_live_child_session(
    *,
    agent_id: str,
    capability: str,
    orchestrator_task_id: str,
    log: AgentLogger,
) -> dict[str, str]:
    """Reuse the first live child instance for the orchestrator task when present."""
    if not orchestrator_task_id:
        return {}

    try:
        from framework.launcher import get_launcher

        live_instances = get_launcher().find_live_instances(agent_id, orchestrator_task_id)
    except Exception as exc:
        log.warn(
            "duplicate instance check failed (non-fatal)",
            agent_id=agent_id,
            task_id=orchestrator_task_id,
            error=str(exc),
        )
        return {}

    if not live_instances:
        return {}

    container_names = [str(item.get("container_name") or "").strip() for item in live_instances]
    first_container = next((name for name in container_names if name), "")
    if not first_container:
        return {}

    port = _capability_launch_port(capability)
    service_url = f"http://{first_container}:{port}"
    log.warn(
        "duplicate live instance detected",
        agent_id=agent_id,
        task_id=orchestrator_task_id,
        existing_containers=[name for name in container_names if name],
    )
    log.info(
        "reusing detected live instance",
        agent_id=agent_id,
        container=first_container,
        service_url=service_url,
    )
    return {
        "agent_id": agent_id,
        "container_name": first_container,
        "service_url": service_url,
    }


def _keepalive_interval_seconds() -> float:
    raw_value = str(os.environ.get("TEAM_LEAD_CHILD_KEEPALIVE_INTERVAL_SECONDS", "240")).strip()
    try:
        interval = float(raw_value)
        return interval if interval > 0 else 240.0
    except ValueError:
        return 240.0


@contextmanager
def _keep_child_sessions_alive(
    log: AgentLogger,
    sessions: list[dict[str, Any] | None],
    *,
    estimated_remaining_wait_seconds: int = 900,
):
    """Ping waiting child sessions while Team Lead blocks on downstream work."""
    keepalive_targets: list[dict[str, str]] = []
    for session in sessions:
        if not isinstance(session, dict):
            continue
        base_url = str(session.get("service_url") or "").strip()
        task_id = str(session.get("task_id") or "").strip()
        if not base_url or not task_id:
            continue
        keepalive_targets.append(
            {
                "agent_id": str(session.get("agent_id") or "unknown-agent").strip() or "unknown-agent",
                "service_url": base_url,
                "task_id": task_id,
            }
        )

    if not keepalive_targets:
        yield
        return

    stop_event = threading.Event()
    interval_seconds = _keepalive_interval_seconds()

    def _run_keepalive_loop() -> None:
        import asyncio

        from framework.a2a.client import A2AClient

        while not stop_event.wait(interval_seconds):
            for target in keepalive_targets:
                try:
                    asyncio.run(
                        A2AClient(timeout=10).send_ping(
                            target["service_url"],
                            target["task_id"],
                            estimated_remaining_wait_seconds=estimated_remaining_wait_seconds,
                        )
                    )
                    log.a2a(
                        "→",
                        target["agent_id"],
                        action="ping",
                        child_task_id=target["task_id"],
                    )
                except Exception as exc:
                    log.warn(
                        "child keepalive ping failed",
                        agent_id=target["agent_id"],
                        child_task_id=target["task_id"],
                        error=str(exc),
                    )

    worker = threading.Thread(target=_run_keepalive_loop, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stop_event.set()
        worker.join(timeout=1.0)


async def _ack_and_cleanup_dev_agent(
    state: dict,
    *,
    exit_reason: str = "task_completed_success",
) -> dict[str, Any]:
    """Acknowledge child tasks and tear down any per-task instances.

    Also sends ACK to the Code Review agent if a session exists.
    Per Section 17: ACK is sent to both Dev and CR simultaneously.
    Container destroy is best-effort after ACK — agents should self-exit on ACK.
    """
    log = _logger(state)
    result: dict[str, Any] = {}

    # --- Dev Agent ACK + cleanup ---
    session = state.get("dev_agent_session") or {}
    if not isinstance(session, dict):
        session = {}

    child_task_id = str(session.get("task_id") or "").strip()
    child_service_url = str(session.get("service_url") or "").strip()
    child_container_name = str(session.get("container_name") or "").strip()
    child_agent_id = str(session.get("agent_id") or "web-dev").strip() or "web-dev"

    dev_acknowledged = False
    dev_cleaned_up = False

    if child_task_id and child_service_url:
        try:
            from framework.a2a.client import A2AClient

            await A2AClient(timeout=10).send_ack(
                child_service_url,
                child_task_id,
                exit_reason=exit_reason,
                orchestrator_task_id=state.get("_task_id", ""),
            )
            dev_acknowledged = True
            log.a2a("→", child_agent_id, action="ack", child_task_id=child_task_id)
        except Exception as exc:
            log.warn("dev agent ack failed", error=str(exc), child_task_id=child_task_id)

    # Best-effort container cleanup after ACK (agent should self-exit, but safety net)
    if child_container_name:
        try:
            from framework.launcher import get_launcher

            get_launcher().destroy_instance(child_agent_id, child_container_name)
            dev_cleaned_up = True
            log.info("dev agent instance destroyed", agent_id=child_agent_id, container_name=child_container_name)
        except Exception as exc:
            log.warn("dev agent cleanup failed (agent may have already exited)", error=str(exc), container_name=child_container_name)

    result["dev_agent_acknowledged"] = dev_acknowledged
    result["dev_agent_cleaned_up"] = dev_cleaned_up
    result["dev_agent_session"] = {}
    _clear_cached_child_sessions(state.get("_task_id", ""), child_agent_id)

    # --- CR Agent ACK + cleanup ---
    cr_session = state.get("cr_agent_session") or {}
    if not isinstance(cr_session, dict):
        cr_session = {}

    cr_task_id = str(cr_session.get("task_id") or "").strip()
    cr_service_url = str(cr_session.get("service_url") or "").strip()
    cr_container_name = str(cr_session.get("container_name") or "").strip()
    cr_agent_id = str(cr_session.get("agent_id") or "code-review").strip() or "code-review"

    cr_acknowledged = False
    cr_cleaned_up = False

    if cr_task_id and cr_service_url:
        try:
            from framework.a2a.client import A2AClient

            await A2AClient(timeout=10).send_ack(
                cr_service_url,
                cr_task_id,
                exit_reason=exit_reason,
                orchestrator_task_id=state.get("_task_id", ""),
            )
            cr_acknowledged = True
            log.a2a("→", cr_agent_id, action="ack", child_task_id=cr_task_id)
        except Exception as exc:
            log.warn("cr agent ack failed", error=str(exc), child_task_id=cr_task_id)

    if cr_container_name:
        try:
            from framework.launcher import get_launcher

            get_launcher().destroy_instance(cr_agent_id, cr_container_name)
            cr_cleaned_up = True
            log.info("cr agent instance destroyed", agent_id=cr_agent_id, container_name=cr_container_name)
        except Exception as exc:
            log.warn("cr agent cleanup failed (agent may have already exited)", error=str(exc), container_name=cr_container_name)

    result["cr_agent_acknowledged"] = cr_acknowledged
    result["cr_agent_cleaned_up"] = cr_cleaned_up
    result["cr_agent_session"] = {}
    _clear_cached_child_sessions(state.get("_task_id", ""), cr_agent_id)

    return result


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
    _audit_command(state, "receive_task", jira_key=jira_key)
    _audit_stage(
        state,
        "receive_task",
        completed_steps=["receive_task"],
        pending_steps=["analyze_requirements", "gather_context", "create_plan", "dispatch_dev_agent"],
        extra={"jira_key": jira_key},
    )

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
    _audit_command(state, "analyze_requirements")
    _record_timeline_step(
        state,
        step_key="tl.analyzing",
        title="Team Lead analyzing task",
        summary_template="Team Lead is analyzing the Jira task requirements.",
    )

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

    _record_timeline_step(
        state,
        step_key="tl.analyzing",
        title="Team Lead analyzing task",
        lifecycle_state=LIFECYCLE_DONE,
        summary_template="Team Lead analyzed the task as {task_type} work with {complexity} complexity.",
        summary_facts={
            "task_type": analysis.get("task_type", "general"),
            "complexity": analysis.get("complexity", "medium"),
        },
    )

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
    _audit_command(state, "gather_context", jira_key=state.get("jira_key", ""))
    _record_timeline_step(
        state,
        step_key="tl.gathering",
        title="Team Lead gathering context",
        summary_template="Team Lead is gathering Jira, design, and repository context.",
    )

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

    # Auto-detect tech stack from cloned repo if not yet determined
    if not tech_stack and repo_path and os.path.isdir(repo_path):
        try:
            from framework.standards_loader import detect_tech_stack_from_repo
            tech_stack = detect_tech_stack_from_repo(repo_path)
            if tech_stack:
                log.info("auto-detected tech_stack from repo", tech_stack=tech_stack)
                print(f"[{_AGENT_ID}] Auto-detected tech_stack: {tech_stack}")
        except Exception:
            pass


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

    _record_timeline_step(
        state,
        step_key="tl.gathering",
        title="Team Lead gathering context",
        lifecycle_state=LIFECYCLE_DONE,
        summary_template="Team Lead gathered context: jira={has_jira}, design={has_design}, repo={has_repo}.",
        summary_facts={
            "has_jira": bool(jira_context),
            "has_design": bool(design_context),
            "has_repo": bool(repo_url),
        },
    )

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
    _audit_command(state, "validate_readiness")

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
    _audit_command(state, "create_plan", task_type=state.get("task_type", "general"))

    if not runtime:
        fallback_agent_type = _resolve_dev_agent_type(
            task_type=state.get("task_type", "general"),
        ) or "web-dev"
        return {
            "plan": {
                "agent_type": fallback_agent_type,
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

    # Resolve the primary dev agent_type via the capability registry.
    # The LLM-suggested value (if any) is honoured only when it matches a
    # registered dev agent — otherwise we fall back to a task-type hint or
    # the first registered dev agent.  This keeps planning blind to any
    # specific test task while still producing a stable, registry-backed
    # ``agent_type`` field expected by downstream tooling.
    suggested_agent_type = ""
    if isinstance(plan.get("agent_type"), str):
        suggested_agent_type = plan.get("agent_type", "")
    resolved_agent_type = _resolve_dev_agent_type(
        task_type=state.get("task_type", "general"),
        suggested=suggested_agent_type,
    )
    if resolved_agent_type:
        plan["agent_type"] = resolved_agent_type

    # Build skill context
    skills_registry = state.get("_skills_registry")
    required = state.get("required_skills", [])
    skill_context = ""
    if skills_registry and required:
        skill_context = skills_registry.build_prompt_context(required)

    # Validation gate: ensure plan has required structure BEFORE writing to disk,
    # so the persisted delivery-plan.json always satisfies the schema (incl. the
    # registry-resolved ``agent_type`` field).
    from framework.validation_gates import validate_plan_schema
    gate_result = validate_plan_schema(plan)
    if not gate_result.passed:
        log = _logger(state)
        log.warn("validate_plan_schema gate failed", feedback=gate_result.feedback)
        fallback_agent_type = resolved_agent_type or "web-dev"
        plan = {
            "agent_type": fallback_agent_type,
            "steps": [
                {
                    "step": 1,
                    "action": raw or state.get("analysis_summary") or "Execute task",
                    "agent": fallback_agent_type,
                }
            ],
        }

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
    _audit_command(
        state,
        "dispatch_dev_agent",
        revision_round=state.get("revision_count", 0) + 1,
        agent_type=state.get("dev_agent_type", "")
            or (state.get("plan") or {}).get("agent_type", "web-dev"),
    )
    _audit_stage(
        state,
        "dispatch_dev_agent",
        completed_steps=[
            "receive_task",
            "analyze_requirements",
            "gather_context",
            "validate_readiness",
            "create_plan",
        ],
        pending_steps=["dispatch_dev_agent", "review_result", "report_success"],
        extra={"revision_round": state.get("revision_count", 0) + 1},
    )
    revision_feedback = state.get("revision_feedback", "")
    if not revision_feedback:
        _record_timeline_step(
            state,
            step_key="tl.dispatched_dev",
            title="Team Lead dispatching to Web Dev",
            summary_template="Team Lead is dispatching the implementation task to Web Dev.",
        )
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
        orchestrator_task_id = str(state.get("_task_id", "")).strip()
        # For revision cycles, reuse the existing container instead of launching a new one.
        # Per architecture spec: "Team Lead reuses the same dev-agent container (same service
        # URL, new task ID via POST /message:send). It does NOT launch a new container."
        existing_session = _normalize_child_session(state.get("dev_agent_session"), "web-dev")
        if not existing_session:
            existing_session = _get_cached_child_session(orchestrator_task_id, "web-dev")
            if existing_session:
                log.info(
                    "using cached web-dev child session",
                    child_task_id=existing_session.get("task_id", ""),
                    service_url=existing_session.get("service_url", ""),
                )
        existing_service_url = str(existing_session.get("service_url") or "").strip()
        existing_container_name = str(existing_session.get("container_name") or "").strip()

        # Duplicate instance prevention: ensure no other live container exists
        # for the same orchestratorTaskId to prevent resource leaks.
        if not existing_service_url:
            live_session = _reuse_live_child_session(
                agent_id="web-dev",
                capability="web-dev.task.execute",
                orchestrator_task_id=orchestrator_task_id,
                log=log,
            )
            if live_session:
                _cache_child_session(orchestrator_task_id, "web-dev", live_session)
            existing_service_url = live_session.get("service_url", "")
            existing_container_name = live_session.get("container_name", "")

        dispatch_args = {
                "task_description": task_description,
                "jira_context": state.get("jira_context", {}),
                "design_context": state.get("design_context"),
                "design_code_path": state.get("design_code_path", ""),
                "repo_url": state.get("repo_url", ""),
                "repo_path": state.get("repo_path", ""),
                "branch_name": state.get("branch_name", ""),
                "workspace_path": state.get("workspace_path", ""),
                "context_manifest_path": state.get("context_manifest_path", ""),
                "jira_files": state.get("jira_files", []),
                "jira_local_folder": state.get("jira_local_folder", ""),
                "design_files": state.get("design_files", []),
                "design_local_folder": state.get("design_local_folder", ""),
                "design_md_path": state.get("design_md_path", ""),
                "tech_stack": state.get("tech_stack") or [],
                "stitch_screen_name": state.get("stitch_screen_name", ""),
                "orchestrator_task_id": orchestrator_task_id,
                "revision_feedback": revision_feedback,
                "review_report_path": state.get("review_report_path", ""),
                "revision_mode": bool(revision_feedback),
                "revision_round": state.get("revision_count", 0) + 1 if revision_feedback else 0,
                "existing_pr_url": state.get("pr_url", ""),
                "existing_pr_number": state.get("pr_number", 0),
                "definition_of_done": definition_of_done,
                # Pass existing container info so the tool can reuse it for revisions
                # instead of launching a fresh container every cycle.
                "child_service_url": existing_service_url,
                "child_container_name": existing_container_name,
        }
        if execution_contract:
            dispatch_args["execution_contract"] = execution_contract.to_dict()
        if child_permissions:
            dispatch_args["permissions"] = child_permissions
        if existing_service_url:
            log.info("reusing existing web-dev container for revision",
                     service_url=existing_service_url, container=existing_container_name)
        # Emit the tool name so downstream log auditors can verify the
        # team-lead invoked the dev-dispatch tool for this task.
        log.info(
            "invoking tool",
            tool="dispatch_web_dev",
            agent_type=state.get("dev_agent_type", "web-dev"),
            revision_round=state.get("revision_count", 0) + 1 if revision_feedback else 1,
        )
        with _keep_child_sessions_alive(log, [state.get("cr_agent_session")]):
            result_str = registry.execute_sync(
                "dispatch_web_dev",
                dispatch_args,
            )
        payload = json.loads(result_str) if result_str else {}
    except Exception as exc:
        if not revision_feedback:
            _record_timeline_step(
                state,
                step_key="tl.dispatched_dev",
                title="Team Lead dispatching to Web Dev",
                lifecycle_state=LIFECYCLE_FAILED,
                summary_template="Team Lead failed to dispatch Web Dev: {error}.",
                summary_facts={"error": str(exc)[:500]},
            )
        log.error("dev dispatch failed", error=str(exc))
        print(f"[{_AGENT_ID}] Dev dispatch failed: {exc}")
        # Container crash recovery: check if workspace has partial progress
        workspace_path = state.get("workspace_path", "")
        if workspace_path:
            web_dev_dir = os.path.join(workspace_path, "web-dev")
            pr_evidence_path = os.path.join(web_dev_dir, "pr-evidence.json")
            if os.path.isfile(pr_evidence_path):
                # Dev agent produced PR before crashing — recover evidence
                try:
                    with open(pr_evidence_path, encoding="utf-8") as _f:
                        evidence = json.load(_f)
                    recovered_pr = evidence.get("data", evidence).get("pr_url", "")
                    if recovered_pr:
                        log.info("recovered PR evidence from crashed dev agent",
                                 pr_url=recovered_pr)
                        payload = {
                            "status": "completed",
                            "prUrl": recovered_pr,
                            "branch": evidence.get("data", evidence).get("branch", ""),
                            "jiraInReview": evidence.get("data", evidence).get("jira_in_review", False),
                            "screenshotIncluded": evidence.get("data", evidence).get("screenshot_included", False),
                            "screenshotUploaded": evidence.get("data", evidence).get("screenshot_uploaded", False),
                            "recovered_from_crash": True,
                        }
                except Exception:
                    pass
        if not payload or payload.get("status") == "error":
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

    if not revision_feedback:
        _record_timeline_step(
            state,
            step_key="tl.dispatched_dev",
            title="Team Lead dispatching to Web Dev",
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Team Lead dispatched the task to Web Dev and received delivery evidence.",
            summary_facts={"has_pr": bool(pr_url), "branch": branch_name or ""},
        )

    next_dev_session = _cache_child_session(
        orchestrator_task_id,
        "web-dev",
        {
            "task_id": str(payload.get("childTaskId") or "").strip(),
            "service_url": str(payload.get("childServiceUrl") or "").strip(),
            "container_name": str(payload.get("childContainerName") or "").strip(),
            "agent_id": str(payload.get("childAgentId") or "web-dev").strip() or "web-dev",
        },
    )

    return {
        "dev_dispatched": True,
        "dev_result": payload,
        "dev_agent_session": next_dev_session or existing_session,
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
    log = _logger(state)
    log.node("review_result")
    review_attempt = int(state.get("revision_count", 0) or 0)
    review_round = max(review_attempt - 1, 0) if review_attempt > 0 else 0
    review_step_key = "tl.re_requesting_review" if review_attempt > 0 else "tl.requesting_review"
    review_title = (
        "Team Lead requesting follow-up code review"
        if review_attempt > 0
        else "Team Lead requesting code review"
    )
    _record_timeline_step(
        state,
        step_key=review_step_key,
        title=review_title,
        summary_template="Team Lead is requesting a code review of the current PR.",
        round=review_round,
        conditional=review_attempt > 0,
    )
    _audit_command(
        state,
        "review_result",
        review_round=state.get("revision_count", 0) + 1,
        pr_url=state.get("pr_url", ""),
    )
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
        orchestrator_task_id = str(state.get("_task_id", "")).strip()
        existing_cr_session = _normalize_child_session(state.get("cr_agent_session"), "code-review")
        if not existing_cr_session:
            existing_cr_session = _get_cached_child_session(orchestrator_task_id, "code-review")
            if existing_cr_session:
                log.info(
                    "using cached code-review child session",
                    child_task_id=existing_cr_session.get("task_id", ""),
                    service_url=existing_cr_session.get("service_url", ""),
                )
        existing_cr_service_url = str(existing_cr_session.get("service_url") or "").strip()
        existing_cr_container_name = str(existing_cr_session.get("container_name") or "").strip()
        if not existing_cr_service_url:
            live_cr_session = _reuse_live_child_session(
                agent_id="code-review",
                capability="review.code.check",
                orchestrator_task_id=orchestrator_task_id,
                log=log,
            )
            if live_cr_session:
                _cache_child_session(orchestrator_task_id, "code-review", live_cr_session)
            existing_cr_service_url = live_cr_session.get("service_url", "")
            existing_cr_container_name = live_cr_session.get("container_name", "")

        review_repo_url = (
            state.get("repo_url", "")
            or dev_result.get("repoUrl", "")
            or dev_result.get("repo_url", "")
        )
        review_changed_files = (
            dev_result.get("changedFiles")
            or dev_result.get("changed_files")
            or []
        )
        # Emit the tool name so downstream log auditors can verify the
        # team-lead invoked the code-review dispatch tool for this task.
        log.info(
            "invoking tool",
            tool="dispatch_code_review",
            pr_number=state.get("pr_number") or dev_result.get("prNumber") or dev_result.get("pr_number") or 0,
            review_round=state.get("revision_count", 0) + 1,
        )
        with _keep_child_sessions_alive(log, [state.get("dev_agent_session")]):
            result_str = registry.execute_sync(
                "dispatch_code_review",
                {
                    "pr_url": pr_url,
                    "pr_number": state.get("pr_number") or dev_result.get("prNumber") or dev_result.get("pr_number") or 0,
                    "repo_url": review_repo_url,
                    "changed_files": review_changed_files,
                    "diff_summary": dev_result.get("summary", ""),
                    "requirements": state.get("analysis_summary", "") or state.get("user_request", ""),
                    "jira_context": state.get("jira_context", {}),
                    "design_context": state.get("design_context"),
                    "workspace_path": state.get("workspace_path", ""),
                    "context_manifest_path": state.get("context_manifest_path", ""),
                    "orchestrator_task_id": orchestrator_task_id,
                    "task_id": orchestrator_task_id,
                    "execution_contract": review_contract,
                    "permissions": review_permissions,
                    "repo_path": state.get("repo_path", ""),
                    "review_round": state.get("revision_count", 0) + 1,
                    "previous_review_path": (
                        f"code-review/review-report-{state.get('revision_count', 0)}.json"
                        if state.get("revision_count", 0) > 0 else None
                    ),
                    "tech_stack": state.get("tech_stack") or [],
                    # Reuse existing CR container for re-review rounds.
                    "child_service_url": existing_cr_service_url,
                    "child_container_name": existing_cr_container_name,
                },
            )
        payload = json.loads(result_str) if result_str else {}
    except Exception as exc:
        _record_timeline_step(
            state,
            step_key=review_step_key,
            title=review_title,
            lifecycle_state=LIFECYCLE_FAILED,
            summary_template="Team Lead failed to complete the code-review request: {error}.",
            summary_facts={"error": str(exc)[:500]},
            round=review_round,
            conditional=review_attempt > 0,
        )
        print(f"[{_AGENT_ID}] Code review dispatch failed: {exc}")
        payload = {"verdict": "error", "message": str(exc)}

    # Persist CR session from the dispatch result so we can reuse the container
    cr_session = {}
    cr_launch = payload.pop("_crSession", None)
    if isinstance(cr_launch, dict) and cr_launch.get("service_url"):
        cr_session = _cache_child_session(orchestrator_task_id, "code-review", {
            "task_id": cr_launch.get("task_id", ""),
            "service_url": cr_launch.get("service_url", ""),
            "container_name": cr_launch.get("container_name", ""),
            "agent_id": cr_launch.get("agent_id", "code-review"),
        })

    verdict = payload.get("verdict", "rejected")
    revision_count = state.get("revision_count", 0)
    manual_review_required = bool(payload.get("manual_review_required", False))

    if verdict == "approved":
        route = "approved"
    elif manual_review_required:
        route = "need_user_input"
    elif revision_count >= state.get("max_revisions", 3):
        route = "need_user_input"
    else:
        route = "needs_revision"

    review_lifecycle = LIFECYCLE_FAILED if verdict == "error" else LIFECYCLE_DONE
    _record_timeline_step(
        state,
        step_key=review_step_key,
        title=review_title,
        lifecycle_state=review_lifecycle,
        summary_template="Team Lead completed the code-review request with verdict {verdict}.",
        summary_facts={"verdict": verdict},
        round=review_round,
        conditional=review_attempt > 0,
    )

    return {
        "review_result": payload,
        "review_verdict": verdict,
        "manual_review_required": manual_review_required,
        "route": route,
        # Persist CR agent session for container reuse on next review round
        **({"cr_agent_session": cr_session} if cr_session else {}),
    }


async def request_revision(state: dict) -> dict:
    """Prepare revision feedback for the dev agent and loop back."""
    log = _logger(state)
    review = state.get("review_result", {})
    comments = review.get("comments", [])
    summary = review.get("summary", review.get("message", ""))

    feedback_lines = []
    if summary:
        feedback_lines.append(f"Review summary: {summary}")
    for c in comments[:10]:  # Limit to top 10 comments
        feedback_lines.append(f"- [{c.get('severity', 'info')}] {c.get('message', '')}")

    revision_feedback = "\n".join(feedback_lines) or "Code review rejected. Please fix issues."
    revision_round = state.get("revision_count", 0)
    _record_timeline_step(
        state,
        step_key="tl.requesting_changes",
        title="Team Lead requesting changes from Web Dev",
        lifecycle_state=LIFECYCLE_DONE,
        summary_template="Team Lead requested implementation changes after code review.",
        summary_facts={"review_summary": summary[:500] or "Code review rejected."},
        round=revision_round,
        conditional=True,
    )

    # Determine review report path for dev agent self-assessment reference
    revision_count = state.get("revision_count", 0)
    review_round = revision_count + 1
    review_report_path = f"code-review/review-report-{review_round}.json"

    jira_key = str(state.get("jira_key") or (state.get("jira_context") or {}).get("key") or "")
    if jira_key:
        try:
            from framework.tools.registry import get_registry

            jira_result = _safe_json(
                get_registry().execute_sync(
                    "jira_comment",
                    {
                        "ticket_key": jira_key,
                        "comment": "Code review requested a revision.\n\n" + revision_feedback[:3000],
                        "task_id": state.get("_task_id", ""),
                    },
                ),
                {},
            )
            if isinstance(jira_result, dict) and jira_result.get("error"):
                log.warn("jira review feedback comment failed", jira_key=jira_key, error=str(jira_result.get("error")))
        except Exception as exc:
            print(f"[{_AGENT_ID}] Jira review feedback comment failed: {exc}")

    # Post inline review comments to PR if available
    pr_url = state.get("pr_url", "")
    pr_number = state.get("pr_number", 0)
    repo_url = state.get("repo_url", "")
    if pr_url and repo_url and pr_number:
        try:
            from framework.tools.registry import get_registry as _get_registry
            tool_registry = _get_registry()
            inline_comments = [c for c in comments if c.get("file") and c.get("line")]
            for ic in inline_comments[:5]:  # Limit to top 5 inline comments per round
                inline_result = _safe_json(
                    tool_registry.execute_sync(
                        "scm_add_pr_inline_comment",
                        {
                            "repo_url": repo_url,
                            "pr_number": pr_number,
                            "file_path": ic["file"],
                            "line": ic["line"],
                            "comment": f"[{ic.get('severity', 'info').upper()}] {ic.get('message', '')}",
                            "task_id": state.get("_task_id", ""),
                        },
                    ),
                    {},
                )
                if isinstance(inline_result, dict) and inline_result.get("error"):
                    log.warn(
                        "inline PR comment failed",
                        repo_url=repo_url,
                        pr_number=pr_number,
                        file=ic.get("file", ""),
                        line=ic.get("line", 0),
                        error=str(inline_result.get("error")),
                    )
        except Exception as exc:
            print(f"[{_AGENT_ID}] Inline PR comments failed (non-critical): {exc}")

    return {
        "revision_feedback": revision_feedback,
        "revision_count": state.get("revision_count", 0) + 1,
        "review_report_path": review_report_path,
    }


async def report_success(state: dict) -> dict:
    """Build final success report."""
    log = _logger(state)
    log.node("report_success")
    _audit_command(state, "report_success")
    _record_timeline_step(
        state,
        step_key="tl.reported",
        title="Team Lead reporting to Compass",
        summary_template="Team Lead is preparing the final delivery report for Compass.",
    )
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
            log.info(
                "final-report.json written",
                report_path=report_file,
                pr_url=pr_url,
                revision_count=revision_count,
                review_verdict=verdict,
            )
            _audit_stage(
                state,
                "report_success",
                completed_steps=[
                    "receive_task",
                    "analyze_requirements",
                    "gather_context",
                    "validate_readiness",
                    "create_plan",
                    "dispatch_dev_agent",
                    "review_result",
                    "report_success",
                ],
                pending_steps=[],
                extra={
                    "pr_url": pr_url,
                    "branch": branch,
                    "review_verdict": verdict,
                    "revision_count": revision_count,
                },
            )
        except OSError as exc:
            print(f"[{_AGENT_ID}] Failed to write final-report.json: {exc}")

    jira_key = str(state.get("jira_key") or (state.get("jira_context") or {}).get("key") or "")
    if jira_key:
        try:
            from framework.tools.registry import get_registry

            jira_comment_result = _safe_json(
                get_registry().execute_sync(
                    "jira_comment",
                    {
                        "ticket_key": jira_key,
                        "comment": f"Code review passed. PR is ready for merge: {pr_url}",
                        "task_id": state.get("_task_id", ""),
                    },
                ),
                {},
            )
            if isinstance(jira_comment_result, dict) and jira_comment_result.get("error"):
                log.warn("jira final review comment failed", jira_key=jira_key, error=str(jira_comment_result.get("error")))
            # Transition Jira to "Ready for Merge" state
            jira_transition_result = _safe_json(
                get_registry().execute_sync(
                    "jira_transition",
                    {
                        "ticket_key": jira_key,
                        "transition_name": "Ready for Merge",
                        "task_id": state.get("_task_id", ""),
                    },
                ),
                {},
            )
            if isinstance(jira_transition_result, dict) and jira_transition_result.get("error"):
                log.warn("jira ready-for-merge transition failed", jira_key=jira_key, error=str(jira_transition_result.get("error")))
        except Exception as exc:
            print(f"[{_AGENT_ID}] Jira final review comment/transition failed: {exc}")

    cleanup = await _ack_and_cleanup_dev_agent(state)

    _record_timeline_step(
        state,
        step_key="tl.reported",
        title="Team Lead reporting to Compass",
        lifecycle_state=LIFECYCLE_DONE,
        summary_template="Team Lead reported success with review verdict {verdict}.",
        summary_facts={"verdict": verdict, "pr_url": pr_url},
    )

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
        _record_timeline_step(
            state,
            step_key="tl.requesting_user_input",
            title="Team Lead requesting user input for clarification",
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Team Lead received user input and resumed the workflow.",
            conditional=True,
        )
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
    _record_timeline_step(
        state,
        step_key="tl.requesting_user_input",
        title="Team Lead requesting user input for clarification",
        lifecycle_state=LIFECYCLE_WAITING_FOR_USER,
        summary_template="Team Lead requested user input after {revision_count} revision attempts.",
        summary_facts={"revision_count": revision_count},
        conditional=True,
    )

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
