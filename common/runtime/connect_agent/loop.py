"""Core agent loop for the Connect Agent runtime."""

from __future__ import annotations

import hashlib
import json
import random
import time
from typing import Callable
from urllib.error import HTTPError, URLError

from common.runtime.connect_agent.checkpoint import CheckpointManager
from common.runtime.connect_agent.compressor import auto_compact, estimate_tokens, micro_compact
from common.runtime.connect_agent.planner import TodoManager
from common.runtime.connect_agent.policy import PolicyProfile, is_tool_allowed
from common.runtime.connect_agent.sandbox import audit_log, redact_secrets, truncate_output
from common.runtime.connect_agent.transport import call_chat_completion


class LoopDetector:
    def __init__(self, repeat_threshold: int = 3, fail_threshold: int = 5) -> None:
        self._history: list[str] = []
        self._consecutive_failures = 0
        self._repeat_threshold = repeat_threshold
        self._fail_threshold = fail_threshold

    def record(self, tool_name: str, args: dict, *, failed: bool = False) -> str | None:
        sig = hashlib.md5(f"{tool_name}:{json.dumps(args, sort_keys=True)}".encode()).hexdigest()[:12]
        self._history.append(sig)
        self._consecutive_failures = self._consecutive_failures + 1 if failed else 0

        if len(self._history) >= self._repeat_threshold:
            recent = self._history[-self._repeat_threshold:]
            if len(set(recent)) == 1:
                self._history.clear()
                return (
                    "<loop_detected>You have called the same tool with the same arguments "
                    f"{self._repeat_threshold} times in a row. Try a different approach.</loop_detected>"
                )
        if self._consecutive_failures >= self._fail_threshold:
            self._consecutive_failures = 0
            return (
                "<failure_streak>The last "
                f"{self._fail_threshold} tool calls failed. Consider trying a different strategy.</failure_streak>"
            )
        return None


class RetryPolicy:
    def __init__(
        self,
        max_retries: int = 3,
        base_delay: float = 2.0,
        max_delay: float = 30.0,
        backoff_factor: float = 2.0,
        retryable_codes: set[int] | None = None,
    ) -> None:
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.backoff_factor = backoff_factor
        self.retryable_codes = retryable_codes or {429, 500, 502, 503, 504}

    def delay(self, attempt: int) -> float:
        base = min(self.base_delay * (self.backoff_factor ** attempt), self.max_delay)
        return base + random.uniform(0, base * 0.1)


_DEFAULT_RETRY = RetryPolicy()


def _call_llm(
    messages: list[dict],
    tool_defs: list[dict],
    *,
    model: str,
    timeout: int = 120,
    retry: RetryPolicy | None = None,
) -> dict:
    retry = retry or _DEFAULT_RETRY
    last_exc: Exception | None = None
    for attempt in range(retry.max_retries + 1):
        try:
            return call_chat_completion(
                messages,
                model=model,
                timeout=timeout,
                max_tokens=4096,
                tools=tool_defs,
            )
        except HTTPError as exc:
            last_exc = exc
            if exc.code not in retry.retryable_codes or attempt >= retry.max_retries:
                raise
            wait = retry.delay(attempt)
            retry_after = exc.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                wait = max(wait, int(retry_after))
            audit_log("LLM_RETRY", attempt=attempt + 1, status=exc.code, wait_s=round(wait, 1))
            time.sleep(wait)
        except URLError as exc:
            last_exc = exc
            if attempt >= retry.max_retries:
                raise
            wait = retry.delay(attempt)
            audit_log("LLM_RETRY", attempt=attempt + 1, error=str(exc.reason)[:100], wait_s=round(wait, 1))
            time.sleep(wait)
    raise last_exc or RuntimeError("LLM call failed after retries.")


