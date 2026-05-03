"""Checkpoint persistence helpers for connect-agent."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from typing import Any


def build_task_id(task: str, sandbox_root: str) -> str:
    seed = f"{sandbox_root}|{task}|{time.time()}"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]
    return f"connect-agent-{digest}"


class CheckpointManager:
    def __init__(self, root_dir: str, *, enabled: bool = True) -> None:
        self._root_dir = root_dir
        self._enabled = enabled
        if self._enabled:
            os.makedirs(self._root_dir, exist_ok=True)

    def save(
        self,
        *,
        task_id: str,
        provider: str,
        messages: list[dict],
        tool_calls: list[dict],
        turns_used: int,
        policy_profile: str,
        tool_names: list[str],
        summary: str = "",
        verified_state: str | None = None,
    ) -> str | None:
        if not self._enabled:
            return None

        payload = {
            "task_id": task_id,
            "provider": provider,
            "policy_profile": policy_profile,
            "policy_hash": self._hash_text(policy_profile),
            "toolset_hash": self._hash_json(tool_names),
            "summary": summary,
            "verified_state": verified_state,
            "turns_used": turns_used,
            "messages": messages,
            "tool_calls": tool_calls,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        path = self._checkpoint_path(task_id)
        fd, tmp_path = tempfile.mkstemp(dir=self._root_dir, prefix=f"{task_id}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return task_id

    def load(
        self,
        continuation: str,
        *,
        expected_provider: str,
        expected_policy_profile: str,
        expected_tool_names: list[str],
    ) -> dict[str, Any] | None:
        if not self._enabled or not continuation:
            return None

        path = continuation if os.path.isfile(continuation) else self._checkpoint_path(continuation)
        if not os.path.isfile(path):
            raise ValueError(f"Checkpoint not found: {continuation}")

        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)

        if payload.get("provider") != expected_provider:
            raise ValueError("Checkpoint provider mismatch.")
        if payload.get("policy_hash") != self._hash_text(expected_policy_profile):
            raise ValueError("Checkpoint policy mismatch.")
        if payload.get("toolset_hash") != self._hash_json(expected_tool_names):
            raise ValueError("Checkpoint toolset mismatch.")
        return payload

    def _checkpoint_path(self, task_id: str) -> str:
        return os.path.join(self._root_dir, f"{task_id}.json")

    @staticmethod
    def _hash_text(text: str) -> str:
        return hashlib.sha1((text or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _hash_json(value: Any) -> str:
        return hashlib.sha1(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()