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
from agents.code_review.nodes import (
    load_pr_context,
    review_quality,
    review_security,
    review_tests,
    review_requirements,
    generate_report,
)

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
)

# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------

code_review_definition = AgentDefinition(
    agent_id="code-review",
    name="Code Review Agent",
    description="Independent code review: quality, security, tests, requirements compliance",
    mode=AgentMode.TASK,
    execution_mode=ExecutionMode.PER_TASK,
    workflow=code_review_workflow,
    skills=["code-review"],
    tools=["read_file", "search_code"],
    permissions={"scm": "read"},
)


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------

class CodeReviewAgent(BaseAgent):
    """Code Review Agent implementation with graph-first lifecycle."""

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

                config = RunConfig(
                    session_id=task.id,
                    thread_id=task.id,
                    checkpoint_service=self.checkpoint_service,
                    event_store=self.event_store,
                    plugin_manager=self.plugin_manager,
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
