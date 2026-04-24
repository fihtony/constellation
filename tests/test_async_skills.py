#!/usr/bin/env python3
"""Tests for async skill contract: scm.git.clone and Android agent callback endpoint.

Usage:
    # Against running containers:
    python3 tests/test_async_skills.py --container [-v]

    # Against local agents (specify URLs):
    python3 tests/test_async_skills.py --scm-url http://127.0.0.1:8020 \
                                        --android-url http://127.0.0.1:8030 [-v]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import time
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _post(url, payload, timeout=30):
    data = json.dumps(payload).encode("utf-8")
    req = Request(url, data=data,
                  headers={"Content-Type": "application/json", "Accept": "application/json"},
                  method="POST")
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw.strip() else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"error": raw[:300]}
        return exc.code, body
    except URLError as exc:
        return 0, {"error": str(exc)}


def _get(url, timeout=30):
    req = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw.strip() else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except Exception:
            body = {"error": raw[:300]}
        return exc.code, body
    except URLError as exc:
        return 0, {"error": str(exc)}


def _wait_for_task(base_url, task_id, poll_interval=3, timeout=120):
    """Poll GET /tasks/{task_id} until terminal state or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = _get(f"{base_url}/tasks/{task_id}", timeout=10)
        if status == 200 and isinstance(body, dict):
            task = body.get("task", body)
            state = (task.get("status") or {}).get("state", "") if isinstance(task, dict) else ""
            if state in ("TASK_STATE_COMPLETED", "TASK_STATE_FAILED"):
                return task
        time.sleep(poll_interval)
    return None


def _health_ok(url, timeout=5):
    status, _ = _get(f"{url}/health", timeout=timeout)
    return status == 200


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestReport:
    def __init__(self, verbose=False):
        self._verbose = verbose
        self._passed = []
        self._failed = []

    def ok(self, name, detail=""):
        self._passed.append(name)
        mark = "✓" if sys.stdout.encoding and "utf" in sys.stdout.encoding.lower() else "PASS"
        print(f"  {mark} {name}" + (f"  ({detail})" if detail and self._verbose else ""))

    def fail(self, name, detail=""):
        self._failed.append(name)
        mark = "✗" if sys.stdout.encoding and "utf" in sys.stdout.encoding.lower() else "FAIL"
        print(f"  {mark} {name}" + (f"  → {detail}" if detail else ""))

    def summary(self):
        total = len(self._passed) + len(self._failed)
        print(f"\n  {len(self._passed)}/{total} tests passed")
        return 0 if not self._failed else 1


# ---------------------------------------------------------------------------
# SCM async clone tests
# ---------------------------------------------------------------------------

def test_scm_agent_card_declares_clone_async(bb_url, report):
    """Agent card must expose scm.git.clone with executionMode=async."""
    status, body = _get(f"{bb_url}/.well-known/agent-card.json")
    if status != 200:
        report.fail("agent_card_accessible", f"status={status}")
        return
    skills = (body or {}).get("skills") or []
    clone_skill = next((s for s in skills if s.get("id") == "scm.git.clone"), None)
    if not clone_skill:
        report.fail("clone_skill_declared", "scm.git.clone not found in agent card")
        return
    report.ok("clone_skill_declared")
    if clone_skill.get("executionMode") == "async":
        report.ok("clone_skill_is_async", clone_skill.get("executionMode"))
    else:
        report.fail("clone_skill_is_async",
                    f"executionMode={clone_skill.get('executionMode')!r} (expected 'async')")


def test_scm_sync_skills_declared(bb_url, report):
    """Key sync skills (repo.tree, repo.file, git.push) must declare executionMode=sync."""
    status, body = _get(f"{bb_url}/.well-known/agent-card.json")
    if status != 200:
        report.fail("sync_skills_agent_card", f"status={status}")
        return
    skills = {s["id"]: s for s in (body or {}).get("skills") or [] if "id" in s}
    for skill_id in ("scm.repo.tree", "scm.repo.file", "scm.git.push"):
        if skill_id not in skills:
            report.fail(f"skill_present_{skill_id}", "not found in agent card")
        elif skills[skill_id].get("executionMode") == "sync":
            report.ok(f"skill_sync_{skill_id}")
        else:
            report.fail(f"skill_sync_{skill_id}",
                        f"executionMode={skills[skill_id].get('executionMode')!r}")


