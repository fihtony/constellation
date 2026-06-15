"""Office Agent — Graph outside, ReAct inside.

Handles generic office work across documents, spreadsheets, presentations,
and folder organization using agentic tool calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from typing import Any

from framework.agent import AgentDefinition, AgentMode, AgentServices, BaseAgent, ExecutionMode
from framework import devlog  # noqa: F401  # default-tz side-effect
from framework.config import build_agent_definition_from_config
from framework.workflow import Workflow, START, END
from framework.a2a.protocol import Artifact
from framework.clarification_reply import (
    build_approve_or_modify_contract,
    resolve_reply,
)
from framework.devlog import _ts

from agents.office.nodes import (
    receive_task,
    analyze_request,
    execute_office_work,
    report_result,
)
from agents.office.office_tools import register_office_tools


logger = logging.getLogger(__name__)


def _append_office_log(task_id: str, message: str, level: str = "INFO ", **kwargs: Any) -> None:
    if not task_id:
        return
    artifact_root = os.environ.get("ARTIFACT_ROOT", "artifacts/")
    log_path = os.path.join(artifact_root, task_id, "office", "agent.log")
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        extra = ""
        if kwargs:
            parts = []
            for key, value in kwargs.items():
                rendered = str(value)
                if len(rendered) > 200:
                    rendered = rendered[:197] + "..."
                parts.append(f"{key}={rendered!r}")
            extra = " " + " ".join(parts)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(f"{_ts()} [{level}] [office] {message}{extra}\n")
    except OSError:
        return


def _normalize_source_paths(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if item]
    return []


def _normalize_capability(value: str) -> str:
    capability = (value or "").strip().lower()
    mapping = {
        "office.document.summarize": "summarize",
        "office.folder.summarize": "summarize",
        "office.data.analyze": "analyze",
        "office.folder.organize": "organize",
    }
    capability = mapping.get(capability, capability)
    return capability if capability in {"summarize", "analyze", "organize"} else ""


def _normalize_custom_plan_action(value: str) -> str:
    text = (value or "").strip().lower()
    if not text:
        return ""
    if text in {"approve", "approved", "yes", "ok", "okay", "go", "y", "lgtm"}:
        return "approve"
    if text in {"modify", "change", "revise", "update"} or text.startswith(
        ("modify:", "change:", "revise:")
    ):
        return "modify"
    return ""


def _unpack_resume_payload(resume_value: Any) -> tuple[str, dict[str, Any]]:
    if isinstance(resume_value, dict):
        reply_text = str(
            resume_value.get("text")
            or resume_value.get("input")
            or resume_value.get("reply")
            or ""
        ).strip()
        resolution = resume_value.get("clarification_resolution") or {}
        return reply_text, dict(resolution) if isinstance(resolution, dict) else {}
    return str(resume_value or "").strip(), {}


# ---------------------------------------------------------------------------
# Workflow definition
# ---------------------------------------------------------------------------

office_workflow = Workflow(
    name="office",
    edges=[
        (START, receive_task, analyze_request),
        (analyze_request, execute_office_work),
        (execute_office_work, report_result, END),
    ],
)


# ---------------------------------------------------------------------------
# Agent definition
# ---------------------------------------------------------------------------

def _build_office_definition() -> AgentDefinition:
    cfg = build_agent_definition_from_config("office")
    return AgentDefinition(
        agent_id=cfg.get("agent_id", "office"),
        name=cfg.get("name", "Office Agent"),
        description=cfg.get("description", "Document processing and data analysis"),
        version="1.0.0",
        mode=AgentMode.TASK,
        execution_mode=ExecutionMode.PER_TASK,
        skills=cfg.get("skills", []),
        tools=cfg.get("tools", []),
        permissions=cfg.get("permissions", {}),
        permission_profile=cfg.get("permission_profile", "office"),
        runtime_backend=cfg.get("runtime_backend", "connect-agent"),
        model=cfg.get("model", "gpt-5-mini"),
        runtime_capabilities=cfg.get("runtime_capabilities", {}),
        workflow=office_workflow,
        config=cfg,
        launch_spec=cfg.get("launch_spec"),
    )


office_definition = _build_office_definition()


# ---------------------------------------------------------------------------
# OfficeAgent
# ---------------------------------------------------------------------------

class _CancelWorkflow(Exception):
    """Raised inside the office workflow when the user requests a cancel.

    The workflow thread catches this exception, marks the task CANCELLED
    via :meth:`TaskStore.cancel_task`, and exits cleanly without
    re-entering the post-workflow success path. Distinct from a normal
    exception: a cancel is a deliberate termination, not a failure.
    """


class OfficeAgent(BaseAgent):
    # task_id → ``threading.Event`` set by ``handle_task_cancel`` when the
    # user requests a cancel. The office workflow thread polls this event
    # between node invocations and raises ``_CancelWorkflow`` if it fires.
    # Keying by task id lets the office run multiple workflows concurrently
    # and only signal the one being cancelled.
    _cancel_events: dict[str, threading.Event] = {}

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        from framework.lifecycle import PerTaskLifecycleManager

        idle_timeout = float(os.environ.get("OFFICE_IDLE_TIMEOUT_SECONDS", "1800"))
        workspace_path = os.environ.get("CONSTELLATION_TASK_WORKSPACE", "")
        self._lifecycle = PerTaskLifecycleManager(
            agent_id=self.definition.agent_id,
            idle_timeout_seconds=idle_timeout,
            workspace_path=workspace_path,
        )

    async def start(self) -> None:
        await super().start()
        register_office_tools()

    async def handle_task_cancel(self, task_id: str, reason: str = "") -> dict:
        """Signal an in-flight office workflow to stop.

        Looks up the ``threading.Event`` registered for this task id by
        :meth:`handle_message` and sets it. The workflow thread will
        observe the signal at the next node boundary, raise
        ``_CancelWorkflow``, and exit cleanly. Then we delegate to the
        base implementation so the task store row is also transitioned to
        ``CANCELLED``.
        """
        event = self._cancel_events.get(task_id)
        if event is not None:
            event.set()
            _append_office_log(
                task_id,
                "office cancel signal delivered to in-flight workflow",
                reason=reason,
            )
        # Always delegate to the base behavior so the task store row is
        # transitioned to CANCELLED even if the workflow already exited
        # (e.g. the cancel raced a normal completion).
        return await super().handle_task_cancel(task_id, reason)

    def _resolve_execution_contract(self, task_id: str, exec_contract: Any):
        from framework.execution_contract import resolve_execution_contract_permission_set
        from framework.permissions import PermissionEngine

        if not exec_contract or not isinstance(exec_contract, dict):
            raise ValueError("Missing executionContract metadata")
        contract, permission_set = resolve_execution_contract_permission_set(
            self.definition.permission_profile,
            exec_contract,
        )
        self._permission_engine = PermissionEngine(permission_set)
        return contract

    def _build_run_payload(
        self,
        *,
        task_id: str,
        user_text: str,
        metadata: dict[str, Any],
        contract: Any,
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        from framework.devlog import AgentLogger
        from framework.office.dimensions import (
            CUSTOM_DIMENSION,
            extract_custom_dimension_hint as _extract_custom_dimension_hint,
            parse_dimension as _parse_dimension,
        )

        source_paths = _normalize_source_paths(
            metadata.get("source_paths") or metadata.get("officeTargetPaths")
        )
        capability = _normalize_capability(
            str(
                metadata.get("capability")
                or metadata.get("officeCapability")
                or metadata.get("requestedCapability")
                or ""
            )
        )
        output_mode = str(
            metadata.get("output_mode") or metadata.get("officeOutputMode") or "workspace"
        ).strip().lower()
        if output_mode not in {"workspace", "inplace"}:
            output_mode = "workspace"

        compass_task_id = str(
            metadata.get("compassTaskId")
            or metadata.get("taskId")
            or task_id
        ).strip() or task_id
        log_task_id = compass_task_id or task_id

        log = AgentLogger(task_id=log_task_id, agent_name=self.definition.agent_id)
        log.node(
            "handle_message",
            compass_task_id=compass_task_id,
            office_task_id=task_id,
            request_preview=user_text[:200],
        )
        _append_office_log(
            log_task_id,
            "[NODE] handle_message",
            compass_task_id=compass_task_id,
            office_task_id=task_id,
            request_preview=user_text[:200],
        )
        log.info(
            "office agent started",
            output_mode=output_mode,
            has_callback=bool(
                metadata.get("callbackUrl") or metadata.get("orchestratorCallbackUrl")
            ),
        )
        _append_office_log(
            log_task_id,
            "office agent started",
            output_mode=output_mode,
            has_callback=bool(
                metadata.get("callbackUrl") or metadata.get("orchestratorCallbackUrl")
            ),
        )
        log.a2a(
            "←",
            "compass",
            event="task received",
            compass_task_id=compass_task_id,
            office_task_id=task_id,
        )
        _append_office_log(
            log_task_id,
            "[A2A] ← compass",
            capability=capability,
            compass_task_id=compass_task_id,
            office_task_id=task_id,
        )

        ephemeral_state = {
            "_task_logger": log,
            "_message_metadata": dict(metadata),
            "_permission_engine": getattr(self, "_permission_engine", None),
        }

        approved_plan = metadata.get("organizeCustomPlan") or {}
        custom_action = str(metadata.get("organizeCustomAction") or "").strip()
        custom_modify_note = str(
            metadata.get("organizeCustomModifyNote") or ""
        ).strip()
        custom_hint = str(
            metadata.get("customDimensionHint")
            or _extract_custom_dimension_hint(user_text)
            or ""
        ).strip()
        organize_dimension = _parse_dimension(dict(metadata), user_text)
        state: dict[str, Any] = {
            "_task_id": task_id,
            "_compass_task_id": compass_task_id,
            "_allowed_tools": contract.allowed_tools,
            "required_skills": list(self.definition.skills or []),
            "user_request": user_text,
            "output_mode": output_mode,
            "source_paths": source_paths,
            "capability": capability or "summarize",
            "test_cycles": 0,
            "organize_dimension": organize_dimension,
        }
        if approved_plan:
            state["organize_custom_plan"] = dict(approved_plan)
        if custom_action:
            state["organize_custom_action"] = custom_action
        if custom_modify_note:
            state["organize_custom_modify_note"] = custom_modify_note
        if custom_hint:
            state["organize_custom_hint"] = custom_hint
        if organize_dimension == CUSTOM_DIMENSION and custom_hint:
            state["organize_custom_hint"] = custom_hint

        if compass_task_id and compass_task_id != task_id:
            try:
                from framework.major_step import resolve_progress_sink

                state["_major_step_progress_sink"] = resolve_progress_sink(
                    compass_task_id
                )
                ephemeral_state["_major_step_progress_sink"] = state[
                    "_major_step_progress_sink"
                ]
            except Exception as exc:  # noqa: BLE001
                logger.debug("office: failed to resolve progress_sink: %s", exc)

        artifact_root = os.environ.get("ARTIFACT_ROOT", "")
        if artifact_root:
            workspace_root = os.path.join(artifact_root, compass_task_id or task_id, "office")
            artifacts_dir = os.path.join(workspace_root, "artifacts")
            os.makedirs(artifacts_dir, exist_ok=True)
            os.environ["OFFICE_WORKSPACE_ROOT"] = artifacts_dir
            log.info(
                "office workspace prepared",
                workspace_root=workspace_root,
                artifacts_dir=artifacts_dir,
            )
            _append_office_log(
                log_task_id,
                "office workspace prepared",
                workspace_root=workspace_root,
                artifacts_dir=artifacts_dir,
            )

        return state, ephemeral_state, log_task_id

    def _execute_office_workflow(
        self,
        *,
        task_id: str,
        state: dict[str, Any],
        ephemeral_state: dict[str, Any],
        task_store,
        callback_url: str,
        log_task_id: str,
        send_callback: bool,
    ) -> dict:
        cancel_event = OfficeAgent._cancel_events.setdefault(
            task_id, threading.Event()
        )
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            config = self._build_run_config(
                task_id,
                max_steps=50,
                timeout_seconds=3600,
                ephemeral_state=ephemeral_state,
            )
            state["_cancel_event"] = cancel_event
            try:
                result = loop.run_until_complete(
                    self._compiled_workflow.invoke(state, config)
                )
            except _CancelWorkflow:
                _append_office_log(
                    log_task_id,
                    "office workflow cancelled by user",
                    reason="cancelled by user",
                )
                try:
                    task_store.cancel_task(task_id, "cancelled by user")
                except Exception:
                    pass
                self._lifecycle.arm_idle_timer(task_id)
                return task_store.get_task_dict(task_id)

            needs_clarification = result.get("needs_clarification")
            if needs_clarification:
                if (
                    isinstance(needs_clarification, dict)
                    and str(needs_clarification.get("missing") or "").strip()
                    == "organizeCustomPlan"
                    and not isinstance(needs_clarification.get("reply_contract"), dict)
                ):
                    needs_clarification = dict(needs_clarification)
                    needs_clarification["reply_contract"] = (
                        build_approve_or_modify_contract()
                    )
                user_message = str(
                    needs_clarification.get("user_message")
                    or "Office requires additional input before continuing."
                ).strip()
                try:
                    task_store.pause_task(
                        task_id,
                        question=user_message,
                        interrupt_metadata={
                            "kind": "office_clarification",
                            "needs_clarification": needs_clarification,
                        },
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "office: pause_task for clarification failed: %s", exc
                    )
                _append_office_log(
                    log_task_id,
                    "office task awaiting clarification",
                    question=user_message,
                    kind=str(needs_clarification.get("missing") or ""),
                )
                if callback_url and send_callback:
                    _send_callback_input_required(
                        callback_url, task_id, user_message
                    )
                self._lifecycle.arm_idle_timer(task_id)
                return task_store.get_task_dict(task_id)

            artifacts = [
                Artifact(
                    name="office-result",
                    artifact_type="text/plain",
                    parts=[{"text": result.get("summary", "Office task completed.")}],
                    metadata={
                        "capability": result.get("capability", "summarize"),
                        "output_mode": result.get("output_mode", "workspace"),
                    },
                )
            ]
            if result.get("success", False) and not _result_summary_indicates_failure(result):
                summary = str(result.get("summary") or "Office task completed.").strip()
                task_store.complete_task(task_id, artifacts=artifacts, message=summary)
                if callback_url and send_callback:
                    _send_callback(callback_url, task_id, result)
            else:
                if result.get("success", False):
                    logger.warning(
                        "office agent result claimed success but summary indicates failure: %s",
                        str(result.get("summary", ""))[:200],
                    )
                    result = dict(result)
                    result["success"] = False
                    result.setdefault("status", "failed")
                task_store.fail_task(
                    task_id,
                    result.get("summary", "Office task failed."),
                )
            self._lifecycle.arm_idle_timer(task_id)
            return task_store.get_task_dict(task_id)
        except Exception as exc:
            logger.exception(f"Office workflow failed: {exc}")
            _append_office_log(
                log_task_id,
                "office workflow failed",
                level="ERROR",
                error=str(exc),
            )
            task_store.fail_task(task_id, str(exc))
            self._lifecycle.arm_idle_timer(task_id)
            return task_store.get_task_dict(task_id)
        finally:
            OfficeAgent._cancel_events.pop(task_id, None)
            loop.close()

    def _reask_for_clarification(
        self,
        *,
        task_store,
        task_id: str,
        question: str,
        interrupt_metadata: dict[str, Any],
    ) -> dict:
        try:
            task_store.resume_task(task_id)
        except Exception:
            pass
        task_store.pause_task(
            task_id,
            question=question,
            interrupt_metadata=interrupt_metadata,
        )
        self._lifecycle.arm_idle_timer(task_id)
        return task_store.get_task_dict(task_id)

    async def handle_message(self, message: dict) -> dict:
        """Handle incoming A2A message.

        Non-blocking: returns task dict immediately, runs workflow in background thread.
        """
        msg = message.get("message", message)
        parts = msg.get("parts", [])
        user_text = parts[0].get("text", "") if parts else ""
        metadata = dict(msg.get("metadata", {}) or {})
        callback_url = str(
            metadata.get("callbackUrl", "") or metadata.get("orchestratorCallbackUrl", "")
        ).strip()
        source_paths = _normalize_source_paths(
            metadata.get("source_paths") or metadata.get("officeTargetPaths")
        )
        capability = _normalize_capability(
            str(
                metadata.get("capability")
                or metadata.get("officeCapability")
                or metadata.get("requestedCapability")
                or ""
            )
        )
        output_mode = str(
            metadata.get("output_mode") or metadata.get("officeOutputMode") or "workspace"
        ).strip().lower()
        if output_mode not in {"workspace", "inplace"}:
            output_mode = "workspace"
        compass_task_id = str(
            metadata.get("compassTaskId", metadata.get("taskId", ""))
        ).strip()

        self._lifecycle.cancel_idle_timer()

        # Create task via task store
        task_store = self.services.task_store
        canonical_task_id = compass_task_id or ""
        task = task_store.create_task(
            agent_id=self.definition.agent_id,
            metadata={
                "compass_task_id": compass_task_id,
                "user_text": user_text,
                "source_paths": source_paths,
                "capability": capability,
                "output_mode": output_mode,
                "callback_url": callback_url,
                "request_metadata": dict(metadata),
            },
            task_id=canonical_task_id or None,
        )

        canonical_task_id = task.id
        try:
            contract = self._resolve_execution_contract(
                canonical_task_id,
                metadata.get("executionContract"),
            )
        except Exception as exc:
            _append_office_log(
                canonical_task_id,
                "invalid execution contract",
                level="ERROR",
                error=str(exc),
            )
            task_store.fail_task(canonical_task_id, str(exc))
            return task_store.get_task_dict(canonical_task_id)

        self._lifecycle.configure_timeout_notification(
            callback_url,
            orchestrator_task_id=compass_task_id or canonical_task_id,
        )
        self._lifecycle.mark_working(canonical_task_id)

        # A re-dispatched task (same task_id as a previously paused run)
        # must start the workflow fresh.  The office workflow is invoked
        # (not resumed), so the saved checkpoint from the previous run
        # would otherwise overwrite the new ``organize_custom_action`` /
        # ``organize_custom_modify_note`` metadata and silently drop
        # the user's modify reply.  Drop the checkpoint here so the
        # planner actually re-runs with the new state.
        if self.checkpoint_service is not None:
            try:
                await self.checkpoint_service.delete(
                    canonical_task_id, canonical_task_id
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "office: failed to clear checkpoint for re-dispatched task %s: %s",
                    canonical_task_id,
                    exc,
                )

        state, ephemeral_state, log_task_id = self._build_run_payload(
            task_id=canonical_task_id,
            user_text=user_text,
            metadata=metadata,
            contract=contract,
        )

        # Run workflow in background thread
        def _run() -> None:
            self._execute_office_workflow(
                task_id=canonical_task_id,
                state=dict(state),
                ephemeral_state=dict(ephemeral_state),
                task_store=task_store,
                callback_url=callback_url,
                log_task_id=log_task_id,
                send_callback=True,
            )

        worker = threading.Thread(target=_run, daemon=True)
        worker.start()

        return task_store.get_task_dict(canonical_task_id)

    async def resume_task(self, task_id: str, resume_value: Any) -> dict:
        from framework.a2a.protocol import TaskState
        from framework.office.dimensions import (
            CUSTOM_DIMENSION,
            extract_custom_dimension_hint,
            parse_dimension,
        )

        task_store = self.services.task_store
        task = task_store.get_task(task_id)
        if task is None:
            raise RuntimeError(f"Task {task_id} not found")

        current_state = getattr(
            getattr(task.status, "state", None), "value",
            str(getattr(task.status, "state", "")),
        )
        if current_state != TaskState.INPUT_REQUIRED.value:
            return task_store.get_task_dict(task_id)

        metadata = dict(task.metadata or {})
        request_metadata = dict(metadata.get("request_metadata") or {})
        callback_url = str(metadata.get("callback_url") or "").strip()
        interrupt = dict(metadata.get("_interrupt") or {})
        needs_clarification = dict(interrupt.get("needs_clarification") or {})
        reply_text, structured_resolution = _unpack_resume_payload(resume_value)

        updated_metadata = dict(request_metadata)
        missing = str(needs_clarification.get("missing") or "").strip()
        if missing == "organizeGroupBy":
            resolution_kind = str(
                structured_resolution.get("contract_kind")
                or structured_resolution.get("kind")
                or ""
            ).strip()
            dimension = ""
            if resolution_kind == "select_option":
                dimension = str(structured_resolution.get("selection") or "").strip()
            if not dimension:
                dimension = parse_dimension({}, reply_text)
            if not dimension:
                return self._reask_for_clarification(
                    task_store=task_store,
                    task_id=task_id,
                    question=str(
                        needs_clarification.get("user_message")
                        or "Office organize needs a grouping dimension."
                    ),
                    interrupt_metadata={
                        "kind": "office_clarification",
                        "needs_clarification": needs_clarification,
                    },
                )
            updated_metadata["organizeGroupBy"] = dimension
            if dimension == CUSTOM_DIMENSION:
                custom_hint = str(
                    extract_custom_dimension_hint(reply_text)
                    or request_metadata.get("customDimensionHint")
                    or ""
                ).strip()
                if custom_hint:
                    updated_metadata["customDimensionHint"] = custom_hint
        elif missing == "organizeCustomHint":
            custom_hint = str(
                extract_custom_dimension_hint(reply_text)
                or reply_text
                or ""
            ).strip()
            if not custom_hint:
                return self._reask_for_clarification(
                    task_store=task_store,
                    task_id=task_id,
                    question=str(
                        needs_clarification.get("user_message")
                        or "Office organize needs a custom grouping hint."
                    ),
                    interrupt_metadata={
                        "kind": "office_clarification",
                        "needs_clarification": needs_clarification,
                    },
                )
            updated_metadata["organizeGroupBy"] = CUSTOM_DIMENSION
            updated_metadata["customDimensionHint"] = custom_hint
        elif missing == "organizeCustomPlan":
            resolution = dict(structured_resolution)
            resolution_kind = str(
                resolution.get("contract_kind")
                or resolution.get("kind")
                or ""
            ).strip()
            if resolution_kind != "approve_or_modify":
                reply_contract = dict(needs_clarification.get("reply_contract") or {})
                if not reply_contract:
                    reply_contract = build_approve_or_modify_contract()
                resolved = resolve_reply(reply_contract, reply_text)
                if not resolved.get("ok"):
                    question = str(
                        resolved.get("reask_message")
                        or needs_clarification.get("user_message")
                        or "Please reply `approve` or `modify: <change>`."
                    )
                    refreshed = dict(needs_clarification)
                    refreshed["reply_contract"] = reply_contract
                    refreshed["user_message"] = question
                    return self._reask_for_clarification(
                        task_store=task_store,
                        task_id=task_id,
                        question=question,
                        interrupt_metadata={
                            "kind": "office_clarification",
                            "needs_clarification": refreshed,
                        },
                    )
                resolution = {
                    "contract_kind": "approve_or_modify",
                    **dict(resolved.get("normalized") or {}),
                }

            action = str(resolution.get("action") or "").strip()
            note = str(resolution.get("note") or "").strip()
            if not action:
                return self._reask_for_clarification(
                    task_store=task_store,
                    task_id=task_id,
                    question=str(
                        needs_clarification.get("user_message")
                        or "Please reply `approve` or `modify: <change>`."
                    ),
                    interrupt_metadata={
                        "kind": "office_clarification",
                        "needs_clarification": needs_clarification,
                    },
                )
            updated_metadata["organizeGroupBy"] = CUSTOM_DIMENSION
            plan = dict(needs_clarification.get("plan") or {})
            if plan:
                updated_metadata["organizeCustomPlan"] = plan
            updated_metadata["organizeCustomAction"] = action
            if action == "modify":
                updated_metadata["organizeCustomModifyNote"] = note or reply_text
            else:
                updated_metadata.pop("organizeCustomModifyNote", None)
            custom_hint = str(
                needs_clarification.get("custom_hint")
                or request_metadata.get("customDimensionHint")
                or ""
            ).strip()
            if custom_hint:
                updated_metadata["customDimensionHint"] = custom_hint
            updated_metadata["clarificationResolution"] = {
                "contract_kind": "approve_or_modify",
                "action": action,
                "note": note or "",
            }
        else:
            updated_metadata["clarificationResponse"] = reply_text

        try:
            contract = self._resolve_execution_contract(
                task_id,
                updated_metadata.get("executionContract"),
            )
        except Exception as exc:
            task_store.fail_task(task_id, str(exc))
            return task_store.get_task_dict(task_id)

        self._lifecycle.cancel_idle_timer()
        self._lifecycle.configure_timeout_notification(
            callback_url,
            orchestrator_task_id=str(
                metadata.get("compass_task_id") or task_id
            ).strip() or task_id,
        )
        self._lifecycle.mark_working(task_id)
        task_store.resume_task(task_id)
        task_store.update_metadata(task_id, {"request_metadata": updated_metadata})

        # The resume path also invokes the workflow fresh (not via
        # :meth:`CompiledWorkflow.resume`), so the saved checkpoint from
        # the previous run would otherwise overwrite the user's
        # ``organize_custom_modify_note`` (etc.) with the stale state.
        # Drop the checkpoint so the new state survives.
        if self.checkpoint_service is not None:
            try:
                await self.checkpoint_service.delete(task_id, task_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "office: failed to clear checkpoint for resumed task %s: %s",
                    task_id,
                    exc,
                )

        state, ephemeral_state, log_task_id = self._build_run_payload(
            task_id=task_id,
            user_text=str(metadata.get("user_text") or ""),
            metadata=updated_metadata,
            contract=contract,
        )
        return await asyncio.to_thread(
            self._execute_office_workflow,
            task_id=task_id,
            state=state,
            ephemeral_state=ephemeral_state,
            task_store=task_store,
            callback_url=callback_url,
            log_task_id=log_task_id,
            send_callback=False,
        )

    async def get_task(self, task_id: str) -> dict:
        """Return real task state from TaskStore."""
        return self.services.task_store.get_task_dict(task_id)


_OFFICE_FAILURE_PATTERNS = (
    "cannot be found or accessed",
    "could not be found",
    "does not exist or is not a valid",
    "error encountered",
    "i cannot inspect or analyze",
    "i cannot access",
    "no such file or directory",
    "required action",
    "source file is not accessible",
    "the path does not exist",
    "the file does not exist",
    "file not found",
    "the requested source file cannot",
)


def _result_summary_indicates_failure(result: dict) -> bool:
    """Return True when the LLM's summary text describes a real failure
    even though the agentic runtime marked the run as ``success: True``.

    The agentic runtime only checks that *some* response was produced.  The
    LLM can therefore happily return "the file could not be found" while the
    runtime still wraps that text in ``success: True``.  We must downgrade
    those runs so the orchestrator (and downstream consumers like Compass)
    sees an honest ``failed`` status.
    """
    summary = str(result.get("summary") or result.get("message") or "").strip()
    if not summary:
        return False
    lowered = summary.lower()
    return any(needle in lowered for needle in _OFFICE_FAILURE_PATTERNS)


def _send_callback(callback_url: str, task_id: str, result: dict) -> None:
    """Send completion callback to orchestrator."""
    import urllib.request
    # Sanitize result - remove non-JSON-serializable objects
    safe_result = {
        "status": result.get("status", "completed"),
        "summary": result.get("summary", ""),
        "capability": result.get("capability", ""),
        "output_mode": result.get("output_mode", "workspace"),
        "warnings_count": result.get("warnings_count", 0),
    }
    payload = json.dumps({
        "task_id": task_id,
        "state": "completed",
        "result": safe_result,
    }).encode("utf-8")
    req = urllib.request.Request(
        callback_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            pass
    except Exception:
        pass


def _send_callback_input_required(callback_url: str, task_id: str, question: str) -> None:
    """Send an ``input-required`` callback to the orchestrator.

    Mirrors :func:`_send_callback` but signals the ``input-required``
    A2A state and surfaces the clarification question. The orchestrator
    (compass) reads this state and re-prompts the user.
    """
    import urllib.request
    payload = json.dumps({
        "task_id": task_id,
        "state": "input-required",
        "question": question,
    }).encode("utf-8")
    req = urllib.request.Request(
        callback_url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# In-process dispatch (overrides HTTP dispatch)
# ---------------------------------------------------------------------------
