"""Compass Agent -- LLM-driven control plane entry point.

Architecture: **ReAct-first** (appropriate for open-ended user interaction).

Routing strategy (hybrid — reliable + intelligent):
1. Heuristic classification for obvious development/office tasks (no LLM needed).
2. LLM single-shot classification for ambiguous requests.
3. Direct ToolRegistry dispatch for development/office tasks (deterministic,
   bypasses Claude MCP tool-calling which is unreliable in --print mode).
4. run_agentic (LLM + tools) only for general conversational responses.

Instructions (system prompt) live in:
  agents/compass/instructions/system.md

Tools live in:
  agents/compass/tools.py
"""
from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from typing import Any

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode
# Importing framework.devlog early is what activates the default
# timezone fallback (``config/constellation.yaml:default_tz``), so
# every subsequent log line and datetime emission is anchored to the
# right zone even before the agent calls its first AgentLogger.
from framework import devlog  # noqa: F401
from framework.major_step import (
    LIFECYCLE_CANCELLED,
    LIFECYCLE_DONE,
    LIFECYCLE_FAILED,
    LIFECYCLE_RESUMING,
    LIFECYCLE_RUNNING,
    LIFECYCLE_TERMINATED,
    LIFECYCLE_WAITING_FOR_USER,
    ensure_major_step_skeleton,
    record_major_step,
)
from agents.compass.ui.routes import handle_ui_request
from agents.compass.tools import TOOL_NAMES, register_compass_tools


def _build_compass_definition() -> AgentDefinition:
    """Build Compass's AgentDefinition from YAML config, with tool fallback."""
    from framework.config import build_agent_definition_from_config

    try:
        cfg = build_agent_definition_from_config("compass")
    except Exception:
        cfg = {}

    return AgentDefinition(
        agent_id=cfg.get("agent_id", "compass"),
        name=cfg.get("name", "Compass Agent"),
        description=cfg.get(
            "description",
            "Control plane: task classification, permission check, routing, and user summary",
        ),
        mode=AgentMode.CHAT,
        execution_mode=ExecutionMode.PERSISTENT,
        workflow=None,
        tools=cfg.get("tools", TOOL_NAMES),
        permissions=cfg.get("permissions", {"scm": "none", "filesystem": "workspace-only"}),
        permission_profile=cfg.get("permission_profile", "compass"),
        config=cfg.get("config", {}),
    )


compass_definition = _build_compass_definition()


def _parse_classification_payload(raw_output: str) -> tuple[str, float]:
    """Parse and validate an LLM triage response.

    Preferred response shape is JSON: {"type": "development", "confidence": 0.9}.
    Legacy one-word responses are accepted for compatibility, but still pass
    through the deterministic classification gate.
    """
    from framework.validation_gates import validate_classification

    raw = (raw_output or "").strip()
    parsed: Any = None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = None

    if isinstance(parsed, dict):
        candidate = str(parsed.get("type") or parsed.get("category") or "").strip().lower()
        try:
            confidence = float(parsed.get("confidence", 1.0))
        except (TypeError, ValueError):
            confidence = 0.0
    else:
        cleaned = raw.strip().lower().strip(".`'\" ")
        candidate = cleaned.split()[0] if cleaned else ""
        confidence = 1.0

    gate = validate_classification(candidate)
    if not gate.passed:
        return "", 0.0
    if confidence < 0 or confidence > 1:
        confidence = 0.0
    return candidate, confidence


def _classify_request(user_text: str, runtime) -> str:
    """Classify request as 'development', 'office', or 'general'.

    Strategy:
    1. Strong heuristics catch unambiguous cases quickly (no LLM call).
    2. Heuristic signals are passed as context hints to the LLM for
       everything else, making the LLM the primary decision maker.
    3. Falls back to 'general' when runtime is unavailable.
    """
    lower = user_text.lower()

    # --- Heuristic pre-screening (high-confidence shortcuts only) ---
    has_jira_url = bool(re.search(
        r"https?://[^\s]+/browse/[A-Z][A-Z0-9]+-\d+", user_text
    ))
    has_jira_key = bool(re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", user_text))
    has_dev_action = any(kw in lower for kw in [
        "implement", "fix bug", "fix the bug", "create pr", "create a pr",
        "open pr", "pull request", "code review", "refactor", "develop",
        "write tests", "add tests", "write unit tests", "set up ci",
        "set up docker", "migrate database", "database migration",
    ])

    # Obvious development: Jira URL + development verb
    if has_jira_url and has_dev_action:
        return "development"
    # Obvious development: explicit implementation request for a Jira ticket
    if has_jira_url and any(kw in lower for kw in ["implement", "implement the", "implement jira"]):
        return "development"
    # Jira key alone is strong enough → development
    if has_jira_key and has_dev_action:
        return "development"

    # Obvious office: document/data operation verbs + file/folder hints
    office_verb = any(kw in lower for kw in ["summarize", "analyze", "organize"])
    office_target = any(kw in lower for kw in [
        "pdf", "docx", "txt", "csv", "xlsx", "xls", "spreadsheet",
        "document", "documents", "folder", "files", "essay", "essays",
    ])
    if office_verb and office_target:
        return "office"
    if any(kw in lower for kw in ["summarize the pdf", "analyze the spreadsheet", "organize files"]):
        return "office"

    # --- LLM-primary classification for everything else ---
    if runtime is None:
        # Unit-test path without runtime: apply minimal fallback heuristics
        if has_jira_url or (has_jira_key and has_dev_action):
            return "development"
        if any(kw in lower for kw in ["summarize", "pdf", "docx", "spreadsheet", "document", "organize files"]):
            return "office"
        return "general"

    try:
        from agents.compass.prompts.triage import TRIAGE_SYSTEM, TRIAGE_TEMPLATE
        result = runtime.run(
            prompt=TRIAGE_TEMPLATE.format(user_request=user_text),
            system_prompt=TRIAGE_SYSTEM,
            max_tokens=128,
        )
        raw = (result.get("raw_response") or "").strip()
        classification, confidence = _parse_classification_payload(raw)
        if classification and confidence >= 0.45:
            return classification
        if classification:
            print(
                f"[compass] LLM triage low confidence: "
                f"classification={classification!r} confidence={confidence:.2f} — defaulting to general"
            )
            return "general"
        # Unexpected output: log and fall back
        print(f"[compass] LLM triage unexpected response: {raw!r} — defaulting to general")
    except Exception as exc:
        print(f"[compass] LLM classification failed: {exc} — defaulting to general")

    return "general"


def _extract_jira_key(user_text: str) -> str:
    """Extract the first Jira issue key from the request text."""
    m = re.search(r"\b([A-Z][A-Z0-9]+-\d+)\b", user_text)
    return m.group(1) if m else ""


def _normalize_output_mode(value: str) -> str:
    mode = (value or "").strip().lower()
    return mode if mode in {"workspace", "inplace"} else ""


def _normalize_source_paths(value: object) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, (list, tuple, set)):
        candidates = [str(item) for item in value if item]
    else:
        return []

    normalized: list[str] = []
    for candidate in candidates:
        sanitized = str(candidate).strip().strip('"\'`').lstrip("([{").rstrip(".,;:!?)]}\"'`")
        if sanitized and sanitized not in normalized:
            normalized.append(sanitized)
    return normalized


