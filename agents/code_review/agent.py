"""Code Review Agent — independent code quality review.

Architecture: **Graph outside, ReAct inside**.

Reviews PR diffs for code quality, security, test coverage, and
requirements compliance using a deterministic graph workflow.
Individual review nodes use LLM single-shot calls for analysis.
"""
from __future__ import annotations

import json
import threading

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode
from framework.workflow import Workflow, START, END
from framework.state import Channel, append_reducer
from agents.code_review.nodes import (
    load_pr_context,
    review_quality,
    review_security,
    review_tests,
    review_requirements,
    generate_report,
)

# ---------------------------------------------------------------------------
# State schema — declares how review issues accumulate
# ---------------------------------------------------------------------------

_code_review_state_schema = {
    "quality_issues": Channel(reducer=append_reducer),
    "security_issues": Channel(reducer=append_reducer),
    "test_issues": Channel(reducer=append_reducer),
    "requirement_gaps": Channel(reducer=append_reducer),
}

# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

code_review_workflow = Workflow(
    name="code_review",
    edges=[
        (START, load_pr_context, review_quality),
        (review_quality, review_security),
        (review_security, review_tests),
        (review_tests, review_requirements),
        (review_requirements, generate_report),
        (generate_report, END),
    ],
    state_schema=_code_review_state_schema,
)

# ---------------------------------------------------------------------------
# Agent definition — derived from config.yaml (single source of truth)
# ---------------------------------------------------------------------------

def _build_code_review_definition() -> AgentDefinition:
    """Build Code Review's AgentDefinition from YAML config + workflow."""
    from framework.config import build_agent_definition_from_config

    try:
        cfg = build_agent_definition_from_config("code-review")
    except Exception:
        cfg = {}
    return AgentDefinition(
        agent_id=cfg.get("agent_id", "code-review"),
        name=cfg.get("name", "Code Review Agent"),
        description=cfg.get("description", "Independent code review: quality, security, tests, requirements compliance"),
        mode=AgentMode.TASK,
        execution_mode=ExecutionMode.PER_TASK,
        workflow=code_review_workflow,
        skills=cfg.get("skills", ["code-review"]),
        tools=cfg.get("tools", ["read_file", "search_code"]),
        permissions=cfg.get("permissions", {"scm": "read"}),
        permission_profile=cfg.get("permission_profile", "read_only"),
        config=cfg.get("config", {}),
    )


code_review_definition = _build_code_review_definition()


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

