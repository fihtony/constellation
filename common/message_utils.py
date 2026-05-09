"""Shared helpers for A2A message and artifact payloads."""

from __future__ import annotations

import json
import re
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


def parse_json_object(text):
    """Parse the first JSON object found in model output."""
    text = (text or "").strip()
    if not text:
        return {}
    if text.startswith("```"):
        lines = text.splitlines()
        start = 1
        end = len(lines)
        while end > start and lines[end - 1].strip() in ("```", ""):
            end -= 1
        text = "\n".join(lines[start:end]).strip()
    try:
        payload = json.loads(text)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        payload = json.loads(match.group())
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        return {}