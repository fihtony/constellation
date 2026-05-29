from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Submit a live task to Compass and poll it")
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--text", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    return parser.parse_args()


def _read_json(url: str, data: dict | None = None) -> dict:
    raw = None
    if data is not None:
        raw = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=raw,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def _first_text(parts: list[dict]) -> str:
    for part in parts or []:
        if part.get("text"):
            return str(part["text"])
    return ""


def main() -> int:
    args = _parse_args()
    base_url = args.base_url.rstrip("/")
    payload = {
        "message": {
            "messageId": f"live-task-{int(time.time())}",
            "role": "ROLE_USER",
            "parts": [{"text": args.text}],
            "metadata": {},
        },
        "configuration": {"returnImmediately": True},
    }

    start = _read_json(f"{base_url}/message:send", payload)
    task = start.get("task", {})
    task_id = str(task.get("id") or "").strip()
    if not task_id:
        print(json.dumps({"phase": "error", "response": start}, ensure_ascii=False))
        return 1

    print(json.dumps({
        "phase": "submitted",
        "task_id": task_id,
        "state": task.get("status", {}).get("state", ""),
    }, ensure_ascii=False))

    terminal_states = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_INPUT_REQUIRED"}
    last_state = None
    last_message = None
    deadline = time.time() + args.timeout_seconds
    while time.time() < deadline:
        current = _read_json(f"{base_url}/tasks/{task_id}")
        task = current.get("task", {})
        status = task.get("status", {})
        state = status.get("state", "")
        message = _first_text((status.get("message") or {}).get("parts", []))
        if state != last_state or message != last_message:
            print(json.dumps({
                "phase": "poll",
                "task_id": task_id,
                "state": state,
                "message": message[:1000],
            }, ensure_ascii=False))
            last_state = state
            last_message = message
        if state in terminal_states:
            artifacts = []
            for artifact in task.get("artifacts", [])[:5]:
                artifacts.append({
                    "name": artifact.get("name", ""),
                    "metadata": artifact.get("metadata", {}),
                    "text_preview": _first_text(artifact.get("parts", []))[:1000],
                })
            print(json.dumps({
                "phase": "final",
                "task_id": task_id,
                "state": state,
                "message": message[:2000],
                "artifacts": artifacts,
            }, ensure_ascii=False))
            return 0 if state == "TASK_STATE_COMPLETED" else 2
        time.sleep(5)

    print(json.dumps({"phase": "timeout", "task_id": task_id}, ensure_ascii=False))
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