def test_clone_endpoint_accepts_and_returns_task_id(bb_url, project, repo, report):
    """POST /scm/git/clone must return 202 with a taskId immediately."""
    with tempfile.TemporaryDirectory(prefix="async-skill-test-") as tmpdir:
        target = os.path.join(tmpdir, "clone-target")
        status, body = _post(
            f"{bb_url}/scm/git/clone",
            {
                "project": project,
                "repo": repo,
                "branch": "develop",
                "targetPath": target,
                # No callbackUrl: testing poll-only mode
            },
            timeout=15,
        )
        if status not in (200, 202):
            report.fail("clone_accepts", f"status={status} body={str(body)[:200]}")
            return
        task_id = (body or {}).get("taskId", "")
        if not task_id:
            report.fail("clone_task_id_present", f"no taskId in response: {body}")
            return
        report.ok("clone_accepts", f"taskId={task_id}")
        exec_mode = (body or {}).get("executionMode", "")
        if exec_mode == "async":
            report.ok("clone_response_declares_async")
        else:
            report.fail("clone_response_declares_async",
                        f"executionMode={exec_mode!r} in response")

        # Poll until complete (or timeout)
        final_task = _wait_for_task(bb_url, task_id, poll_interval=3, timeout=180)
        if final_task is None:
            report.fail("clone_completes_via_poll", "timed out waiting for terminal state")
            return
        final_state = (final_task.get("status") or {}).get("state", "")
        if final_state == "TASK_STATE_COMPLETED":
            clone_path = (final_task.get("extra") or {}).get("clonePath", "")
            report.ok("clone_completes_via_poll", f"state={final_state} clonePath={clone_path}")
            return clone_path
        else:
            msg_parts = (final_task.get("status") or {}).get("message", {})
            error_text = ""
            if isinstance(msg_parts, dict):
                parts = msg_parts.get("parts") or []
                error_text = parts[0].get("text", "") if parts else ""
            report.fail("clone_completes_via_poll", f"state={final_state} msg={error_text[:200]}")
    return None


def test_repo_tree_endpoint(bb_url, clone_path, report):
    """GET /scm/repo/tree must return tree text for a valid clone path."""
    if not clone_path or not os.path.isdir(clone_path):
        report.fail("repo_tree_returns_content", f"clone_path not available: {clone_path!r}")
        return
    status, body = _get(f"{bb_url}/scm/repo/tree?path={clone_path}&depth=3")
    if status != 200:
        report.fail("repo_tree_returns_content", f"status={status}")
        return
    tree = (body or {}).get("tree", "")
    if tree:
        report.ok("repo_tree_returns_content", f"{len(tree)} chars")
    else:
        report.fail("repo_tree_returns_content", "empty tree")


def test_repo_file_endpoint(bb_url, clone_path, report):
    """GET /scm/repo/file must return file content for a known file."""
    if not clone_path or not os.path.isdir(clone_path):
        report.fail("repo_file_returns_content", f"clone_path not available: {clone_path!r}")
        return
    # Try common root files
    candidate_files = ["README.md", "build.gradle", "build.gradle.kts", "settings.gradle.kts",
                       ".gitignore", "pom.xml"]
    found_file = None
    for fname in candidate_files:
        full = os.path.join(clone_path, fname)
        if os.path.isfile(full):
            found_file = fname
            break
    if not found_file:
        report.ok("repo_file_returns_content", "skip — no known root file exists in repo")
        return
    status, body = _get(f"{bb_url}/scm/repo/file?path={clone_path}&file={found_file}")
    if status != 200:
        report.fail("repo_file_returns_content", f"status={status} file={found_file}")
        return
    content = (body or {}).get("content", "")
    if content:
        report.ok("repo_file_returns_content", f"file={found_file} len={len(content)}")
    else:
        report.fail("repo_file_returns_content", f"empty content for {found_file}")


