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

    Uses heuristics first for speed and reliability, falls back to LLM for
    ambiguous cases.
    """
    lower = user_text.lower()

    # Strong development signals: Jira key/URL present + action keyword
    has_jira_url = bool(re.search(
        r"https?://[^\s]+/browse/[A-Z][A-Z0-9]+-\d+|jira[^\s]*/browse/", user_text
    ))
    has_jira_key = bool(re.search(r"\b[A-Z][A-Z0-9]+-\d+\b", user_text))
    has_dev_action = any(kw in lower for kw in [
        "implement", "implement the", "implement jira", "fix bug", "create pr",
        "create a pr", "feature", "develop", "code review", "pull request",
    ])
    if (has_jira_url or has_jira_key) and has_dev_action:
        return "development"
    if has_jira_url or "implement the jira ticket" in lower:
        return "development"

    # Office signals
    if any(kw in lower for kw in [
        "summarize", "pdf", "docx", "spreadsheet", "document", "folder", "organize files",
    ]):
        return "office"

    # Fallback: single-shot LLM classification (fast, no tool calling needed)
    try:
        from agents.compass.prompts.triage import TRIAGE_SYSTEM, TRIAGE_TEMPLATE
        result = runtime.run(
            prompt=TRIAGE_TEMPLATE.format(user_request=user_text),
            system_prompt=TRIAGE_SYSTEM,
            max_tokens=256,
        )
        raw = (result.get("raw_response") or "").strip().lower()
        if "development" in raw:
            return "development"
        if "office" in raw:
            return "office"
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
        from framework.a2a.protocol import Artifact
        from framework.instructions import load_instructions
        from framework.runtime.adapter import get_runtime
        from framework.tools.registry import get_registry

        register_compass_tools()

        msg = message.get("message", message)
        parts = msg.get("parts") or []
        user_text = next((p.get("text", "") for p in parts if p.get("text")), "")

        # Create task via TaskStore
        task_store = self.services.task_store
        task = task_store.create_task(agent_id=self.definition.agent_id)

        runtime = self.services.runtime or get_runtime()
        registry = get_registry()

        # --- Classify ---
        task_type = _classify_request(user_text, runtime)
        print(f"[compass] task_type={task_type!r} request={user_text[:120]!r}")

        # --- Dispatch ---
        if task_type == "development":
            jira_key = _extract_jira_key(user_text)
            print(f"[compass] dispatching development task: jira_key={jira_key!r}")
            try:
                dispatch_result_str = registry.execute_sync(
                    "dispatch_development_task",
                    {"task_description": user_text, "jira_key": jira_key},
                )
                dispatch_data = json.loads(dispatch_result_str) if dispatch_result_str else {}
            except Exception as exc:
                dispatch_data = {"status": "error", "message": str(exc)}
                print(f"[compass] dispatch_development_task error: {exc}")

            status = dispatch_data.get("status", "unknown")
            task_id_tl = dispatch_data.get("taskId", "N/A")
            response_text = (
                f"Development task dispatched to Team Lead.\n"
                f"Jira: {jira_key or 'N/A'}  Status: {status}  TL task: {task_id_tl}"
            )
            print(f"[compass] dispatch result: status={status} taskId={task_id_tl}")

        elif task_type == "office":
            try:
                dispatch_result_str = registry.execute_sync(
                    "dispatch_office_task",
                    {"task_description": user_text},
                )
                dispatch_data = json.loads(dispatch_result_str) if dispatch_result_str else {}
            except Exception as exc:
                dispatch_data = {"status": "error", "message": str(exc)}
                print(f"[compass] dispatch_office_task error: {exc}")
            response_text = f"Office task dispatched. Status: {dispatch_data.get('status', 'unknown')}"

        else:
            # General conversational task — use LLM for a direct answer
            system_prompt = load_instructions("compass")
            agentic_result = runtime.run_agentic(
                task=user_text,
                tools=None,
                system_prompt=system_prompt,
                max_turns=5,
                timeout=120,
            )
            response_text = agentic_result.summary or "I can help you with that."

        artifacts = [Artifact(
            name="compass-response",
            artifact_type="text/plain",
            parts=[{"text": response_text}],
            metadata={"agentId": "compass"},
        )]
        task_store.complete_task(task.id, artifacts=artifacts)
        return task_store.get_task_dict(task.id)

    async def get_task(self, task_id: str) -> dict:
        """Return real task state from TaskStore."""
        return self.services.task_store.get_task_dict(task_id)