def _dispatch_tool_call(name: str, args: dict, *, profile: PolicyProfile) -> str:
    if not is_tool_allowed(profile, name):
        audit_log("TOOL_DENIED", tool=name)
        return f"Error: tool '{name}' is not allowed under the current policy profile."
    try:
        from common.tools.native_adapter import dispatch_function_call

        return truncate_output(redact_secrets(dispatch_function_call(name, args)))
    except KeyError:
        return f"Error: unknown tool '{name}'."
    except Exception as exc:  # noqa: BLE001
        return f"Error executing '{name}': {exc}"


def agent_loop(
    task: str,
    *,
    task_id: str,
    system_prompt: str,
    model: str,
    profile: PolicyProfile,
    todo_manager: TodoManager,
    tool_names: list[str] | None = None,
    max_turns: int = 50,
    timeout: int = 1800,
    token_threshold: int = 100_000,
    on_progress: Callable[[str], None] | None = None,
    llm_run_fn: Callable | None = None,
    transcript_dir: str | None = None,
    checkpoint_manager: CheckpointManager | None = None,
    checkpoint_state: dict | None = None,
) -> dict:
    try:
        from common.tools.native_adapter import get_function_definitions

        tool_defs = get_function_definitions(tool_names or None)
    except ImportError:
        tool_defs = []

    if not tool_names and "*" not in profile.allow_tools:
        allowed_names = set(profile.allow_tools) - set(profile.deny_tools)
        tool_defs = [tool for tool in tool_defs if tool["function"]["name"] in allowed_names]

    if checkpoint_state:
        messages = checkpoint_state.get("messages") or []
        all_tool_calls = checkpoint_state.get("tool_calls") or []
        turns_used = int(checkpoint_state.get("turns_used") or 0)
    else:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": task},
        ]
        all_tool_calls = []
        turns_used = 0

    final_content = ""
    start_time = time.time()
    loop_detector = LoopDetector()
    checkpoint_id = None
    stop_with_pending_todos = 0

    if checkpoint_manager is not None:
        checkpoint_id = checkpoint_manager.save(
            task_id=task_id,
            provider="connect-agent",
            messages=messages,
            tool_calls=all_tool_calls,
            turns_used=turns_used,
            policy_profile=profile.name,
            tool_names=tool_names or [],
        )

    for turn in range(turns_used, max_turns):
        elapsed = time.time() - start_time
        if elapsed > timeout:
            audit_log("LOOP_TIMEOUT", turns=turns_used, elapsed_s=round(elapsed))
            return {
                "success": False,
                "summary": f"Timed out after {round(elapsed)}s ({turns_used} turns).",
                "tool_calls": all_tool_calls,
                "turns_used": turns_used,
                "messages": messages,
                "checkpoint_id": checkpoint_id,
                "continuation": checkpoint_id,
            }

        turns_used = turn + 1
        micro_compact(messages)
        if estimate_tokens(messages) > token_threshold:
            audit_log("AUTO_COMPACT", tokens_before=estimate_tokens(messages))
            messages[:] = auto_compact(messages, llm_fn=llm_run_fn, transcript_dir=transcript_dir)
            audit_log("AUTO_COMPACT_DONE", tokens_after=estimate_tokens(messages))

        reminder = todo_manager.tick()
        if reminder:
            messages.append({"role": "user", "content": reminder})

        try:
            response = _call_llm(
                messages,
                tool_defs,
                model=model,
                timeout=max(1, min(120, timeout - int(elapsed))),
            )
        except (HTTPError, URLError) as exc:
            audit_log("LLM_FATAL", error=str(exc)[:200], turn=turns_used)
            return {
                "success": False,
                "summary": f"LLM request failed: {exc}",
                "tool_calls": all_tool_calls,
                "turns_used": turns_used,
                "messages": messages,
                "checkpoint_id": checkpoint_id,
                "continuation": checkpoint_id,
            }

        choices = response.get("choices") or []
        if not choices:
            audit_log("LLM_EMPTY", turn=turns_used)
            return {
                "success": False,
                "summary": "The model returned no choices.",
                "tool_calls": all_tool_calls,
                "turns_used": turns_used,
                "messages": messages,
                "checkpoint_id": checkpoint_id,
                "continuation": checkpoint_id,
            }

        choice = choices[0]
        message = choice.get("message") or {}
        content = message.get("content") or ""
        tool_calls_raw = message.get("tool_calls") or []
        finish_reason = choice.get("finish_reason", "stop")

        usage = response.get("usage", {})
        if usage:
            audit_log(
                "LLM_USAGE",
                turn=turns_used,
                prompt_tokens=usage.get("prompt_tokens"),
                completion_tokens=usage.get("completion_tokens"),
            )

        if finish_reason == "stop" or not tool_calls_raw:
            final_content = str(content).strip()
            pending_todos = [item.content for item in todo_manager.items if item.status != "completed"]
            if pending_todos:
                stop_with_pending_todos += 1
                audit_log(
                    "LOOP_STOP_WITH_PENDING_TODOS",
                    turns=turns_used,
                    pending=len(pending_todos),
                    attempt=stop_with_pending_todos,
                )
                if final_content:
                    messages.append({"role": "assistant", "content": final_content})
                if stop_with_pending_todos < 3 and turns_used < max_turns:
                    messages.append({
                        "role": "user",
                        "content": (
                            "<unfinished_work>You tried to stop, but your todo list still has unfinished items:\n- "
                            + "\n- ".join(pending_todos)
                            + "\nContinue autonomously. Do not ask for confirmation unless external input is genuinely missing.</unfinished_work>"
                        ),
                    })
                    continue
            if on_progress:
                on_progress(final_content[:300])
            audit_log("LOOP_COMPLETE", turns=turns_used, reason=finish_reason)
            checkpoint_id = checkpoint_manager.save(
                task_id=task_id,
                provider="connect-agent",
                messages=messages + [{"role": "assistant", "content": final_content}],
                tool_calls=all_tool_calls,
                turns_used=turns_used,
                policy_profile=profile.name,
                tool_names=tool_names or [],
                summary=final_content,
            ) if checkpoint_manager is not None else checkpoint_id
            return {
                "success": True,
                "summary": final_content or "(no final response)",
                "tool_calls": all_tool_calls,
                "turns_used": turns_used,
                "messages": messages,
                "checkpoint_id": checkpoint_id,
                "continuation": checkpoint_id,
            }

        assistant_message = {"role": "assistant", "content": content, "tool_calls": tool_calls_raw}
        messages.append(assistant_message)

        for tool_call in tool_calls_raw:
            fn = tool_call.get("function") or {}
            fn_name = fn.get("name", "")
            try:
                fn_args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                fn_args = {}

            tool_result = _dispatch_tool_call(fn_name, fn_args, profile=profile)
            failed = tool_result.startswith("Error")
            all_tool_calls.append({"name": fn_name, "args": fn_args, "result": tool_result[:500]})
            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.get("id", ""),
                "content": tool_result,
            })

            nudge = loop_detector.record(fn_name, fn_args, failed=failed)
            if nudge:
                messages.append({"role": "user", "content": nudge})

        checkpoint_id = checkpoint_manager.save(
            task_id=task_id,
            provider="connect-agent",
            messages=messages,
            tool_calls=all_tool_calls,
            turns_used=turns_used,
            policy_profile=profile.name,
            tool_names=tool_names or [],
        ) if checkpoint_manager is not None else checkpoint_id

        if on_progress:
            on_progress(f"[turn {turns_used}] executed {len(tool_calls_raw)} tool call(s)")

    audit_log("LOOP_MAX_TURNS", turns=turns_used)
    return {
        "success": False,
        "summary": f"Reached the max turn limit ({max_turns}) without a final answer.",
        "tool_calls": all_tool_calls,
        "turns_used": turns_used,
        "messages": messages,
        "checkpoint_id": checkpoint_id,
        "continuation": checkpoint_id,
    }