# ---------------------------------------------------------------------------
# Android agent tests
# ---------------------------------------------------------------------------

def test_android_agent_card_declares_async(android_url, report):
    """Android agent card must declare android.task.execute as async."""
    status, body = _get(f"{android_url}/.well-known/agent-card.json")
    if status != 200:
        report.fail("android_agent_card_accessible", f"status={status}")
        return
    skills = (body or {}).get("skills") or []
    task_skill = next((s for s in skills if s.get("id") == "android.task.execute"), None)
    if not task_skill:
        report.fail("android_task_skill_declared", "android.task.execute not found")
        return
    report.ok("android_task_skill_declared")
    if task_skill.get("executionMode") == "async":
        report.ok("android_task_skill_is_async")
    else:
        report.fail("android_task_skill_is_async",
                    f"executionMode={task_skill.get('executionMode')!r}")


def test_android_clone_callback_endpoint(android_url, report):
    """POST /clone-callbacks/{task_id} must accept clone completion payloads and return 200."""
    fake_task_id = "android-test-0000"
    status, body = _post(
        f"{android_url}/clone-callbacks/{fake_task_id}",
        {
            "taskId": "scm-task-0000",
            "agentId": "scm-agent",
            "state": "TASK_STATE_COMPLETED",
            "clonePath": "/tmp/test-clone",
            "error": "",
        },
    )
    if status == 200:
        report.ok("android_clone_callback_accepts")
    else:
        report.fail("android_clone_callback_accepts", f"status={status} body={body}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scm-url", default="")
    parser.add_argument("--android-url", default="")
    parser.add_argument("--container", action="store_true",
                        help="Use container URLs http://127.0.0.1:8020 and :8030")
    parser.add_argument("--owner", default="fihtony")
    parser.add_argument("--repo", default="microservice-test")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--skip-clone", action="store_true",
                        help="Skip the actual git clone test (faster, no network needed)")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    bb_url = args.scm_url
    android_url = args.android_url
    if args.container:
        bb_url = bb_url or "http://127.0.0.1:8020"
        android_url = android_url or "http://127.0.0.1:8030"
    if not bb_url:
        bb_url = "http://127.0.0.1:8020"
    if not android_url:
        android_url = "http://127.0.0.1:8030"

    report = TestReport(verbose=args.verbose)

    print(f"\n=== Async Skill Tests ===")
    print(f"  SCM: {bb_url}")
    print(f"  Android:   {android_url}")

    # ── SCM agent card ──────────────────────────────────────────────────
    print("\n── SCM agent skill declarations ──")
    if not _health_ok(bb_url):
        print(f"  SKIP: SCM agent not reachable at {bb_url}")
    else:
        test_scm_agent_card_declares_clone_async(bb_url, report)
        test_scm_sync_skills_declared(bb_url, report)

        clone_path = None
        if not args.skip_clone:
            print("\n── SCM git.clone async flow ──")
            clone_path = test_clone_endpoint_accepts_and_returns_task_id(
                bb_url, args.project, args.repo, report
            )
            if clone_path:
                print("\n── SCM repo.tree + repo.file (sync) ──")
                test_repo_tree_endpoint(bb_url, clone_path, report)
                test_repo_file_endpoint(bb_url, clone_path, report)
        else:
            print("\n  (--skip-clone: skipping git clone test)")

    # ── Android agent ─────────────────────────────────────────────────────────
    print("\n── Android agent skill declarations ──")
    if not _health_ok(android_url):
        print(f"  SKIP: Android agent not reachable at {android_url}")
    else:
        test_android_agent_card_declares_async(android_url, report)
        test_android_clone_callback_endpoint(android_url, report)

    return report.summary()


if __name__ == "__main__":
    raise SystemExit(main())
