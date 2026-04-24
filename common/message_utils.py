"""Shared helpers for A2A message and artifact payloads."""

from __future__ import annotations

import json
import time
import uuid


def extract_text(message):
    parts = message.get("parts", []) if isinstance(message, dict) else []
    return "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()


def deep_copy_json(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


def artifact_text(artifact):
    parts = artifact.get("parts", []) if isinstance(artifact, dict) else []
    return "".join(part.get("text", "") for part in parts if isinstance(part, dict)).strip()


def build_text_artifact(name, text, *, artifact_type="text/plain", metadata=None, artifact_id=None):
    return {
        "artifactId": artifact_id or str(uuid.uuid4())[:8],
        "name": name,
        "artifactType": artifact_type,
        "createdAt": time.time(),
        "metadata": metadata or {},
        "parts": [{"text": text}],
    }