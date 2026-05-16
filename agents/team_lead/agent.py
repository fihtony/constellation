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
import os
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

    async def start(self) -> None:
        await super().start()
        _register_team_lead_dispatch(self)

    async def handle_message(self, message: dict) -> dict:
        from framework.a2a.protocol import Artifact, TaskState
        from framework.workflow import RunConfig

        register_team_lead_tools()

        # Extract message content
        msg = message.get("message", message)
        parts = msg.get("parts") or []
        user_text = next((p.get("text", "") for p in parts if p.get("text")), "")
        meta = msg.get("metadata") or {}

        task_store = self.services.task_store

        # Build workspace_path: {ARTIFACT_ROOT}/{orchestratorTaskId}/
        # Compass passes its task.id as orchestratorTaskId — this is the master task_id
        # that all agents in the workflow share for logging purposes.
        workspace_path = meta.get("workspacePath", "") or meta.get("workspace_path", "")
        orchestrator_task_id = meta.get("orchestratorTaskId", "")

        # Create team-lead task (its ID is a fallback when there's no orchestrator)
        task = task_store.create_task(
            agent_id=self.definition.agent_id,
            metadata={
                "orchestratorTaskId": orchestrator_task_id,
                "orchestratorCallbackUrl": meta.get("orchestratorCallbackUrl", ""),
            },
        )

        if not workspace_path:
            artifact_root = os.environ.get("ARTIFACT_ROOT", "artifacts/")
            if orchestrator_task_id:
                workspace_path = os.path.join(artifact_root, orchestrator_task_id)
            else:
                workspace_path = os.path.join(artifact_root, f"tl-{task.id[:8]}")

        state = {
            "user_request": user_text,
            "jira_key": meta.get("jiraKey", ""),
            "repo_url": meta.get("repoUrl", ""),
            "figma_url": meta.get("designUrl", "") or meta.get("figmaUrl", ""),
            "stitch_project_id": meta.get("stitchProjectId", ""),
            "stitch_screen_id": meta.get("stitchScreenId", ""),
            "jira_context": meta.get("jiraContext", {}),
            "design_context": meta.get("designContext"),
            "workspace_path": workspace_path,
            "metadata": meta,
            # _task_id: use Compass task ID as the master task ID for logging.
            # All agents in this workflow log under {ARTIFACT_ROOT}/{_task_id}/
            "_task_id": orchestrator_task_id or task.id,
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
                    timeout_seconds=3600,
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
                            "jiraInReview": result.get("jira_in_review", False),
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
            config = self._build_run_config(task_id, max_steps=50, timeout_seconds=3600)
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
                            "jiraInReview": result.get("jira_in_review", False) if isinstance(result, dict) else False,
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
                    "jiraInReview": result.get("jira_in_review", False),
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


def _register_team_lead_dispatch(team_lead_agent: "TeamLeadAgent") -> None:
    """Register in-process dispatch_development_task (overrides Compass's HTTP version)."""
    import asyncio
    import re as _re
    from framework.tools.base import BaseTool, ToolResult
    from framework.tools.registry import get_registry

    class InProcessDispatchDevelopmentTask(BaseTool):
        name = "dispatch_development_task"
        description = (
            "Dispatch a software development task (implement feature, fix bug, "
            "create PR, review code) to the Team Lead Agent.  Returns immediately "
            "after the task is submitted."
        )
        parameters_schema = {
            "type": "object",
            "properties": {
                "task_description": {"type": "string"},
                "jira_key": {"type": "string"},
                "repo_url": {"type": "string"},
                "design_url": {"type": "string"},
            },
            "required": ["task_description"],
        }

        def execute_sync(
            self,
            task_description: str = "",
            jira_key: str = "",
            repo_url: str = "",
            design_url: str = "",
            **kw,
        ) -> ToolResult:
            # Sanitize jira_key
            if jira_key:
                m = _re.search(r"[A-Z][A-Z0-9]+-\d+", jira_key)
                jira_key = m.group(0) if m else ""
            # Also try to extract from task_description
            if not jira_key:
                m = _re.search(r"[A-Z][A-Z0-9]+-\d+", task_description)
                if m:
                    jira_key = m.group(0)

            # Validate repo_url is a real SCM host, not a Jira URL
            _scm_hosts = ("github.com", "bitbucket.org", "gitlab.com", "dev.azure.com")
            if repo_url and not any(h in repo_url for h in _scm_hosts):
                print(f"[tl-dispatch] Ignoring non-SCM repo_url: {repo_url!r}")
                repo_url = ""
            effective_repo_url = repo_url or os.environ.get("SCM_REPO_URL", "")
            effective_workspace = os.environ.get("TL_WORKSPACE_PATH", "")
            print(f"[tl-dispatch] Dispatching: jira={jira_key} repo={effective_repo_url}")

            task_id_holder: dict = {}

            def _run() -> None:
                loop = asyncio.new_event_loop()
                try:
                    msg = {
                        "message": {
                            "parts": [{"text": task_description}],
                            "metadata": {
                                "jiraKey": jira_key,
                                "repoUrl": effective_repo_url,
                                "designUrl": design_url,
                                "workspacePath": effective_workspace,
                            },
                        }
                    }
                    result = loop.run_until_complete(team_lead_agent.handle_message(msg))
                    task_id_holder["task_id"] = result["task"]["id"]
                    print(f"[tl-dispatch] Team Lead task started: {task_id_holder['task_id']}")
                except Exception as exc:
                    task_id_holder["error"] = str(exc)
                    print(f"[tl-dispatch] Team Lead start error: {exc}")
                finally:
                    loop.close()

            t = threading.Thread(target=_run, daemon=True, name="tl-dispatch")
            t.start()
            # Wait briefly to capture task_id
            t.join(timeout=5.0)

            task_id = task_id_holder.get("task_id", "")
            if task_id_holder.get("error"):
                return ToolResult(output=json.dumps({
                    "status": "error",
                    "message": task_id_holder["error"],
                }))

            return ToolResult(output=json.dumps({
                "status": "submitted",
                "taskId": task_id,
                "message": f"Development task dispatched to Team Lead (jira={jira_key}).",
            }))

    registry = get_registry()
    # Force-register (unregister Compass's HTTP version first if present)
    try:
        registry.unregister("dispatch_development_task")
    except Exception:
        pass
    registry.register(InProcessDispatchDevelopmentTask())
    print("[team-lead] Registered in-process dispatch_development_task")
