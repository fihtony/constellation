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

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode
from agents.compass.ui.routes import handle_ui_request
from agents.compass.tools import TOOL_NAMES, register_compass_tools

compass_definition = AgentDefinition(
    agent_id="compass",
    name="Compass Agent",
    description="Control plane: task classification, routing, and user summary (ReAct-first)",
    mode=AgentMode.CHAT,
    execution_mode=ExecutionMode.PERSISTENT,
    workflow=None,
    tools=TOOL_NAMES,
)


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
            max_tokens=16,
        )
        raw = (result.get("raw_response") or "").strip().lower()
        # Accept partial matches — LLM may output "development\n" or "development."
        if "development" in raw:
            return "development"
        if "office" in raw:
            return "office"
        if "general" in raw:
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
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return []


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
    return {
        "source_paths": _normalize_source_paths(
            metadata.get("source_paths") or metadata.get("officeTargetPaths") or metadata.get("filePath")
        ),
        "capability": _normalize_office_capability(
            str(metadata.get("capability") or metadata.get("requestedCapability") or ""),
            user_text,
        ),
        "output_mode": _normalize_output_mode(
            str(metadata.get("output_mode") or metadata.get("officeOutputMode") or "")
        ),
    }


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
        office_url = rc.discover(requested_capability)
        if not office_url:
            office_url = rc.discover("office.agent")
        discovered_from_registry = bool(office_url)
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
            log.info("office delivery verified", task_report=report_path)
        else:
            log.warn("office delivery report missing", task_report=report_path)

    log.info("office dispatch complete", status=dispatch_data.get("status", "unknown"))
    return dispatch_data


class CompassAgent(BaseAgent):
    """Compass Agent -- routes requests via heuristic + LLM classification."""

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
        task = task_store.create_task(agent_id=self.definition.agent_id, metadata={"user_request": user_text})

        runtime = self.services.runtime or get_runtime()
        registry = get_registry()

        # --- Classify ---
        task_type = _classify_request(user_text, runtime)
        _aid = self.definition.agent_id
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
        if task_type == "development":
            jira_key = _extract_jira_key(user_text)
            log.info("dispatching development task", jira_key=jira_key)
            log.a2a("→", "team-lead", capability="dispatch_development_task", jira_key=jira_key)
            print(f"[{_aid}] dispatching development task: jira_key={jira_key!r}")
            try:
                dispatch_result_str = registry.execute_sync(
                    "dispatch_development_task",
                    {
                        "task_description": user_text,
                        "jira_key": jira_key,
                        "orchestratorTaskId": task.id,
                        "workspacePath": workspace_path,
                    },
                )
                dispatch_data = json.loads(dispatch_result_str) if dispatch_result_str else {}
            except Exception as exc:
                dispatch_data = {"status": "error", "message": str(exc)}
                log.error("dispatch_development_task failed", error=str(exc))
                print(f"[{_aid}] dispatch_development_task error: {exc}")

            status = dispatch_data.get("status", "unknown")
            task_id_tl = dispatch_data.get("taskId", "N/A")
            display_status = "dispatched" if status not in ("error", "failed", "unknown") else status
            log.info("dispatch complete", status=display_status, tl_task_id=task_id_tl,
                     pr_url=dispatch_data.get("prUrl", ""))
            log.a2a("←", "team-lead", status=display_status, tl_task_id=task_id_tl)
            response_text = (
                f"Development task dispatched to Team Lead.\n"
                f"Jira: {jira_key or 'N/A'}  Status: {display_status}  TL task: {task_id_tl}"
            )
            print(f"[{_aid}] dispatch result: status={display_status} taskId={task_id_tl}")

        elif task_type == "office":
            log.info("dispatching office task")
            office_request = _extract_office_request(user_text, meta)
            task_store.update_metadata(task.id, {"task_type": "office", "office_request": office_request})

            if not office_request.get("output_mode"):
                question = _office_output_mode_question()
                task_store.pause_task(
                    task.id,
                    question=question,
                    interrupt_metadata={"kind": "office_output_mode", "office_request": office_request},
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

        log.info("task complete", response_len=len(response_text))
        artifacts = [Artifact(
            name="compass-response",
            artifact_type="text/plain",
            parts=[{"text": response_text}],
            metadata={"agentId": _aid},
        )]
        task_store.complete_task(task.id, artifacts=artifacts)

        # Build UI-friendly response with ui_update for frontend rendering
        display_status = dispatch_data.get("status", "unknown") if task_type == "development" else (
            dispatch_data.get("status", "unknown") if task_type == "office" else "completed"
        )
        ui_update = {
            "task_id": task.id,
            "task_status": task.status.state.value,
            "chat_message": {
                "role": "COMPASS",
                "text": response_text,
                "style": "failed" if display_status in ("error", "failed", "unknown") else "normal",
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

        metadata = task.metadata or {}
        if metadata.get("task_type") != "office":
            return await super().resume_task(task_id, resume_value)

        register_compass_tools()
        registry = get_registry()
        log = AgentLogger(task_id=task_id, agent_name=self.definition.agent_id)

        office_request = dict(metadata.get("office_request") or {})
        output_mode = _normalize_output_mode(str(resume_value))
        if not output_mode:
            question = "Please reply with `workspace` or `inplace` so I can route the office task correctly."
            task_store.pause_task(
                task_id,
                question=question,
                interrupt_metadata={"kind": "office_output_mode", "office_request": office_request},
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
        task_store.resume_task(task_id)
        log.info("office output mode selected", output_mode=output_mode)

        user_text = str(metadata.get("user_request") or "")
        dispatch_data = _dispatch_office_request(task_id, user_text, office_request, registry, log)
        response_text = dispatch_data.get("message") or f"Office task dispatched. Status: {dispatch_data.get('status', 'unknown')}"

        artifacts = [Artifact(
            name="compass-response",
            artifact_type="text/plain",
            parts=[{"text": response_text}],
            metadata={"agentId": self.definition.agent_id},
        )]
        task_store.complete_task(task_id, artifacts=artifacts, message=response_text)

        display_status = dispatch_data.get("status", "unknown")
        ui_update = {
            "task_id": task_id,
            "task_status": task_store.get_task(task_id).status.state.value if task_store.get_task(task_id) else "TASK_STATE_COMPLETED",
            "chat_message": {
                "role": "COMPASS",
                "text": response_text,
                "style": "failed" if display_status in ("error", "failed", "unknown") else "normal",
            },
        }
        return {**task_store.get_task_dict(task_id), "ui_update": ui_update}

    async def get_task(self, task_id: str) -> dict:
        """Return real task state from TaskStore."""
        return self.services.task_store.get_task_dict(task_id)

    def serve_ui(self, path: str) -> dict:
        """Handle UI-related requests."""
        return handle_ui_request("GET", path, self.services.task_store)
