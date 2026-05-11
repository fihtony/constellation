"""Team Lead Agent — graph-first intelligence layer.

Architecture: **Graph outside, ReAct inside**.

The Team Lead uses a declarative graph workflow for its macro lifecycle:
  receive → analyze → gather_context → plan → dispatch_dev → review → report

Each node may internally use LLM reasoning (single-shot or bounded ReAct)
for local decisions, but the overall progression is driven by the graph.

Instructions (per-node prompts) live in:
  agents/team_lead/prompts/

Tools live in:
  agents/team_lead/tools.py
"""
from __future__ import annotations

import json
import threading
from typing import Any

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode
from framework.workflow import Workflow, START, END
from framework.state import Channel, append_reducer
from agents.team_lead.nodes import (
    receive_task,
    analyze_requirements,
    gather_context,
    create_plan,
    dispatch_dev_agent,
    review_result,
    request_revision,
    report_success,
    escalate_to_user,
)
from agents.team_lead.tools import register_team_lead_tools

# ---------------------------------------------------------------------------
# State schema — declares how keys are merged across nodes
# ---------------------------------------------------------------------------

_team_lead_state_schema = {
    "required_skills": Channel(reducer=append_reducer),
}

# ---------------------------------------------------------------------------
# Workflow definition (graph-first)
# ---------------------------------------------------------------------------

team_lead_workflow = Workflow(
    name="team_lead",
    edges=[
        (START, receive_task, analyze_requirements),
        (analyze_requirements, gather_context),
        (gather_context, create_plan),
        (create_plan, dispatch_dev_agent),
        (dispatch_dev_agent, review_result),
        (review_result, {
            "approved": report_success,
            "needs_revision": request_revision,
            "need_user_input": escalate_to_user,
        }),
        (request_revision, dispatch_dev_agent),  # loop back
        (report_success, END),
        (escalate_to_user, {
            "user_responded": dispatch_dev_agent,  # resume: user provided guidance
        }),
    ],
    state_schema=_team_lead_state_schema,
)

# ---------------------------------------------------------------------------
# Agent definition — derived from config.yaml (single source of truth)
# ---------------------------------------------------------------------------

def _build_team_lead_definition() -> AgentDefinition:
    """Build Team Lead's AgentDefinition from YAML config + workflow."""
    from framework.config import build_agent_definition_from_config

    try:
        cfg = build_agent_definition_from_config("team-lead")
    except Exception:
        # Fallback if config loading fails (e.g. in minimal test environments)
        cfg = {}
    return AgentDefinition(
        agent_id=cfg.get("agent_id", "team-lead"),
        name=cfg.get("name", "Team Lead Agent"),
        description=cfg.get(
            "description",
            "Intelligence layer: analysis, context gathering, planning, "
            "dev dispatch, code review coordination (graph-first, ReAct-inside-nodes)",
        ),
        mode=AgentMode.TASK,
        execution_mode=ExecutionMode.PERSISTENT,
        workflow=team_lead_workflow,
        tools=cfg.get("tools", [
            "fetch_jira_ticket",
            "fetch_design",
            "dispatch_web_dev",
            "dispatch_code_review",
            "request_clarification",
        ]),
        permission_profile=cfg.get("permission_profile", ""),
        permissions=cfg.get("permissions", {}),
        config=cfg.get("config", {}),
    )


team_lead_definition = _build_team_lead_definition()


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