def _extract_office_paths_from_text(user_text: str) -> list[str]:
    absolute_paths = re.findall(r'(?:(?<=\s)|^)(/[^\s"\'`]+)', user_text or "")
    quoted_paths = re.findall(r'["\']([^"\']*[\\/][^"\']+)["\']', user_text or "")
    paths = [candidate for candidate in absolute_paths + quoted_paths if not candidate.startswith("//")]
    return _normalize_source_paths(paths)


def _normalize_office_capability(value: str, user_text: str = "") -> str:
    raw = (value or "").strip().lower()
    mapping = {
        "office.document.summarize": "summarize",
        "office.folder.summarize": "summarize",
        "office.data.analyze": "analyze",
        "office.folder.organize": "organize",
    }
    raw = mapping.get(raw, raw)
    if raw in {"summarize", "analyze", "organize"}:
        return raw
    lower = user_text.lower()
    if "organize" in lower:
        return "organize"
    if "analyze" in lower:
        return "analyze"
    return "summarize"


def _office_requested_capability(capability: str) -> str:
    mapping = {
        "analyze": "office.data.analyze",
        "organize": "office.folder.organize",
        "summarize": "office.document.summarize",
    }
    return mapping.get(capability, "office.document.summarize")


def _extract_office_request(user_text: str, metadata: dict) -> dict:
    source_paths = _normalize_source_paths(
        metadata.get("source_paths") or metadata.get("officeTargetPaths") or metadata.get("filePath")
    )
    if not source_paths:
        source_paths = _extract_office_paths_from_text(user_text)

    return {
        "source_paths": source_paths,
        "capability": _normalize_office_capability(
            str(metadata.get("capability") or metadata.get("requestedCapability") or ""),
            user_text,
        ),
        "output_mode": _normalize_output_mode(
            str(metadata.get("output_mode") or metadata.get("officeOutputMode") or "")
        ),
    }


def _office_major_step_skeleton(office_request: dict) -> list[dict]:
    """Return the proposal-aligned office timeline skeleton for Compass UI."""
    capability = _normalize_office_capability(str(office_request.get("capability") or ""))

    rows: list[dict] = [
        {
            "step_key": "compass.received",
            "title": "Compass receiving task",
            "agent": "compass",
        },
        {
            "step_key": "compass.asking_output_mode",
            "title": "Compass asking for output location",
            "agent": "compass",
            "conditional": True,
        },
        {
            "step_key": "compass.dispatched",
            "title": "Compass dispatching to Office Agent",
            "agent": "compass",
        },
        {
            "step_key": "office.received",
            "title": "Office receiving task",
            "agent": "office",
        },
        {
            "step_key": "office.validating",
            "title": "Office validating sources and permissions",
            "agent": "office",
        },
    ]

    if capability == "analyze":
        rows.extend(
            [
                {
                    "step_key": "office.inferring_schema",
                    "title": "Office inferring data schema",
                    "agent": "office",
                },
                {
                    "step_key": "office.computing_stats",
                    "title": "Office computing statistics",
                    "agent": "office",
                },
                {
                    "step_key": "office.generating_report",
                    "title": "Office generating analysis report",
                    "agent": "office",
                },
                {
                    "step_key": "office.writing",
                    "title": "Office writing deliverable",
                    "agent": "office",
                },
            ]
        )
    elif capability == "organize":
        rows.extend(
            [
                {
                    "step_key": "office.scanning",
                    "title": "Office scanning folder structure",
                    "agent": "office",
                },
                {
                    "step_key": "office.planning",
                    "title": "Office planning organization",
                    "agent": "office",
                },
                {
                    "step_key": "office.creating_folders",
                    "title": "Office creating folder structure",
                    "agent": "office",
                },
                {
                    "step_key": "office.moving_files",
                    "title": "Office moving files into organized structure",
                    "agent": "office",
                },
                {
                    "step_key": "office.writing_plan",
                    "title": "Office writing organization plan",
                    "agent": "office",
                },
            ]
        )
    else:
        rows.extend(
            [
                {
                    "step_key": "office.reading",
                    "title": "Office reading documents",
                    "agent": "office",
                },
                {
                    "step_key": "office.summarizing",
                    "title": "Office summarizing each document",
                    "agent": "office",
                },
                {
                    "step_key": "office.combining",
                    "title": "Office creating combined summary",
                    "agent": "office",
                    "conditional": True,
                },
                {
                    "step_key": "office.writing",
                    "title": "Office writing deliverable",
                    "agent": "office",
                },
            ]
        )

    rows.extend(
        [
            {
                "step_key": "office.verifying",
                "title": "Office verifying deliverable",
                "agent": "office",
            },
            {
                "step_key": "office.delivered",
                "title": "Office delivering report to Compass",
                "agent": "office",
            },
        ]
    )
    return rows


