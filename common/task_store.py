"""In-memory task state machine for orchestrated flows."""

from __future__ import annotations

import threading
import time

_TASK_SEQ = 0
_LOCK = threading.Lock()


def _next_id():
    global _TASK_SEQ
    with _LOCK:
        _TASK_SEQ += 1
        return f"task-{_TASK_SEQ:04d}"


class Task:
    __slots__ = (
        "task_id",
        "context_id",
        "state",
        "status_message",
        "agent_id",
        "instance_id",
        "artifacts",
        "history",
        "progress_steps",
        "workspace_path",
        "created_at",
        "updated_at",
        "original_message",
        "pending_workflow",
        # Used by Compass to resume an INPUT_REQUIRED task forwarded to Team Lead
        "downstream_task_id",    # Team Lead task ID that raised INPUT_REQUIRED
        "downstream_service_url",  # Team Lead service URL for sending the resume message
        "summary",
        "jira_ticket_id",
        "design_url",
        "design_type",
        "router_context",
        # Owner / multi-channel fields (Teams integration)
        "owner_user_id",
        "owner_display_name",
        "tenant_id",
        "source_channel",
    )

    def __init__(self, context_id=None):
        self.task_id = _next_id()
        self.context_id = context_id or self.task_id
        self.state = "SUBMITTED"
        self.status_message = ""
        self.agent_id = None
        self.instance_id = None
        self.artifacts = []
        self.history = [{"state": "SUBMITTED", "ts": time.time(), "message": ""}]
        self.progress_steps = []
        self.workspace_path = ""
        self.created_at = time.time()
        self.updated_at = time.time()
        self.original_message = None
        self.pending_workflow = None
        self.downstream_task_id = ""
        self.downstream_service_url = ""
        self.summary = ""
        self.jira_ticket_id = ""
        self.design_url = ""
        self.design_type = ""
        self.router_context = {}
        self.owner_user_id = ""
        self.owner_display_name = ""
        self.tenant_id = ""
        self.source_channel = ""

    def to_dict(self):
        return {
            "id": self.task_id,
            "contextId": self.context_id,
            "status": {
                "state": self.state,
                "message": {"role": "ROLE_AGENT", "parts": [{"text": self.status_message}]},
            },
            "artifacts": self.artifacts,
            "agentId": self.agent_id,
            "instanceId": self.instance_id,
            "history": self.history,
            "progressSteps": self.progress_steps,
            "workspacePath": self.workspace_path,
            "downstreamTaskId": self.downstream_task_id,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "summary": self.summary,
            "jiraTicketId": self.jira_ticket_id,
            "designUrl": self.design_url,
            "designType": self.design_type,
            "routerContext": self.router_context,
            "ownerUserId": self.owner_user_id,
            "ownerDisplayName": self.owner_display_name,
            "tenantId": self.tenant_id,
            "sourceChannel": self.source_channel,
        }


class TaskStore:
    def __init__(self):
        self._lock = threading.Lock()
        self._tasks = {}

    def create(self, context_id=None):
        task = Task(context_id)
        with self._lock:
            self._tasks[task.task_id] = task
        return task

    def get(self, task_id):
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self, owner_user_id=None):
        with self._lock:
            tasks = self._tasks.values()
            if owner_user_id:
                tasks = [task for task in tasks if task.owner_user_id == owner_user_id]
            return sorted(
                tasks,
                key=lambda task: (task.created_at, task.task_id),
                reverse=True,
            )

    def update_state(self, task_id, state, status_message=""):
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.state = state
                task.status_message = status_message
                task.updated_at = time.time()
                task.history.append({"state": state, "ts": task.updated_at, "message": status_message})
            return task

    def assign_agent(self, task_id, agent_id, instance_id):
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.agent_id = agent_id
                task.instance_id = instance_id
                task.updated_at = time.time()
            return task

    def add_progress_step(self, task_id, step, agent_id="", ts=None):
        """Append a major workflow step reported by an agent."""
        with self._lock:
            task = self._tasks.get(task_id)
            if task:
                task.progress_steps.append({
                    "step": step,
                    "agentId": agent_id,
                    "ts": ts or time.time(),
                })
                task.updated_at = time.time()
            return task