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
import re

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode
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

    # Obvious office: document operation verbs
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
        task = task_store.create_task(agent_id=self.definition.agent_id)

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
            log.info("dispatch complete", status=status, tl_task_id=task_id_tl,
                     pr_url=dispatch_data.get("prUrl", ""))
            log.a2a("←", "team-lead", status=status, tl_task_id=task_id_tl)
            response_text = (
                f"Development task dispatched to Team Lead.\n"
                f"Jira: {jira_key or 'N/A'}  Status: {status}  TL task: {task_id_tl}"
            )
            print(f"[{_aid}] dispatch result: status={status} taskId={task_id_tl}")

        elif task_type == "office":
            log.info("dispatching office task")
            try:
                dispatch_result_str = registry.execute_sync(
                    "dispatch_office_task",
                    {"task_description": user_text},
                )
                dispatch_data = json.loads(dispatch_result_str) if dispatch_result_str else {}
            except Exception as exc:
                dispatch_data = {"status": "error", "message": str(exc)}
                log.error("dispatch_office_task failed", error=str(exc))
                print(f"[{_aid}] dispatch_office_task error: {exc}")
            log.info("office dispatch complete", status=dispatch_data.get("status", "unknown"))
            response_text = f"Office task dispatched. Status: {dispatch_data.get('status', 'unknown')}"

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
        return task_store.get_task_dict(task.id)

    async def get_task(self, task_id: str) -> dict:
        """Return real task state from TaskStore."""
        return self.services.task_store.get_task_dict(task_id)