def _development_major_step_skeleton(jira_key: str = "") -> list[dict]:
    """Return the proposal-aligned development timeline skeleton for Compass UI."""
    return [
        {
            "step_key": "compass.received",
            "title": "Compass receiving task",
            "agent": "compass",
        },
        {
            "step_key": "compass.dispatched",
            "title": "Compass dispatching to Team Lead",
            "agent": "compass",
        },
        {
            "step_key": "tl.analyzing",
            "title": "Team Lead analyzing task",
            "agent": "team-lead",
        },
        {
            "step_key": "tl.gathering",
            "title": "Team Lead gathering context",
            "agent": "team-lead",
        },
        {
            "step_key": "tl.dispatched_dev",
            "title": "Team Lead dispatching to Web Dev",
            "agent": "team-lead",
        },
        {
            "step_key": "wd.drafting_plan",
            "title": "Web Dev drafting plan",
            "agent": "web-dev",
        },
        {
            "step_key": "wd.implementing",
            "title": "Web Dev implementing changes",
            "agent": "web-dev",
        },
        {
            "step_key": "wd.building",
            "title": "Web Dev building and testing",
            "agent": "web-dev",
        },
        {
            "step_key": "wd.self_check",
            "title": "Web Dev running self-check",
            "agent": "web-dev",
        },
        {
            "step_key": "wd.handover",
            "title": "Web Dev handing over to Team Lead",
            "agent": "web-dev",
        },
        {
            "step_key": "tl.requesting_review",
            "title": "Team Lead requesting code review",
            "agent": "team-lead",
        },
        {
            "step_key": "cr.reviewing",
            "title": "Code Review reviewing PR",
            "agent": "code-review",
        },
        {
            "step_key": "tl.reported",
            "title": "Team Lead reporting to Compass",
            "agent": "team-lead",
        },
        {
            "step_key": "compass.task_completed",
            "title": "Compass marking task completed",
            "agent": "compass",
        },
        {
            "step_key": "tl.requesting_changes",
            "title": "Team Lead requesting changes from Web Dev",
            "agent": "team-lead",
            "conditional": True,
        },
        {
            "step_key": "wd.addressing_feedback",
            "title": "Web Dev addressing review feedback",
            "agent": "web-dev",
            "conditional": True,
        },
        {
            "step_key": "wd.fixing_gaps",
            "title": "Web Dev fixing self-check gaps",
            "agent": "web-dev",
            "conditional": True,
        },
        {
            "step_key": "wd.rebuilding",
            "title": "Web Dev rebuilding and retesting",
            "agent": "web-dev",
            "conditional": True,
        },
        {
            "step_key": "wd.self_check_retry",
            "title": "Web Dev rerunning self-check",
            "agent": "web-dev",
            "conditional": True,
        },
        {
            "step_key": "wd.handover_retry",
            "title": "Web Dev handing over revised result",
            "agent": "web-dev",
            "conditional": True,
        },
        {
            "step_key": "tl.re_requesting_review",
            "title": "Team Lead requesting follow-up code review",
            "agent": "team-lead",
            "conditional": True,
        },
        {
            "step_key": "cr.reviewing_retry",
            "title": "Code Review reviewing revised PR",
            "agent": "code-review",
            "conditional": True,
        },
        {
            "step_key": "tl.requesting_user_input",
            "title": "Team Lead requesting user input for clarification",
            "agent": "team-lead",
            "conditional": True,
        },
        {
            "step_key": "wd.requesting_user_input",
            "title": "Web Dev requesting user input",
            "agent": "web-dev",
            "conditional": True,
        },
        {
            "step_key": "cr.requesting_user_input",
            "title": "Code Review requesting user input",
            "agent": "code-review",
            "conditional": True,
        },
    ]


def _office_output_mode_question() -> str:
    return (
        "Where should the office output go? Reply `workspace` to keep the source read-only and "
        "write results under the task workspace, or reply `inplace` to write inside the source folder."
    )


def _office_callback_url(task_id: str) -> str:
    base_url = os.environ.get("COMPASS_BASE_URL", "").rstrip("/")
    if not base_url:
        return ""
    return f"{base_url}/tasks/{task_id}/callbacks"


def _office_delivery_report_path(task_id: str) -> str:
    artifact_root = os.environ.get("ARTIFACT_ROOT", "artifacts/")
    return os.path.join(artifact_root, task_id, "office", "task-report.json")


def _office_dispatch_failed(dispatch_data: dict[str, Any]) -> bool:
    if str(dispatch_data.get("status") or "").strip().lower() in {
        "error",
        "failed",
        "no-capability",
        "unknown",
    }:
        return True
    # Belt-and-suspenders: if the LLM wrote an error explanation instead of
    # real output, the office agent may still report status="completed" because
    # the agentic runtime only checks that *some* response was produced.  Treat
    # such summaries as failures so the orchestrator surfaces them honestly.
    summary = str(
        dispatch_data.get("summary")
        or dispatch_data.get("message")
        or ""
    ).strip()
    return _summary_indicates_office_failure(summary)


_OFFICE_FAILURE_PATTERNS = (
    "cannot be found or accessed",
    "could not be found",
    "does not exist or is not a valid",
    "error encountered",
    "i cannot inspect or analyze",
    "i cannot access",
    "no such file or directory",
    "required action",
    "source file is not accessible",
    "the path does not exist",
    "the file does not exist",
    "file not found",
    "the requested source file cannot",
)


def _summary_indicates_office_failure(summary: str) -> bool:
    if not summary:
        return False
    lowered = summary.lower()
    return any(needle in lowered for needle in _OFFICE_FAILURE_PATTERNS)