class TeamLeadAgent(BaseAgent):
    """Team Lead Agent — graph-first with ReAct-inside-nodes."""

    async def handle_message(self, message: dict) -> dict:
        from framework.a2a.protocol import Artifact, TaskState
        from framework.workflow import RunConfig

        register_team_lead_tools()

        # Extract message content
        msg = message.get("message", message)
        parts = msg.get("parts") or []
        user_text = next((p.get("text", "") for p in parts if p.get("text")), "")
        meta = msg.get("metadata") or {}

        # Create task in task store
        task_store = self.services.task_store
        task = task_store.create_task(
            agent_id=self.definition.agent_id,
            metadata={
                "orchestratorTaskId": meta.get("orchestratorTaskId", ""),
                "orchestratorCallbackUrl": meta.get("orchestratorCallbackUrl", ""),
            },
        )

        # Build initial workflow state
        state = {
            "user_request": user_text,
            "jira_key": meta.get("jiraKey", ""),
            "repo_url": meta.get("repoUrl", ""),
            "figma_url": meta.get("designUrl", "") or meta.get("figmaUrl", ""),
            "stitch_project_id": meta.get("stitchProjectId", ""),
            "jira_context": meta.get("jiraContext", {}),
            "design_context": meta.get("designContext"),
            "metadata": meta,
            "_task_id": task.id,
            "_agent_id": self.definition.agent_id,
            "_runtime": self.services.runtime,
            "_skills_registry": self.skills_registry,
            "_plugin_manager": self.plugin_manager,
        }

        # Run workflow in background thread
        def _run() -> None:
            import asyncio

            from framework.errors import InterruptSignal

            loop = asyncio.new_event_loop()
            try:
                config = self._build_run_config(
                    task.id,
                    max_steps=50,
                    timeout_seconds=900,
                )
                result = loop.run_until_complete(
                    self._compiled_workflow.invoke(state, config)
                )
                # Build artifacts from result
                artifacts = [
                    Artifact(
                        name="team-lead-response",
                        artifact_type="text/plain",
                        parts=[{"text": result.get("report_summary", "")}],
                        metadata={
                            "agentId": self.definition.agent_id,
                            "orchestratorTaskId": meta.get("orchestratorTaskId", ""),
                            "prUrl": result.get("pr_url", ""),
                            "branch": result.get("branch_name", ""),
                        },
                    )
                ]
                task_store.complete_task(task.id, artifacts=artifacts)

                # Send callback if URL provided
                callback_url = meta.get("orchestratorCallbackUrl", "")
                if callback_url:
                    _send_callback(
                        callback_url, task.id, result, self.definition.agent_id
                    )
            except InterruptSignal as sig:
                task_store.pause_task(
                    task.id,
                    question=sig.question,
                    interrupt_metadata=sig.metadata,
                )
                # Send INPUT_REQUIRED callback if URL provided
                callback_url = meta.get("orchestratorCallbackUrl", "")
                if callback_url:
                    _send_input_required_callback(
                        callback_url, task.id, sig.question, self.definition.agent_id
                    )
            except Exception as e:
                task_store.fail_task(task.id, str(e))
            finally:
                loop.close()

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()

        return task_store.get_task_dict(task.id)

    async def get_task(self, task_id: str) -> dict:
        """Return real task state from TaskStore."""
        return self.services.task_store.get_task_dict(task_id)

    async def resume_task(self, task_id: str, resume_value: Any) -> dict:
        """Resume a paused Team Lead task and send callback on completion.

        Overrides BaseAgent.resume_task() to add callback delivery
        (both COMPLETED and re-interrupted INPUT_REQUIRED).
        """
        from framework.a2a.protocol import Artifact
        from framework.errors import InterruptSignal

        task_store = self.services.task_store
        task = task_store.get_task(task_id)
        if task is None:
            raise RuntimeError(f"Task {task_id} not found")

        callback_url = (task.metadata or {}).get("orchestratorCallbackUrl", "")
        task_store.resume_task(task_id)

        if self._compiled_workflow and self.checkpoint_service:
            config = self._build_run_config(task_id, max_steps=50, timeout_seconds=900)
            try:
                result = await self._compiled_workflow.resume(config, resume_value)
                summary = (
                    result.get("report_summary")
                    or result.get("analysis_summary")
                    or "Resumed and completed"
                ) if isinstance(result, dict) else "Resumed and completed"
                artifacts = [
                    Artifact(
                        name="team-lead-response",
                        artifact_type="text/plain",
                        parts=[{"text": summary}],
                        metadata={
                            "agentId": self.definition.agent_id,
                            "orchestratorTaskId": (task.metadata or {}).get("orchestratorTaskId", ""),
                            "prUrl": result.get("pr_url", "") if isinstance(result, dict) else "",
                            "branch": result.get("branch_name", "") if isinstance(result, dict) else "",
                        },
                    )
                ]
                task_store.complete_task(task_id, artifacts=artifacts, message=summary)

                if callback_url:
                    _send_callback(
                        callback_url, task_id,
                        result if isinstance(result, dict) else {},
                        self.definition.agent_id,
                    )
            except InterruptSignal as sig:
                task_store.pause_task(
                    task_id,
                    question=sig.question,
                    interrupt_metadata=sig.metadata,
                )
                if callback_url:
                    _send_input_required_callback(
                        callback_url, task_id, sig.question, self.definition.agent_id,
                    )
            except Exception as exc:
                task_store.fail_task(task_id, str(exc))

        return task_store.get_task_dict(task_id)


def _send_callback(
    callback_url: str, task_id: str, result: dict, agent_id: str
) -> None:
    """POST completion callback to orchestrator (best-effort)."""
    from urllib.request import Request, urlopen

    payload = {
        "downstreamTaskId": task_id,
        "state": "TASK_STATE_COMPLETED",
        "statusMessage": result.get("report_summary", ""),
        "artifacts": [
            {
                "name": "team-lead-response",
                "artifactType": "text/plain",
                "parts": [{"text": result.get("report_summary", "")}],
                "metadata": {
                    "agentId": agent_id,
                    "prUrl": result.get("pr_url", ""),
                    "branch": result.get("branch_name", ""),
                },
            }
        ],
        "agentId": agent_id,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        callback_url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=10):
            pass
    except Exception as exc:
        print(f"[team-lead] Callback failed: {exc}")


def _send_input_required_callback(
    callback_url: str, task_id: str, question: str, agent_id: str
) -> None:
    """POST INPUT_REQUIRED callback to orchestrator (best-effort)."""
    from urllib.request import Request, urlopen

    payload = {
        "downstreamTaskId": task_id,
        "state": "TASK_STATE_INPUT_REQUIRED",
        "statusMessage": question,
        "agentId": agent_id,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = Request(
        callback_url,
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=10):
            pass
    except Exception as exc:
        print(f"[team-lead] INPUT_REQUIRED callback failed: {exc}")
