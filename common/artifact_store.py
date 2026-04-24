"""File-backed artifact storage owned by the orchestrator in MVP."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid


class Artifact:
    __slots__ = (
        "artifact_id",
        "task_id",
        "artifact_type",
        "content",
        "metadata",
        "created_at",
        "file_path",
    )

    def __init__(self, task_id, artifact_type, content, metadata=None, artifact_id=None, created_at=None, file_path=None):
        self.artifact_id = artifact_id or str(uuid.uuid4())[:8]
        self.task_id = task_id
        self.artifact_type = artifact_type
        self.content = content
        self.metadata = metadata or {}
        self.created_at = created_at or time.time()
        self.file_path = file_path

    def to_record(self):
        return {
            "artifact_id": self.artifact_id,
            "task_id": self.task_id,
            "artifact_type": self.artifact_type,
            "content": self.content,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "file_path": self.file_path,
        }

    def to_dict(self, include_content=False):
        payload = {
            "artifact_id": self.artifact_id,
            "task_id": self.task_id,
            "artifact_type": self.artifact_type,
            "content_length": len(self.content) if isinstance(self.content, (str, bytes)) else 0,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "file_path": self.file_path,
        }
        if include_content:
            payload["content"] = self.content
        return payload


class ArtifactStore:
    def __init__(self, root=None):
        self.root = root or os.environ.get("ARTIFACT_ROOT") or os.path.join(os.getcwd(), "artifact")
        os.makedirs(self.root, exist_ok=True)
        self._lock = threading.Lock()

    def store(self, task_id, artifact_type, content, metadata=None):
        artifact = Artifact(task_id, artifact_type, content, metadata=metadata)
        task_dir = os.path.join(self.root, task_id)
        os.makedirs(task_dir, exist_ok=True)
        artifact.file_path = os.path.join(task_dir, f"{artifact.artifact_id}.json")
        with self._lock:
            with open(artifact.file_path, "w", encoding="utf-8") as handle:
                json.dump(artifact.to_record(), handle, ensure_ascii=False, indent=2)
        return artifact

    def get_by_task(self, task_id):
        task_dir = os.path.join(self.root, task_id)
        if not os.path.isdir(task_dir):
            return []
        artifacts = []
        for name in sorted(os.listdir(task_dir)):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(task_dir, name), "r", encoding="utf-8") as handle:
                record = json.load(handle)
            artifacts.append(
                Artifact(
                    task_id=record["task_id"],
                    artifact_type=record["artifact_type"],
                    content=record.get("content"),
                    metadata=record.get("metadata"),
                    artifact_id=record.get("artifact_id"),
                    created_at=record.get("created_at"),
                    file_path=record.get("file_path"),
                )
            )
        return artifacts

    def get(self, task_id, artifact_id):
        for artifact in self.get_by_task(task_id):
            if artifact.artifact_id == artifact_id:
                return artifact
        return None