def _dispatch_office_request(task_id: str, user_text: str, office_request: dict, registry, log) -> dict:
    registry_url = ""
    office_url = ""
    discovered_from_registry = False
    requested_capability = _office_requested_capability(office_request.get("capability", "summarize"))
    try:
        from framework.registry_client import RegistryClient

        rc = RegistryClient.from_config()
        registry_url = rc.url
        log.a2a("→", "registry", capability=requested_capability, registry_url=registry_url)
        # Office is now an on-demand agent: compass always launches a
        # per-task container via the Launcher, never talks to a
        # long-running office service. The only thing we need from the
        # registry is the launch definition (image, port, env); if it's
        # not registered, fail closed.
        definition = rc.get_capability_definition(requested_capability)
        if not definition:
            definition = rc.get_capability_definition("office.document.summarize")
        discovered_from_registry = bool(definition)
        office_url = "per-task-launch" if discovered_from_registry else ""
        log.info("registry lookup", registry_url=registry_url, discovered_url=office_url)
        log.a2a(
            "←",
            "registry",
            capability=requested_capability,
            status="found" if discovered_from_registry else "not_found",
            discovered_url=office_url,
        )
    except Exception as exc:
        log.warn("registry lookup failed", error=str(exc))
        log.a2a("←", "registry", capability=requested_capability, status="error", error=str(exc)[:100])

    if not discovered_from_registry:
        log.warn("office capability not found in registry", registry_url=registry_url)
        return {
            "status": "no-capability",
            "message": "Constellation currently has no registered office-processing agent, so this office task cannot run right now.",
        }

    callback_url = _office_callback_url(task_id)
    log.a2a(
        "→",
        "office",
        capability=requested_capability,
        office_url=office_url,
        task_id=task_id,
        source_count=len(office_request.get("source_paths", [])),
        output_mode=office_request.get("output_mode", "workspace"),
    )
    try:
        dispatch_result_str = registry.execute_sync(
            "dispatch_office_task",
            {
                "task_description": user_text,
                "source_paths": office_request.get("source_paths", []),
                "capability": office_request.get("capability", "summarize"),
                "output_mode": office_request.get("output_mode", "workspace"),
                "orchestrator_task_id": task_id,
                "callback_url": callback_url,
            },
        )
        dispatch_data = json.loads(dispatch_result_str) if dispatch_result_str else {}
        log.a2a("←", "office", status=dispatch_data.get("status", "unknown"), result_preview=str(dispatch_data)[:200])
    except Exception as exc:
        dispatch_data = {"status": "error", "message": str(exc)}
        log.error("dispatch_office_task failed", error=str(exc))
        log.a2a("←", "office", status="error", error=str(exc)[:100])
        print(f"[compass] dispatch_office_task error: {exc}")

    report_path = _office_delivery_report_path(task_id)
    if dispatch_data.get("status") == "completed":
        if os.path.exists(report_path):
            dispatch_data["deliveryVerified"] = True
            dispatch_data["deliveryReportPath"] = report_path
            report_success = True
            try:
                with open(report_path, encoding="utf-8") as fh:
                    report_data = json.load(fh)
                report_success = bool((report_data.get("data") or {}).get("success", True))
            except Exception as exc:
                report_success = False
                dispatch_data["message"] = dispatch_data.get("message") or (
                    "Office task reported completion but task-report.json could not be read."
                )
                log.warn("office delivery report unreadable", task_report=report_path, error=str(exc))

            if report_success:
                log.info("office delivery verified", task_report=report_path)
            else:
                dispatch_data["status"] = "failed"
                if not dispatch_data.get("summary"):
                    summary = str((report_data.get("data") or {}).get("summary") or "").strip() if 'report_data' in locals() else ""
                    if summary:
                        dispatch_data["summary"] = summary
                log.warn("office delivery report indicated failure", task_report=report_path)
        else:
            log.warn("office delivery report missing", task_report=report_path)
            summary = str(dispatch_data.get("summary") or dispatch_data.get("message") or "").strip()
            dispatch_data["status"] = "failed"
            failure_reason = "Office task reported completion but did not write task-report.json."
            dispatch_data["message"] = (
                f"{summary}\n\n{failure_reason}" if summary else failure_reason
            )

    # Final guard: if the LLM produced an error explanation instead of real
    # output, downgrade status even if the office agent claimed success.
    if (
        str(dispatch_data.get("status") or "").strip().lower() == "completed"
        and _summary_indicates_office_failure(
            str(dispatch_data.get("summary") or dispatch_data.get("message") or "")
        )
    ):
        dispatch_data["status"] = "failed"
        log.warn(
            "office summary indicated failure despite completed status",
            summary_preview=str(dispatch_data.get("summary") or "")[:200],
        )

    log.info("office dispatch complete", status=dispatch_data.get("status", "unknown"))
    return dispatch_data


def _development_start_message(jira_key: str) -> str:
    jira_label = jira_key or "N/A"
    return (
        "Development task accepted and running in the background.\n"
        f"Jira: {jira_label}"
    )


def _development_final_message(dispatch_data: dict) -> str:
    summary = str(dispatch_data.get("summary") or "").strip()
    if summary:
        return summary
    status = str(dispatch_data.get("status") or "unknown").strip() or "unknown"
    if status == "completed":
        pr_url = str(dispatch_data.get("prUrl") or "").strip()
        branch = str(dispatch_data.get("branch") or "").strip()
        lines = ["Development task completed successfully."]
        if pr_url:
            lines.append(f"PR: {pr_url}")
        if branch:
            lines.append(f"Branch: {branch}")
        return "\n".join(lines)
    return str(dispatch_data.get("message") or f"Development task ended with status: {status}").strip()


def _chat_entry_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _append_chat_entry(
    task_store,
    task_id: str,
    *,
    role: str,
    text: str,
    tone: str = "normal",
) -> None:
    if not text:
        return
    task = task_store.get_task(task_id)
    if task is None:
        return

    metadata = task.metadata or {}
    history = list(metadata.get("chat_history") or [])
    history.append(
        {
            "role": role,
            "text": text,
            "tone": tone,
            "ts": _chat_entry_timestamp(),
        }
    )
    task_store.update_metadata(task_id, {"chat_history": history})


def _record_major_step(
    task_store,
    task_id: str,
    *,
    step_key: str,
    title: str,
    agent: str = "compass",
    lifecycle_state: str = LIFECYCLE_RUNNING,
    summary_template: str = "",
    summary_facts: dict | None = None,
    conditional: bool = False,
    round: int = 0,
) -> None:
    """Thin Compass-side wrapper around ``framework.major_step.record_major_step``.

    Compass owns the same ``TaskStore`` as the orchestrator task, so the
    ``orchestrator_task_id`` equals ``task_id`` and the cross-process
    fan-out branch is a no-op.
    """
    if not task_id or not step_key or not title:
        return
    if task_store.get_task(task_id) is None:
        return
    record_major_step(
        task_id,
        step_key=step_key,
        title=title,
        agent=agent,
        lifecycle_state=lifecycle_state,
        summary_template=summary_template,
        summary_facts=summary_facts,
        conditional=conditional,
        round=round,
        orchestrator_task_id=task_id,
        task_store=task_store,
    )


def _log_store_url() -> str:
    return (
        os.environ.get("LOG_STORE_URL")
        or os.environ.get("LOG_STORE_BASE_URL")
        or ""
    ).rstrip("/")


