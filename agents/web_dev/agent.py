"""Web Dev Agent — full-stack development execution.

Architecture: **Graph outside, ReAct inside**.

Uses a declarative graph workflow for the macro lifecycle:
  setup_workspace → analyze_task → implement_changes → run_tests → create_pr → report

Individual nodes (especially implement_changes and fix_tests) use bounded
ReAct via the runtime for open-ended code generation and repair.
"""
from __future__ import annotations

import json
import threading

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode
from framework.workflow import Workflow, START, END
from agents.web_dev.nodes import (
    prepare_jira,
    setup_workspace,
    analyze_task,
    implement_changes,
    run_tests,
    fix_tests,
    self_assess,
    fix_gaps,
    capture_screenshot,
    create_pr,
    update_jira,
    report_result,
)

# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

web_dev_workflow = Workflow(
    name="web_dev",
    edges=[
        (START, prepare_jira, setup_workspace),
        (setup_workspace, analyze_task),
        (analyze_task, implement_changes),
        (implement_changes, run_tests),
        (run_tests, {
            "pass": self_assess,
            "fail": fix_tests,
        }),
        (fix_tests, run_tests),
        (self_assess, {
            "pass": capture_screenshot,
            "fail": fix_gaps,
        }),
        (fix_gaps, run_tests),
        (capture_screenshot, create_pr),
        (create_pr, update_jira),
        (update_jira, report_result),
        (report_result, END),
    ],
)

# ---------------------------------------------------------------------------
# Agent definition — derived from config.yaml (single source of truth)
# ---------------------------------------------------------------------------

def _build_web_dev_definition() -> AgentDefinition:
    """Build Web Dev's AgentDefinition from YAML config + workflow."""
    from framework.config import build_agent_definition_from_config

    try:
        cfg = build_agent_definition_from_config("web-dev")
    except Exception:
        cfg = {}
    return AgentDefinition(
        agent_id=cfg.get("agent_id", "web-dev"),
        name=cfg.get("name", "Web Dev Agent"),
        description=cfg.get("description", "Full-stack web development: clone, branch, implement, test, PR"),
        mode=AgentMode.TASK,
        execution_mode=ExecutionMode.PER_TASK,
        workflow=web_dev_workflow,
        skills=cfg.get("skills", ["react-nextjs", "testing"]),
        tools=cfg.get("tools", [
            "read_file", "write_file", "edit_file", "search_code", "run_command",
            "scm_clone", "scm_branch", "scm_commit", "scm_push", "scm_create_pr",
            "jira_transition", "jira_comment", "jira_update",
            "jira_list_transitions", "jira_get_token_user", "jira_list_comments",
        ]),
        permissions=cfg.get("permissions", {"scm": "read-write", "filesystem": "workspace-only"}),
        permission_profile=cfg.get("permission_profile", "development"),
        config=cfg.get("config", {}),
    )


web_dev_definition = _build_web_dev_definition()


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

class WebDevAgent(BaseAgent):
    """Web Dev Agent implementation with graph-first lifecycle."""

    async def start(self) -> None:
        """Initialize agent and register boundary tools."""
        await super().start()
        from agents.web_dev.tools import register_web_dev_tools
        register_web_dev_tools()

    async def handle_message(self, message: dict) -> dict:
        from framework.a2a.protocol import Artifact
        from framework.workflow import RunConfig

        msg = message.get("message", message)
        parts = msg.get("parts", [])
        user_text = parts[0].get("text", "") if parts else ""
        metadata = msg.get("metadata", {})

        # Create task via TaskStore
        task_store = self.services.task_store
        task = task_store.create_task(
            agent_id=self.definition.agent_id,
            metadata={
                "orchestratorTaskId": metadata.get("orchestratorTaskId", ""),
                "orchestratorCallbackUrl": metadata.get("orchestratorCallbackUrl", ""),
            },
        )

        state = {
            "_task_id": task.id,
            "_runtime": self.services.runtime,
            "_skills_registry": self.skills_registry,
            "_plugin_manager": self.plugin_manager,
            "user_request": user_text,
            "repo_url": metadata.get("repoUrl", ""),
            "branch_name": metadata.get("branchName", ""),
            "jira_context": metadata.get("jiraContext", {}),
            "design_context": metadata.get("designContext"),
            "skill_context": metadata.get("skillContext", ""),
            "task_type": metadata.get("taskType", "general"),
            "analysis": metadata.get("analysis", ""),
            "test_cycles": 0,
            "metadata": metadata,
        }

        def _run() -> None:
            import asyncio

            loop = asyncio.new_event_loop()
            try:
                # Recall relevant past context before starting
                memory_context = loop.run_until_complete(
                    self.recall_task_context(user_text or "web development task")
                )
                if memory_context:
                    state["memory_context"] = memory_context

                config = self._build_run_config(
                    task.id,
                    max_steps=30,
                    timeout_seconds=600,
                )
                result = loop.run_until_complete(
                    self._compiled_workflow.invoke(state, config)
                )
                artifacts = [
                    Artifact(
                        name="web-dev-result",
                        artifact_type="text/plain",
                        parts=[{"text": result.get("implementation_summary", "Done.")}],
                        metadata={
                            "agentId": self.definition.agent_id,
                            "prUrl": result.get("pr_url", ""),
                            "branch": result.get("branch_name", ""),
                        },
                    )
                ]
                task_store.complete_task(task.id, artifacts=artifacts)

                # Consolidate task result into memory for future recall
                loop.run_until_complete(
                    self.consolidate_task_result(
                        summary=result.get("implementation_summary", ""),
                        tags=["web-dev", metadata.get("taskType", "general")],
                    )
                )

                # Send callback if URL provided
                callback_url = metadata.get("orchestratorCallbackUrl", "")
                if callback_url:
                    _send_callback(
                        callback_url, task.id, result, self.definition.agent_id
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


def _send_callback(
    callback_url: str, task_id: str, result: dict, agent_id: str
) -> None:
    """POST completion callback to orchestrator (best-effort)."""
    from urllib.request import Request, urlopen

    payload = {
        "downstreamTaskId": task_id,
        "state": "TASK_STATE_COMPLETED",
        "statusMessage": result.get("implementation_summary", ""),
        "artifacts": [
            {
                "name": "web-dev-result",
                "artifactType": "text/plain",
                "parts": [{"text": result.get("implementation_summary", "")}],
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
        print(f"[web-dev] Callback failed: {exc}")