class CodeReviewAgent(BaseAgent):
    """Code Review Agent implementation with graph-first lifecycle."""

    async def start(self) -> None:
        await super().start()
        _register_code_review_dispatch(self)

    async def handle_message(self, message: dict) -> dict:
        from framework.a2a.protocol import Artifact
        from framework.workflow import RunConfig

        msg = message.get("message", message)
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
            "pr_url": metadata.get("prUrl", ""),
            "repo_url": metadata.get("repoUrl", ""),
            "jira_context": metadata.get("jiraContext", {}),
            "original_requirements": metadata.get("originalRequirements", ""),
            "metadata": metadata,
        }

        def _run() -> None:
            import asyncio

            loop = asyncio.new_event_loop()
            try:
                # Recall relevant past reviews for context
                pr_url_for_recall = metadata.get("prUrl", "") or metadata.get("repoUrl", "code review")
                memory_context = loop.run_until_complete(
                    self.recall_task_context(pr_url_for_recall)
                )
                if memory_context:
                    state["memory_context"] = memory_context

                config = self._build_run_config(
                    task.id,
                    max_steps=20,
                    timeout_seconds=300,
                )
                result = loop.run_until_complete(
                    self._compiled_workflow.invoke(state, config)
                )
                report = {
                    "verdict": result.get("verdict", "rejected"),
                    "comments": result.get("all_comments", []),
                    "summary": result.get("report_summary", ""),
                }
                artifacts = [
                    Artifact(
                        name="code-review-report",
                        artifact_type="application/json",
                        parts=[{"text": json.dumps(report)}],
                        metadata={"agentId": self.definition.agent_id},
                    )
                ]
                task_store.complete_task(task.id, artifacts=artifacts)

                # Consolidate review findings into memory
                loop.run_until_complete(
                    self.consolidate_task_result(
                        summary=result.get("report_summary", ""),
                        tags=["code-review", result.get("verdict", "")],
                    )
                )

                # Send callback if URL provided
                callback_url = metadata.get("orchestratorCallbackUrl", "")
                if callback_url:
                    _send_callback(
                        callback_url, task.id, report, self.definition.agent_id
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
    callback_url: str, task_id: str, report: dict, agent_id: str
) -> None:
    """POST completion callback to orchestrator (best-effort)."""
    from urllib.request import Request, urlopen

    payload = {
        "downstreamTaskId": task_id,
        "state": "TASK_STATE_COMPLETED",
        "statusMessage": report.get("summary", ""),
        "artifacts": [
            {
                "name": "code-review-report",
                "artifactType": "application/json",
                "parts": [{"text": json.dumps(report)}],
                "metadata": {"agentId": agent_id},
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
        print(f"[code-review] Callback failed: {exc}")


def _register_code_review_dispatch(code_review_agent: "CodeReviewAgent") -> None:
    """Register in-process dispatch_code_review tool (overrides HTTP version)."""
    import asyncio
    import time
    from framework.tools.base import BaseTool, ToolResult
    from framework.tools.registry import get_registry

    class InProcessDispatchCodeReview(BaseTool):
        name = "dispatch_code_review"
        description = (
            "Send the dev agent's output (PR URL or diff) to the Code Review Agent "
            "for quality, security, and requirements validation."
        )
        parameters_schema = {
            "type": "object",
            "properties": {
                "pr_url": {"type": "string"},
                "diff_summary": {"type": "string"},
                "requirements": {"type": "string"},
                "jira_context": {"type": "object"},
                "design_context": {"type": "object"},
                "workspace_path": {"type": "string"},
                "context_manifest_path": {"type": "string"},
            },
            "required": [],
        }

        def execute_sync(
            self,
            pr_url: str = "",
            diff_summary: str = "",
            requirements: str = "",
            jira_context=None,
            design_context=None,
            workspace_path: str = "",
            context_manifest_path: str = "",
            **kw,
        ) -> ToolResult:
            task_id_holder: dict = {}

            def _run() -> None:
                loop = asyncio.new_event_loop()
                try:
                    msg = {
                        "message": {
                            "parts": [{"text": diff_summary or pr_url}],
                            "metadata": {
                                "prUrl": pr_url,
                                "originalRequirements": requirements,
                                "jiraContext": jira_context or {},
                                "designContext": design_context or {},
                                "workspacePath": workspace_path,
                                "contextManifestPath": context_manifest_path,
                            },
                        }
                    }
                    result = loop.run_until_complete(code_review_agent.handle_message(msg))
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
                print(f"[code-review-dispatch] Failed to start: {err}")
                return ToolResult(output=json.dumps({"verdict": "error", "message": err}))

            print(f"[code-review-dispatch] Task started: {task_id}")
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                td = code_review_agent.services.task_store.get_task_dict(task_id)
                state = td["task"]["status"]["state"]
                if state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED"):
                    arts = td["task"].get("artifacts", [])
                    payload: dict = {}
                    for art in arts:
                        for part in art.get("parts", []):
                            if "text" in part:
                                try:
                                    payload = json.loads(part["text"])
                                except Exception:
                                    payload = {"verdict": "unknown", "raw": part["text"]}
                                break
                        if payload:
                            break
                    verdict = payload.get("verdict", "unknown")
                    print(f"[code-review-dispatch] Done: state={state} verdict={verdict}")
                    return ToolResult(output=json.dumps(payload or {"verdict": verdict}))
                time.sleep(2.0)

            return ToolResult(output=json.dumps({"verdict": "error", "message": "Code review timed out"}))

    registry = get_registry()
    try:
        registry.unregister("dispatch_code_review")
    except Exception:
        pass
    registry.register(InProcessDispatchCodeReview())
    print("[code-review] Registered in-process dispatch_code_review")