class CompassAgent(BaseAgent):
    """Compass Agent -- routes requests via heuristic + LLM classification."""

    def _complete_development_task(
        self,
        *,
        task_id: str,
        user_text: str,
        jira_key: str,
        workspace_path: str,
    ) -> None:
        from framework.a2a.protocol import Artifact
        from framework.devlog import AgentLogger
        from framework.tools.registry import get_registry

        task_store = self.services.task_store
        log = AgentLogger(task_id=task_id, agent_name=self.definition.agent_id)
        dispatch_data: dict[str, object] = {}

        try:
            registry = get_registry()
            log.a2a("→", "team-lead", capability="dispatch_development_task", jira_key=jira_key)
            dispatch_result_str = registry.execute_sync(
                "dispatch_development_task",
                {
                    "task_description": user_text,
                    "jira_key": jira_key,
                    "orchestratorTaskId": task_id,
                    "workspacePath": workspace_path,
                },
            )
            dispatch_data = json.loads(dispatch_result_str) if dispatch_result_str else {}
        except Exception as exc:
            dispatch_data = {"status": "error", "message": str(exc)}
            log.error("dispatch_development_task failed", error=str(exc))
            print(f"[{self.definition.agent_id}] dispatch_development_task error: {exc}")

        team_lead_task_id = str(dispatch_data.get("taskId") or "").strip()
        if team_lead_task_id:
            task_store.update_metadata(task_id, {"teamLeadTaskId": team_lead_task_id})

        final_message = _development_final_message(dispatch_data)
        artifact_metadata = {"agentId": self.definition.agent_id}
        if team_lead_task_id:
            artifact_metadata["teamLeadTaskId"] = team_lead_task_id
        for key in ("prUrl", "branch", "jiraInReview"):
            value = dispatch_data.get(key)
            if value not in (None, ""):
                artifact_metadata[key] = value

        artifacts = [Artifact(
            name="compass-response",
            artifact_type="text/plain",
            parts=[{"text": final_message}],
            metadata=artifact_metadata,
        )]

        task_state = str(dispatch_data.get("state") or "").strip()
        status = str(dispatch_data.get("status") or "unknown").strip() or "unknown"
        if task_state == "TASK_STATE_INPUT_REQUIRED":
            task_store.set_artifacts(task_id, artifacts)
            task_store.pause_task(
                task_id,
                question=final_message or "Team Lead requested clarification.",
                interrupt_metadata={"teamLeadTaskId": team_lead_task_id, "task_type": "development"},
            )
            _record_major_step(
                task_store,
                task_id,
                step_key="tl.requesting_user_input",
                title="Team Lead requesting user input for clarification",
                agent="team-lead",
                lifecycle_state=LIFECYCLE_WAITING_FOR_USER,
                summary_template="Team Lead requested user input: {input_reason}; awaiting user response.",
                summary_facts={"input_reason": "ambiguous requirements"},
            )
            _append_chat_entry(
                task_store,
                task_id,
                role="COMPASS",
                text=final_message or "Team Lead requested clarification.",
                tone="input-required",
            )
            log.warn("development task awaiting input", tl_task_id=team_lead_task_id)
            log.a2a("←", "team-lead", status="input-required", tl_task_id=team_lead_task_id)
            return

        if status != "completed":
            task_store.set_artifacts(task_id, artifacts)
            task_store.fail_task(task_id, final_message)
            _record_major_step(
                task_store,
                task_id,
                step_key="compass.task_failed",
                title=f"Compass marking task failed: {final_message[:200]}",
                agent="compass",
                lifecycle_state=LIFECYCLE_FAILED,
                summary_template="Compass marked the task as failed: {failure_reason}.",
                summary_facts={"failure_reason": final_message[:500] or "development task did not complete"},
            )
            _append_chat_entry(
                task_store,
                task_id,
                role="COMPASS",
                text=final_message,
                tone="failed",
            )
            log.error("development task failed", tl_task_id=team_lead_task_id, status=status)
            log.a2a("←", "team-lead", status=status or "error", tl_task_id=team_lead_task_id)
            return

        task_store.complete_task(task_id, artifacts=artifacts, message=final_message)
        _record_major_step(
            task_store,
            task_id,
            step_key="compass.task_completed",
            title="Compass marking task completed",
            agent="compass",
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Compass marked the task as completed.",
        )
        _append_chat_entry(
            task_store,
            task_id,
            role="COMPASS",
            text=final_message,
            tone="completed",
        )
        log.info(
            "development task complete",
            tl_task_id=team_lead_task_id,
            pr_url=str(dispatch_data.get("prUrl") or ""),
            branch=str(dispatch_data.get("branch") or ""),
        )
        log.a2a("←", "team-lead", status="completed", tl_task_id=team_lead_task_id)

    async def handle_message(self, message: dict) -> dict:
        import os as _os

        from framework.a2a.protocol import Artifact
        from framework.devlog import AgentLogger
        from framework.instructions import load_instructions
        from framework.runtime.adapter import get_runtime
        from framework.tools.registry import get_registry

        register_compass_tools()

        msg = message.get("message", message)
        parts = msg.get("parts") or []
        user_text = next((p.get("text", "") for p in parts if p.get("text")), "")
        meta = msg.get("metadata") or {}

        # Create task via TaskStore — task.id IS the master task_id for this workflow
        task_store = self.services.task_store
        task = task_store.create_task(
            agent_id=self.definition.agent_id,
            metadata={
                "user_request": user_text,
                "userRequest": user_text,
                "chat_history": [],
                "progress_steps": [],
                "current_major_step": "Request received by Compass",
            },
        )
        _aid = self.definition.agent_id
        # v0.8: emit the first major step (compass.received) as the canonical
        # first row of the timeline. This matches design doc §4.2.1 step #1
        # and ensures ``deriveMajorTimeline`` sees a real ``compass.received``
        # row instead of relying on the legacy ``isLegacyCompassReceived``
        # compatibility branch in the front-end renderer.
        _record_major_step(
            task_store,
            task.id,
            step_key="compass.received",
            title="Compass receiving request",
            agent=_aid,
            lifecycle_state=LIFECYCLE_RUNNING,
            summary_template="Compass received a new request.",
        )
        _append_chat_entry(task_store, task.id, role="USER", text=user_text)

        runtime = self.services.runtime or get_runtime()
        registry = get_registry()

        # --- Classify ---
        task_type = _classify_request(user_text, runtime)
        print(f"[{_aid}] task_type={task_type!r} request={user_text[:120]!r}")

        # --- Workspace path: {ARTIFACT_ROOT}/{task_id}/
        # All agents in this workflow share the same task_id as the workspace root.
        artifact_root = _os.environ.get("ARTIFACT_ROOT", "artifacts/")
        workspace_path = _os.path.join(artifact_root, task.id)

        # --- Compass logger — writes only to its own directory ---
        log = AgentLogger(task_id=task.id, agent_name=_aid)
        log.node("handle_message", task_type=task_type, task_id=task.id,
                 request=user_text[:200])

        # --- Dispatch ---
        dispatch_data = {}
        office_request: dict[str, Any] = {}
        if task_type == "development":
            jira_key = _extract_jira_key(user_text)
            log.info("dispatching development task asynchronously", jira_key=jira_key)
            task_store.update_metadata(
                task.id,
                {
                    "task_type": "development",
                    "jira_key": jira_key,
                    "workspace_path": workspace_path,
                },
            )
            try:
                ensure_major_step_skeleton(
                    task.id,
                    entries=_development_major_step_skeleton(jira_key),
                    task_store=task_store,
                )
            except Exception as exc:  # noqa: BLE001
                log.warn("failed to seed development major-step skeleton", error=str(exc))
            response_text = _development_start_message(jira_key)
            _record_major_step(
                task_store,
                task.id,
                step_key="compass.dispatched",
                title="Compass dispatching to Team Lead",
                agent=_aid,
                lifecycle_state=LIFECYCLE_RUNNING,
                summary_template="Compass dispatched the task to the Team Lead Agent for Jira ticket {jira_key}.",
                summary_facts={"jira_key": jira_key or "unspecified"},
            )
            _append_chat_entry(task_store, task.id, role="COMPASS", text=response_text)
            ui_update = {
                "task_id": task.id,
                "task_status": "TASK_STATE_WORKING",
                "chat_message": {
                    "role": "COMPASS",
                    "text": response_text,
                    "style": "normal",
                },
            }
            initial_response = {**task_store.get_task_dict(task.id), "ui_update": ui_update}
            worker = threading.Thread(
                target=self._complete_development_task,
                kwargs={
                    "task_id": task.id,
                    "user_text": user_text,
                    "jira_key": jira_key,
                    "workspace_path": workspace_path,
                },
                daemon=True,
                name="compass-development-dispatch",
            )
            worker.start()
            print(f"[{_aid}] dispatch started in background: jira_key={jira_key!r} taskId={task.id}")
            return initial_response

        elif task_type == "office":
            log.info("dispatching office task")
            office_request = _extract_office_request(user_text, meta)
            task_store.update_metadata(task.id, {"task_type": "office", "office_request": office_request})
            try:
                ensure_major_step_skeleton(
                    task.id,
                    entries=_office_major_step_skeleton(office_request),
                    task_store=task_store,
                )
            except Exception as exc:  # noqa: BLE001
                log.warn("failed to seed office major-step skeleton", error=str(exc))

            if not office_request.get("output_mode"):
                question = _office_output_mode_question()
                task_store.pause_task(
                    task.id,
                    question=question,
                    interrupt_metadata={"kind": "office_output_mode", "office_request": office_request},
                )
                _record_major_step(
                    task_store,
                    task.id,
                    step_key="compass.asking_output_mode",
                    title="Compass asking for output location",
                    agent=_aid,
                    lifecycle_state=LIFECYCLE_WAITING_FOR_USER,
                    conditional=True,
                    summary_template="Compass is waiting for you to choose the output location.",
                )
                _append_chat_entry(
                    task_store,
                    task.id,
                    role="COMPASS",
                    text=question,
                    tone="input-required",
                )
                log.info(
                    "office task awaiting output mode",
                    capability=office_request.get("capability", "summarize"),
                    source_count=len(office_request.get("source_paths", [])),
                )
                ui_update = {
                    "task_id": task.id,
                    "task_status": "TASK_STATE_INPUT_REQUIRED",
                    "chat_message": {
                        "role": "COMPASS",
                        "text": question,
                        "style": "normal",
                    },
                }
                return {**task_store.get_task_dict(task.id), "ui_update": ui_update}

            dispatch_data = _dispatch_office_request(task.id, user_text, office_request, registry, log)
            response_text = dispatch_data.get("message") or f"Office task dispatched. Status: {dispatch_data.get('status', 'unknown')}"
            office_failed = _office_dispatch_failed(dispatch_data)
            office_status = str(dispatch_data.get("status") or "").strip().lower()
            if office_failed:
                _record_major_step(
                    task_store,
                    task.id,
                    step_key="compass.task_failed",
                    title=f"Compass marking task failed: {response_text[:200]}",
                    agent="compass",
                    lifecycle_state=LIFECYCLE_FAILED,
                    summary_template="Compass marked the task as failed: {failure_reason}.",
                    summary_facts={"failure_reason": response_text[:500] or "office dispatch failed"},
                )
            elif office_status == "completed":
                _record_major_step(
                    task_store,
                    task.id,
                    step_key="compass.task_completed",
                    title="Compass marking task completed",
                    agent="compass",
                    lifecycle_state=LIFECYCLE_DONE,
                    summary_template="Compass marked the task as completed.",
                )
            else:
                _record_major_step(
                    task_store,
                    task.id,
                    step_key="office.delivered",
                    title="Office returned a terminal result",
                    agent="office",
                    lifecycle_state=LIFECYCLE_DONE,
                    summary_template="Office delivered the report to Compass.",
                )

        else:
            # General conversational task — use LLM for a direct answer
            log.info("handling as general query")
            system_prompt = load_instructions("compass")
            agentic_result = runtime.run_agentic(
                task=user_text,
                tools=None,
                system_prompt=system_prompt,
                max_turns=5,
                timeout=120,
            )
            response_text = agentic_result.summary or "I can help you with that."
            _record_major_step(
                task_store,
                task.id,
                step_key="compass.task_completed",
                title="Compass marking task completed",
                agent=_aid,
                lifecycle_state=LIFECYCLE_DONE,
                summary_template="Compass marked the task as completed.",
            )

        response_tone = "normal"
        if task_type == "office":
            office_status = str(dispatch_data.get("status") or "").strip().lower()
            if _office_dispatch_failed(dispatch_data):
                response_tone = "failed"
            elif office_status in {"completed", "success"}:
                response_tone = "completed"
        elif task_type == "general":
            response_tone = "completed"

        _append_chat_entry(task_store, task.id, role="COMPASS", text=response_text, tone=response_tone)

        log.info("task complete", response_len=len(response_text))
        office_artifact_metadata = {"agentId": _aid}
        for key in ("summary", "message", "deliveryReportPath", "workspacePath", "status"):
            value = dispatch_data.get(key)
            if value not in (None, ""):
                office_artifact_metadata[key] = value
        if office_request.get("output_mode"):
            office_artifact_metadata["outputMode"] = office_request.get("output_mode")

        artifacts = [Artifact(
            name="compass-response",
            artifact_type="text/plain",
            parts=[{"text": response_text}],
            metadata=office_artifact_metadata,
        )]
        if task_type == "office" and _office_dispatch_failed(dispatch_data):
            task_store.set_artifacts(task.id, artifacts)
            task_store.fail_task(task.id, response_text)
        else:
            task_store.complete_task(task.id, artifacts=artifacts)

        # Build UI-friendly response with ui_update for frontend rendering
        display_status = dispatch_data.get("status", "unknown") if task_type == "development" else (
            dispatch_data.get("status", "unknown") if task_type == "office" else "completed"
        )
        # Use office_failed to determine UI style since it correctly captures all failure
        # states including "no-capability", "error", "failed", and "unknown"
        ui_style = "failed" if (task_type == "office" and office_failed) else (
            "failed" if display_status in ("error", "failed", "unknown") else "normal"
        )
        current_task = task_store.get_task(task.id)
        ui_update = {
            "task_id": task.id,
            "task_status": current_task.status.state.value if current_task else task.status.state.value,
            "chat_message": {
                "role": "COMPASS",
                "text": response_text,
                "style": ui_style,
            }
        }
        return {**task_store.get_task_dict(task.id), "ui_update": ui_update}

    async def resume_task(self, task_id: str, resume_value: object) -> dict:
        from framework.a2a.protocol import Artifact
        from framework.devlog import AgentLogger
        from framework.tools.registry import get_registry

        task_store = self.services.task_store
        task = task_store.get_task(task_id)
        if task is None:
            raise RuntimeError(f"Task {task_id} not found")

        _append_chat_entry(task_store, task_id, role="USER", text=str(resume_value))

        metadata = task.metadata or {}
        if metadata.get("task_type") != "office":
            result = await super().resume_task(task_id, resume_value)
            resumed_task = task_store.get_task(task_id)
            if resumed_task and resumed_task.status.message:
                state_value = getattr(resumed_task.status.state, "value", str(resumed_task.status.state))
                tone = {
                    "TASK_STATE_COMPLETED": "completed",
                    "TASK_STATE_FAILED": "failed",
                    "TASK_STATE_INPUT_REQUIRED": "input-required",
                }.get(state_value, "normal")
                _append_chat_entry(
                    task_store,
                    task_id,
                    role="COMPASS",
                    text=resumed_task.status.message.text(),
                    tone=tone,
                )
            return result

        register_compass_tools()
        registry = get_registry()
        log = AgentLogger(task_id=task_id, agent_name=self.definition.agent_id)

        office_request = dict(metadata.get("office_request") or {})
        reply_text = str(resume_value or "").strip().lower()
        # B1: user-cancel-during-wait — close the in-flight user-input row
        # to ``cancelled`` and append a terminal ``compass.task_cancelled`` row
        # (two-step close per design doc §13 B1).
        if reply_text in {"cancel", "abort", "stop"}:
            _record_major_step(
                task_store,
                task_id,
                step_key="compass.asking_output_mode",
                title="Compass asked for output location (cancelled by user)",
                agent=self.definition.agent_id,
                lifecycle_state=LIFECYCLE_CANCELLED,
                summary_template="Compass cancelled the output location request.",
            )
            cancel_reason = str(resume_value or "user cancelled").strip() or "user cancelled"
            _record_major_step(
                task_store,
                task_id,
                step_key="compass.task_cancelled",
                title=f"Compass marking task cancelled by user: {cancel_reason[:200]}",
                agent="compass",
                lifecycle_state=LIFECYCLE_CANCELLED,
                summary_template="Compass marked the task as cancelled by user: {cancel_reason}.",
                summary_facts={"cancel_reason": cancel_reason[:500]},
            )
            task_store.fail_task(task_id, f"cancelled by user: {cancel_reason}")
            _append_chat_entry(
                task_store,
                task_id,
                role="COMPASS",
                text=f"Task cancelled by user: {cancel_reason}",
                tone="failed",
            )
            log.warn("office task cancelled by user during wait", reply=reply_text)
            ui_update = {
                "task_id": task_id,
                "task_status": "TASK_STATE_FAILED",
                "chat_message": {
                    "role": "COMPASS",
                    "text": f"Task cancelled by user: {cancel_reason}",
                    "style": "failed",
                },
            }
            return {**task_store.get_task_dict(task_id), "ui_update": ui_update}
        output_mode = _normalize_output_mode(str(resume_value))
        if not output_mode:
            question = "Please reply with `workspace` or `inplace` so I can route the office task correctly."
            task_store.pause_task(
                task_id,
                question=question,
                interrupt_metadata={"kind": "office_output_mode", "office_request": office_request},
            )
            _record_major_step(
                task_store,
                task_id,
                step_key="compass.asking_output_mode",
                title="Compass asking for output location",
                agent=self.definition.agent_id,
                lifecycle_state=LIFECYCLE_WAITING_FOR_USER,
                conditional=True,
                summary_template="Compass is waiting for a valid output location.",
            )
            _append_chat_entry(
                task_store,
                task_id,
                role="COMPASS",
                text=question,
                tone="input-required",
            )
            log.warn("invalid office output mode reply", reply=str(resume_value)[:100])
            ui_update = {
                "task_id": task_id,
                "task_status": "TASK_STATE_INPUT_REQUIRED",
                "chat_message": {
                    "role": "COMPASS",
                    "text": question,
                    "style": "normal",
                },
            }
            return {**task_store.get_task_dict(task_id), "ui_update": ui_update}

        office_request["output_mode"] = output_mode
        task_store.update_metadata(task_id, {"office_request": office_request})
        # A2: Compass writes ``resuming`` on the existing user-input row before
        # the agent resumes. The original ``compass.asking_output_mode#0`` row
        # transitions through ``resuming`` and lands on ``done`` below.
        _record_major_step(
            task_store,
            task_id,
            step_key="compass.asking_output_mode",
            title="Compass resuming after output location was selected",
            agent=self.definition.agent_id,
            lifecycle_state=LIFECYCLE_RESUMING,
            summary_template="Compass is resuming after output location was selected.",
        )
        task_store.resume_task(task_id)
        _record_major_step(
            task_store,
            task_id,
            step_key="compass.asking_output_mode",
            title="Compass accepted output location",
            agent=self.definition.agent_id,
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Compass accepted the output location: {output_mode}.",
            summary_facts={"output_mode": output_mode},
        )
        log.info("office output mode selected", output_mode=output_mode)

        # Fire-and-forget: spawn a daemon worker to run the actual office
        # dispatch and finalize the task state.  Returning WORKING immediately
        # unblocks the HTTP request so the UI can show "In Progress" without
        # blocking on a 5+ minute office roundtrip.  Mirrors the
        # `_complete_development_task` pattern used for development tasks
        # further up in this file.
        user_text = str(metadata.get("user_request") or "")
        office_artifact_metadata = {"agentId": self.definition.agent_id, "outputMode": output_mode}

        # Seed a "dispatching" artifact so the chat pane shows progress text
        # before the background worker finishes.
        dispatching_text = (
            f"Office task accepted with output mode: `{output_mode}`. "
            f"Compass is dispatching the request to the office agent now."
        )
        _record_major_step(
            task_store,
            task_id,
            step_key="compass.dispatched",
            title="Compass dispatching to Office Agent",
            agent=self.definition.agent_id,
            lifecycle_state=LIFECYCLE_DONE,
            summary_template="Compass dispatched the task to the Office Agent.",
        )
        _append_chat_entry(
            task_store,
            task_id,
            role="COMPASS",
            text=dispatching_text,
            tone="normal",
        )
        task_store.set_artifacts(
            task_id,
            [Artifact(
                name="compass-response",
                artifact_type="text/plain",
                parts=[{"text": dispatching_text}],
                metadata=office_artifact_metadata,
            )],
        )

        worker = threading.Thread(
            target=self._complete_office_task,
            kwargs={
                "task_id": task_id,
                "user_text": user_text,
                "office_request": dict(office_request),
            },
            daemon=True,
            name="compass-office-dispatch",
        )
        worker.start()
        print(
            f"[{self.definition.agent_id}] office dispatch started in background: "
            f"task_id={task_id} output_mode={output_mode!r}"
        )

        ui_update = {
            "task_id": task_id,
            "task_status": "TASK_STATE_WORKING",
            "chat_message": {
                "role": "COMPASS",
                "text": dispatching_text,
                "style": "normal",
            },
        }
        return {**task_store.get_task_dict(task_id), "ui_update": ui_update}

    def _complete_office_task(
        self,
        *,
        task_id: str,
        user_text: str,
        office_request: dict,
    ) -> None:
        """Background worker: dispatch the office task and finalize task state.

        Runs in a daemon thread spawned by ``resume_task`` (office branch).
        Mirrors ``_complete_development_task`` for symmetry — both are
        fire-and-forget workers that block on a synchronous A2A call and then
        update the task_store with the terminal result.

        Per-task isolation: every argument here is a snapshot of the resume
        payload captured at enqueue time, so multiple concurrent resumes do
        not share mutable state.  The ``task_store`` itself is thread-safe.
        """
        from framework.a2a.protocol import Artifact
        from framework.devlog import AgentLogger
        from framework.tools.registry import get_registry

        register_compass_tools()
        task_store = self.services.task_store
        # A task_store lookup failure here means compass was restarted between
        # the resume POST and this thread running.  In that case there's
        # nothing to update; log and exit cleanly.
        if task_store.get_task(task_id) is None:
            print(f"[compass] _complete_office_task: task {task_id} not found, skipping")
            return

        log = AgentLogger(task_id=task_id, agent_name=self.definition.agent_id)
        registry = get_registry()

        try:
            dispatch_data = _dispatch_office_request(task_id, user_text, office_request, registry, log)
        except Exception as exc:
            # The dispatch path is supposed to absorb its own errors, but a
            # late exception (e.g. launcher socket failure) must not crash the
            # daemon thread.  Surface it as a failed task and continue.
            log.error("office dispatch raised in background worker", error=str(exc))
            print(f"[compass] _complete_office_task: dispatch raised: {exc}")
            dispatch_data = {"status": "error", "message": str(exc)}

        # Re-check the task exists — a /terminate or restart could have
        # removed it while dispatch was running.
        if task_store.get_task(task_id) is None:
            log.warn("office task disappeared before finalization", task_id=task_id)
            return

        response_text = dispatch_data.get("message") or (
            f"Office task dispatched. Status: {dispatch_data.get('status', 'unknown')}"
        )
        office_failed = _office_dispatch_failed(dispatch_data)
        office_status = str(dispatch_data.get("status") or "").strip().lower()

        office_artifact_metadata = {"agentId": self.definition.agent_id}
        for key in ("summary", "message", "deliveryReportPath", "workspacePath", "status"):
            value = dispatch_data.get(key)
            if value not in (None, ""):
                office_artifact_metadata[key] = value
        if office_request.get("output_mode"):
            office_artifact_metadata["outputMode"] = office_request.get("output_mode")

        artifacts = [Artifact(
            name="compass-response",
            artifact_type="text/plain",
            parts=[{"text": response_text}],
            metadata=office_artifact_metadata,
        )]
        if office_failed:
            task_store.set_artifacts(task_id, artifacts)
            task_store.fail_task(task_id, response_text)
        else:
            task_store.complete_task(task_id, artifacts=artifacts, message=response_text)
        if office_failed:
            _record_major_step(
                task_store,
                task_id,
                step_key="compass.task_failed",
                title=f"Compass marking task failed: {response_text[:200]}",
                agent="compass",
                lifecycle_state=LIFECYCLE_FAILED,
                summary_template="Compass marked the task as failed: {failure_reason}.",
                summary_facts={"failure_reason": response_text[:500] or "office dispatch failed"},
            )
        else:
            # A6: per design doc §13.1 A6, always fire ``compass.task_completed``
            # on the happy path so the timeline has a consistent closing row,
            # regardless of the exact office_status (completed / success /
            # warning / unknown-but-not-failed). The ``office.delivered`` row
            # was previously written in the else branch but it is already
            # emitted by Office's own report_result node when applicable, so
            # we keep the canonical closure here as ``compass.task_completed``.
            _record_major_step(
                task_store,
                task_id,
                step_key="compass.task_completed",
                title="Compass marking task completed",
                agent="compass",
                lifecycle_state=LIFECYCLE_DONE,
                summary_template="Compass marked the task as completed.",
            )
        _append_chat_entry(
            task_store,
            task_id,
            role="COMPASS",
            text=response_text,
            tone="failed" if office_failed else "completed",
        )
        log.info(
            "office task finalization complete",
            task_id=task_id,
            status=office_status,
            failed=office_failed,
        )

    async def get_task(self, task_id: str) -> dict:
        """Return real task state from TaskStore."""
        return self.services.task_store.get_task_dict(task_id)

    def serve_ui(self, path: str) -> dict:
        """Handle UI-related requests."""
        return handle_ui_request("GET", path, self.services.task_store, _log_store_url())
