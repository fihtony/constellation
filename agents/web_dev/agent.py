"""Web Dev Agent — full-stack development execution.

Architecture: **Graph outside, ReAct inside**.

Uses a declarative graph workflow for the macro lifecycle:
  setup_workspace → analyze_task → implement_changes → run_tests → create_pr → report

Individual nodes (especially implement_changes and fix_tests) use bounded
ReAct via the runtime for open-ended code generation and repair.
"""
from __future__ import annotations

import json
import os
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
    pause_for_user_input,
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
            "need_user_input": pause_for_user_input,
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
        from agents.web_dev.coding_tools import register_web_dev_coding_tools
        register_web_dev_tools()
        register_web_dev_coding_tools()
        _register_web_dev_dispatch(self)

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
            "repo_path": metadata.get("repoPath", ""),
            "workspace_path": metadata.get("workspacePath", ""),
            "branch_name": metadata.get("branchName", ""),
            "jira_context": metadata.get("jiraContext", {}),
            # Derive jira_key from jiraContext so prepare_jira / update_jira can resolve it
            "jira_key": (
                (metadata.get("jiraContext") or {}).get("key", "")
                or metadata.get("jiraKey", "")
            ),
            "design_context": metadata.get("designContext"),
            "design_code_path": metadata.get("designCodePath", ""),
            "skill_context": metadata.get("skillContext", ""),
            "context_manifest_path": metadata.get("contextManifestPath", ""),
            "jira_files": metadata.get("jiraFiles", []),
            "design_files": metadata.get("designFiles", []),
            "tech_stack": metadata.get("techStack", []),
            "stitch_screen_name": metadata.get("stitchScreenName", ""),
            "task_type": metadata.get("taskType", "general"),
            "analysis": metadata.get("analysis", ""),
            "revision_feedback": metadata.get("revisionFeedback", ""),
            "definition_of_done": metadata.get("definitionOfDone", {}),
            "test_cycles": 0,
            # max_test_cycles: can be set by caller via metadata, else uses env default.
            # WEB_DEV_MAX_TEST_CYCLES env var (default 3) controls production cycles.
            # Set to 2 in tests/.env for faster E2E runs.
            "max_test_cycles": metadata.get("maxTestCycles") or int(
                os.environ.get("WEB_DEV_MAX_TEST_CYCLES", "3")
            ),
            "metadata": metadata,
            # Populate _allowed_tools from the permission engine so run_agentic
            # only exposes the development-profile tool list to the LLM, rather
            # than the entire global registry.
            "_allowed_tools": (
                self._permission_engine.permissions.allowed_tools[:]
                if self._permission_engine
                else None
            ),
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
                    max_steps=50,
                    timeout_seconds=int(os.environ.get("WEB_DEV_WORKFLOW_TIMEOUT", "7200")),
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
                            "jiraInReview": result.get("jira_in_review", False),
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
        print(f"[web-dev] Callback failed: {exc}")


def _register_web_dev_dispatch(web_dev_agent: "WebDevAgent") -> None:
    """Register in-process dispatch_web_dev tool (overrides idempotent HTTP version)."""
    import asyncio
    import time
    from framework.tools.base import BaseTool, ToolResult
    from framework.tools.registry import get_registry

    class InProcessDispatchWebDev(BaseTool):
        name = "dispatch_web_dev"
        description = (
            "Dispatch a web development implementation task to the Web Dev Agent. "
            "Include all gathered context: Jira ticket details, design spec, repo URL."
        )
        parameters_schema = {
            "type": "object",
            "properties": {
                "task_description": {"type": "string"},
                "jira_context": {"type": "object"},
                "design_context": {"type": "object"},
                "design_code_path": {"type": "string"},
                "repo_url": {"type": "string"},
                "repo_path": {"type": "string"},
                "workspace_path": {"type": "string"},
                "context_manifest_path": {"type": "string"},
                "jira_files": {"type": "array", "items": {"type": "string"}},
                "design_files": {"type": "array", "items": {"type": "string"}},
                "revision_feedback": {"type": "string"},
                "definition_of_done": {"type": "object"},
            },
            "required": ["task_description"],
        }

        def execute_sync(
            self,
            task_description: str = "",
            jira_context=None,
            design_context=None,
            design_code_path: str = "",
            repo_url: str = "",
            repo_path: str = "",
            workspace_path: str = "",
            context_manifest_path: str = "",
            jira_files=None,
            design_files=None,
            revision_feedback: str = "",
            definition_of_done=None,
            **kw,
        ) -> ToolResult:
            task_id_holder: dict = {}

            def _run() -> None:
                loop = asyncio.new_event_loop()
                try:
                    msg = {
                        "message": {
                            "parts": [{"text": task_description}],
                            "metadata": {
                                "jiraContext": jira_context or {},
                                "designContext": design_context,
                                "designCodePath": design_code_path,
                                "repoUrl": repo_url,
                                "repoPath": repo_path,
                                "workspacePath": workspace_path,
                                "contextManifestPath": context_manifest_path,
                                "jiraFiles": jira_files or [],
                                "designFiles": design_files or [],
                                "revisionFeedback": revision_feedback,
                                "definitionOfDone": definition_of_done or {},
                            },
                        }
                    }
                    result = loop.run_until_complete(web_dev_agent.handle_message(msg))
                    task_id_holder["task_id"] = result["task"]["id"]
                except Exception as exc:
                    task_id_holder["error"] = str(exc)
                finally:
                    loop.close()

            t = threading.Thread(target=_run, daemon=True)
            t.start()
            t.join(timeout=10.0)

            task_id = task_id_holder.get("task_id")
            if not task_id:
                err = task_id_holder.get("error", "Task ID unavailable")
                print(f"[web-dev-dispatch] Failed to start: {err}")
                return ToolResult(output=json.dumps({"status": "error", "summary": err}))

            print(f"[web-dev-dispatch] Task started: {task_id}")
            deadline = time.monotonic() + 1800
            while time.monotonic() < deadline:
                td = web_dev_agent.services.task_store.get_task_dict(task_id)
                state = td["task"]["status"]["state"]
                if state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_INPUT_REQUIRED"):
                    arts = td["task"].get("artifacts", [])
                    pr_url = ""
                    branch = ""
                    jira_in_review = False
                    for art in arts:
                        m = art.get("metadata", {})
                        pr_url = pr_url or m.get("prUrl", "")
                        branch = branch or m.get("branch", "")
                        if m.get("jiraInReview"):
                            jira_in_review = True
                    summary = (arts[0].get("parts", [{}])[0].get("text", "Done.") if arts else "Done.")
                    print(f"[web-dev-dispatch] Done: state={state} pr={pr_url} branch={branch}")
                    return ToolResult(output=json.dumps({
                        "status": "completed" if state == "TASK_STATE_COMPLETED" else "error",
                        "summary": summary,
                        "prUrl": pr_url,
                        "branch": branch,
                        "jiraInReview": jira_in_review,
                    }))
                time.sleep(2.0)

            return ToolResult(output=json.dumps({"status": "error", "summary": "Web Dev timed out after 30m"}))

    registry = get_registry()
    # Force-register (unregister HTTP version first if present)
    try:
        registry.unregister("dispatch_web_dev")
    except Exception:
        pass
    registry.register(InProcessDispatchWebDev())
    print("[web-dev] Registered in-process dispatch_web_dev")
