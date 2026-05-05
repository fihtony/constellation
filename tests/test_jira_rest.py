#!/usr/bin/env python3
"""Jira Agent REST API integration tests.

Tests the Jira agent's HTTP endpoints (GET/POST/PUT/DELETE /jira/*) against a
real Jira Cloud site.  All configuration is read EXCLUSIVELY from tests/.env.

Test Cases
----------
TC-01  myself         GET /jira/myself  → authenticated user
TC-02  health         GET /health       → status=ok, backend reported
TC-03  agent-card     GET /.well-known/agent-card.json → valid card
TC-04  ticket-fetch   GET /jira/tickets/{key}
TC-05  transitions    GET /jira/transitions/{key}
TC-06  search         GET /jira/search?jql=key={key}&maxResults=1
TC-07  comment-add    POST /jira/comments/{key}
TC-08  comment-update PUT  /jira/comments/{key}/{id}
TC-09  comment-delete DELETE /jira/comments/{key}/{id}
TC-10  field-update   PUT  /jira/tickets/{key} with labels (restore)
TC-11  transition     POST /jira/transitions/{key} → In Progress + restore

Required keys in tests/.env:
  TEST_JIRA_TICKET_URL   Full Jira browse URL
  TEST_JIRA_TOKEN        Jira API token
  TEST_JIRA_EMAIL        Atlassian account email

Usage:
    python3 tests/test_jira_rest.py              # dry-run
    python3 tests/test_jira_rest.py --integration [-v]
    python3 tests/test_jira_rest.py --integration --agent-url http://localhost:8010 -v
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode

_HERE = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from agent_test_support import (
    PROJECT_ROOT,
    Reporter,
    agent_url_from_args,
    build_test_subprocess_env,
    find_corp_ca_bundle,
    http_request,
    load_env_file,
    summary_exit_code,
)
from agent_test_targets import jira_ticket_key, jira_ticket_url, assert_jira_write_allowed
from common.task_permissions import load_permission_grant

DEFAULT_LOCAL_PORT = 18010
CONTAINER_AGENT_URL = "http://127.0.0.1:8010"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

_ENV = load_env_file("tests/.env")
_DEVELOPMENT_PERMISSIONS = load_permission_grant("development").to_dict()


def _build_cleanup_permissions() -> dict:
    grant = copy.deepcopy(_DEVELOPMENT_PERMISSIONS)
    filtered_denied = []
    for entry in grant.get("denied", []):
        if entry.get("agent") != "jira":
            filtered_denied.append(entry)
            continue
        operations = [
            op for op in entry.get("operations", [])
            if op.get("action") != "comment.delete"
        ]
        if operations:
            new_entry = dict(entry)
            new_entry["operations"] = operations
            filtered_denied.append(new_entry)
    grant["denied"] = filtered_denied
    grant.setdefault("allowed", []).append(
        {
            "agent": "jira",
            "operations": [{"action": "comment.delete", "scope": "*"}],
        }
    )
    return grant


_CLEANUP_PERMISSIONS = _build_cleanup_permissions()


def _permission_headers(grant: dict) -> dict:
    return {"X-Task-Permissions": json.dumps(grant, ensure_ascii=False)}


_READ_HEADERS = _permission_headers(_DEVELOPMENT_PERMISSIONS)


def _env(key: str, fallback: str = "") -> str:
    return _ENV.get(key, fallback)


def _as_dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def _jira_base_url() -> str:
    """Extract Jira base URL from TEST_JIRA_TICKET_URL or JIRA_BASE_URL."""
    ticket_url = _env("TEST_JIRA_TICKET_URL", "")
    if "/browse/" in ticket_url:
        return ticket_url.split("/browse/")[0].rstrip("/")
    return _env("JIRA_BASE_URL", "")


# ---------------------------------------------------------------------------
# Local agent lifecycle
# ---------------------------------------------------------------------------

def _free_port(start: int = DEFAULT_LOCAL_PORT) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_local_agent(port: int) -> subprocess.Popen | None:
    venv_python = os.path.join(PROJECT_ROOT, "venv", "bin", "python")
    python = venv_python if os.path.isfile(venv_python) else sys.executable
    agent_url = f"http://127.0.0.1:{port}"
    openai_base_url = _env("OPENAI_BASE_URL", "http://localhost:1288/v1")
    ca_bundle = find_corp_ca_bundle(_ENV)
    env_overrides: dict = {
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "AGENT_ID": "jira-agent",
        "ADVERTISED_BASE_URL": agent_url,
        "REGISTRY_URL": "http://127.0.0.1:9000",
        "INSTANCE_REPORTER_ENABLED": "0",
        "JIRA_BACKEND": "rest",
        "JIRA_BASE_URL": _jira_base_url(),
        "JIRA_TOKEN": _env("TEST_JIRA_TOKEN"),
        "JIRA_EMAIL": _env("TEST_JIRA_EMAIL"),
        "ALLOW_MOCK_FALLBACK": "1",
        "OPENAI_BASE_URL": openai_base_url,
        "OPENAI_MODEL": _env("OPENAI_MODEL", "gpt-5-mini"),
        "PYTHONPATH": PROJECT_ROOT,
    }
    if ca_bundle:
        env_overrides["CORP_CA_BUNDLE"] = ca_bundle
        env_overrides["SSL_CERT_FILE"] = ca_bundle
    env = build_test_subprocess_env(env_overrides, trusted=True)
    return subprocess.Popen(
        [python, "jira/app.py"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def wait_for_agent(url: str, timeout: int = 20) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _, _ = http_request(f"{url}/health")
        if status == 200:
            return True
        time.sleep(0.5)
    return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--integration", action="store_true",
                        help="Run live integration tests (requires tests/.env credentials)")
    parser.add_argument("--agent-url", default="")
    parser.add_argument("--container", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    reporter = Reporter(verbose=args.verbose)
    ticket_key = jira_ticket_key()
    ticket_url = jira_ticket_url()

    print("\n" + "=" * 60)
    print("  Jira Agent REST Integration Tests")
    print("=" * 60)
    print(f"  Ticket : {ticket_url}")
    print(f"  Key    : {ticket_key}")

    # Static / dry-run checks
    reporter.section("Static / dry-run checks")
    reporter.ok(f"Ticket key parseable: {ticket_key}") if ticket_key else reporter.fail("Ticket key missing from tests/.env or agent_test_targets.json")

    if not args.integration:
        print("\n\033[93mIntegration tests skipped — pass --integration to run live checks.\033[0m")
        print(f"\nPassed: {reporter.passed}  Failed: {reporter.failed}  Skipped: {reporter.skipped}")
        return 0 if reporter.failed == 0 else 1

    token = _env("TEST_JIRA_TOKEN")
    email = _env("TEST_JIRA_EMAIL")
    if not token:
        reporter.fail("TEST_JIRA_TOKEN not set in tests/.env")
        return summary_exit_code(reporter)

    # Start / connect to agent
    requested_url = agent_url_from_args(
        args,
        local_default=f"http://127.0.0.1:{_free_port()}",
        container_default=CONTAINER_AGENT_URL,
    )
    agent_url = requested_url.rstrip("/")
    proc = None

    if not args.agent_url and not args.container:
        port = int(agent_url.rsplit(":", 1)[-1])
        reporter.section("Starting local Jira agent subprocess")
        proc = start_local_agent(port)
        if not proc:
            reporter.fail("Cannot start local agent")
            return summary_exit_code(reporter)
        reporter.info(f"Agent PID {proc.pid} — waiting for /health on {agent_url} …")
        if wait_for_agent(agent_url):
            reporter.ok("Local Jira agent is healthy")
        else:
            proc.terminate()
            reporter.fail("Local agent did not become healthy in time")
            return summary_exit_code(reporter)

    reporter.section(f"Jira Agent REST Tests — {agent_url}")

    # ---------------------------------------------------------------------------
    # Cleanup tracker — collects artifacts to restore in finally block
    # ---------------------------------------------------------------------------
    _cleanup_comment_ids: list[str] = []       # comment IDs created during the test
    _original_state: dict = {}                 # {"labels": [...], "summary": "...", "description": "...", "status": "..."}

    def _teardown_cleanup() -> None:
        """Best-effort restore of all test side-effects.

        Must run even on test failure / exception so the Jira ticket is not
        left in a dirty state.
        """
        reporter.section("Teardown — restoring Jira ticket state")
        # 1. Delete test comments
        for cid in _cleanup_comment_ids:
            try:
                s, b, _ = http_request(
                    f"{agent_url}/jira/comments/{ticket_key}/{cid}",
                    method="DELETE",
                    headers=_permission_headers(_CLEANUP_PERMISSIONS),
                )
                if s == 200 and b.get("result") == "deleted":
                    reporter.info(f"Teardown: deleted comment {cid}")
                else:
                    reporter.info(f"Teardown: comment {cid} delete returned status={s}")
            except Exception as exc:
                reporter.info(f"Teardown: comment {cid} cleanup failed: {exc}")

        # 2. Restore mutable issue fields if they were modified during a test
        restore_fields = {}
        for field_name in ("labels", "summary", "description"):
            if field_name in _original_state:
                restore_fields[field_name] = _original_state[field_name]

        if restore_fields:
            try:
                s, b, _ = http_request(
                    f"{agent_url}/jira/tickets/{ticket_key}",
                    method="PUT",
                    payload={
                        "fields": restore_fields,
                        "permissions": _DEVELOPMENT_PERMISSIONS,
                    },
                )
                if s == 200:
                    reporter.info(f"Teardown: issue fields restored: {sorted(restore_fields.keys())}")
                else:
                    reporter.info(f"Teardown: field restore returned status={s}")
            except Exception as exc:
                reporter.info(f"Teardown: field restore failed: {exc}")

        # 3. Restore status if it was changed
        if _original_state.get("status"):
            try:
                s, b, _ = http_request(
                    f"{agent_url}/jira/transitions/{ticket_key}",
                    method="POST",
                    payload={
                        "transition": _original_state["status"],
                        "permissions": _DEVELOPMENT_PERMISSIONS,
                    },
                )
                if s == 200:
                    reporter.info(f"Teardown: status restored to '{_original_state['status']}'")
                else:
                    reporter.info(f"Teardown: status restore returned status={s}")
            except Exception as exc:
                reporter.info(f"Teardown: status restore failed: {exc}")

    try:
        # TC-01 — myself ----------------------------------------------------
        reporter.step("TC-01  GET /jira/myself")
        status, body, _ = http_request(f"{agent_url}/jira/myself", headers=_READ_HEADERS)
        reporter.show("myself", body)
        user = _as_dict(body.get("user"))
        if status == 200 and body.get("result") == "ok" and user.get("accountId"):
            reporter.info(f"Authenticated as: {user.get('emailAddress') or user.get('displayName')}")
            reporter.ok("GET /jira/myself returned authenticated user")
        else:
            reporter.fail("GET /jira/myself failed", f"status={status} body={body}")

        # TC-02 — health ----------------------------------------------------
        reporter.step("TC-02  GET /health")
        status, body, _ = http_request(f"{agent_url}/health")
        reporter.show("health", body)
        if status == 200 and body.get("status") == "ok":
            backend = body.get("backend", "?")
            reporter.info(f"backend={backend}")
            reporter.ok(f"Health check passed (backend={backend})")
        else:
            reporter.fail("Health check failed", f"status={status} body={body}")

        # TC-03 — agent card ------------------------------------------------
        reporter.step("TC-03  GET /.well-known/agent-card.json")
        status, body, _ = http_request(f"{agent_url}/.well-known/agent-card.json")
        reporter.show("agent-card", body)
        if status == 200 and "jira" in (body.get("name") or "").lower():
            skills = [str(skill.get("id")) for skill in body.get("skills", []) if isinstance(skill, dict) and skill.get("id")]
            reporter.info(f"Skills: {', '.join(skills)}")
            reporter.ok("Agent card valid")
        else:
            reporter.fail("Agent card invalid", f"status={status} body={body}")

        # TC-04 — ticket fetch ----------------------------------------------
        reporter.step(f"TC-04  GET /jira/tickets/{ticket_key}")
        status, body, _ = http_request(f"{agent_url}/jira/tickets/{ticket_key}", headers=_READ_HEADERS)
        reporter.show("ticket-fetch", body)
        issue = _as_dict(body.get("issue"))
        fields = _as_dict(issue.get("fields"))
        if status == 200 and body.get("status") == "fetched":
            summary_text = fields.get("summary", "")
            reporter.info(f"Summary: {summary_text[:80]}")
            reporter.ok(f"GET /jira/tickets/{ticket_key} — fetched")
        else:
            reporter.fail(f"GET /jira/tickets/{ticket_key} failed", f"status={status} result={body.get('status')}")

        # TC-05 — transitions -----------------------------------------------
        reporter.step(f"TC-05  GET /jira/transitions/{ticket_key}")
        status, body, _ = http_request(f"{agent_url}/jira/transitions/{ticket_key}", headers=_READ_HEADERS)
        reporter.show("transitions", body)
        transitions = body.get("transitions", [])
        if status == 200 and body.get("result") == "ok" and transitions:
            names = [t.get("name") for t in transitions if isinstance(t, dict)]
            reporter.info(f"Available: {names[:5]}")
            reporter.ok(f"GET /jira/transitions/{ticket_key} — {len(transitions)} transitions")
        else:
            reporter.fail(f"GET /jira/transitions/{ticket_key} failed", f"status={status} result={body.get('result')}")
            transitions = []

        # TC-06 — search ----------------------------------------------------
        reporter.step(f"TC-06  GET /jira/search?jql=key={ticket_key}")
        status, body, _ = http_request(
            f"{agent_url}/jira/search?{urlencode({'jql': f'key = {ticket_key}', 'maxResults': '1'})}",
            headers=_READ_HEADERS,
        )
        reporter.show("search", body)
        search_result = body.get("search", {})
        issues_found = search_result.get("issues", []) if isinstance(search_result, dict) else []
        if status == 200 and body.get("result") == "ok":
            reporter.ok(f"GET /jira/search returned {len(issues_found)} issue(s)")
        else:
            reporter.fail("GET /jira/search failed", f"status={status} result={body.get('result')}")

        # TC-07 — comment add -----------------------------------------------
        assert_jira_write_allowed(ticket_key)
        reporter.step(f"TC-07  POST /jira/comments/{ticket_key}")
        comment_text = "[Agent Test] Constellation Jira REST test comment — add"
        status, body, _ = http_request(
            f"{agent_url}/jira/comments/{ticket_key}",
            method="POST",
            payload={"text": comment_text, "permissions": _DEVELOPMENT_PERMISSIONS},
        )
        reporter.show("comment-add", body)
        comment_id = body.get("commentId")
        if status == 201 and body.get("result") == "added" and comment_id:
            _cleanup_comment_ids.append(comment_id)
            reporter.ok(f"Comment {comment_id} added to {ticket_key}")
        else:
            reporter.fail(f"POST /jira/comments/{ticket_key} failed", f"status={status} body={body}")
            comment_id = None

        # TC-08 — comment update --------------------------------------------
        if comment_id:
            reporter.step(f"TC-08  PUT /jira/comments/{ticket_key}/{comment_id}")
            status, body, _ = http_request(
                f"{agent_url}/jira/comments/{ticket_key}/{comment_id}",
                method="PUT",
                payload={
                    "text": "[Agent Test] Constellation Jira REST test comment — updated",
                    "permissions": _DEVELOPMENT_PERMISSIONS,
                },
            )
            reporter.show("comment-update", body)
            if status == 200 and body.get("result") == "updated":
                reporter.ok(f"Comment {comment_id} updated")
            else:
                reporter.fail(f"PUT /jira/comments/{ticket_key}/{comment_id} failed", f"status={status} body={body}")

        # TC-09 — comment delete --------------------------------------------
        if comment_id:
            reporter.step(f"TC-09  DELETE /jira/comments/{ticket_key}/{comment_id}")
            status, body, _ = http_request(
                f"{agent_url}/jira/comments/{ticket_key}/{comment_id}",
                method="DELETE",
                headers=_permission_headers(_CLEANUP_PERMISSIONS),
            )
            reporter.show("comment-delete", body)
            if status == 200 and body.get("result") == "deleted":
                # Remove from cleanup list since we already deleted it
                if comment_id in _cleanup_comment_ids:
                    _cleanup_comment_ids.remove(comment_id)
                reporter.ok(f"Comment {comment_id} deleted (cleaned up)")
            else:
                reporter.fail(f"DELETE /jira/comments/{ticket_key}/{comment_id} failed", f"status={status} body={body}")

        # TC-10 — field update (labels) with restore ------------------------
        reporter.step(f"TC-10  PUT /jira/tickets/{ticket_key} — labels update + restore")
        # Read current labels first and save for teardown safety net
        status_r, body_r, _ = http_request(f"{agent_url}/jira/tickets/{ticket_key}", headers=_READ_HEADERS)
        original_labels = []
        if status_r == 200:
            issue_payload = _as_dict(body_r.get("issue"))
            issue_fields = _as_dict(issue_payload.get("fields"))
            original_labels = issue_fields.get("labels") or []
            _original_state["summary"] = issue_fields.get("summary") or ""
            original_description = issue_fields.get("description")
            _original_state["description"] = original_description if original_description is not None else ""
        _original_state["labels"] = list(original_labels)  # save for teardown
        test_labels = list(dict.fromkeys(original_labels + ["constellation-agent-test"]))
        status, body, _ = http_request(
            f"{agent_url}/jira/tickets/{ticket_key}",
            method="PUT",
            payload={
                "fields": {"labels": test_labels},
                "permissions": _DEVELOPMENT_PERMISSIONS,
            },
        )
        reporter.show("field-update", body)
        if status == 200 and body.get("result") == "updated":
            reporter.ok(f"PUT /jira/tickets/{ticket_key} labels updated")
            # Restore
            restore_status, restore_body, _ = http_request(
                f"{agent_url}/jira/tickets/{ticket_key}",
                method="PUT",
                payload={
                    "fields": {"labels": original_labels},
                    "permissions": _DEVELOPMENT_PERMISSIONS,
                },
            )
            if restore_status == 200 and restore_body.get("result") == "updated":
                reporter.ok(f"Labels restored to original: {original_labels}")
            else:
                reporter.fail("Label restore failed", f"status={restore_status} body={restore_body}")
        else:
            reporter.fail(f"PUT /jira/tickets/{ticket_key} failed", f"status={status} body={body}")

        # TC-11 — transition with restore -----------------------------------
        if transitions:
            transition_names = [t.get("name") for t in transitions if isinstance(t, dict)]
            # Find current status and save for teardown safety net
            status_r2, body_r2, _ = http_request(f"{agent_url}/jira/tickets/{ticket_key}", headers=_READ_HEADERS)
            current_status = ""
            if status_r2 == 200:
                issue_payload = _as_dict(body_r2.get("issue"))
                issue_fields = _as_dict(issue_payload.get("fields"))
                status_payload = _as_dict(issue_fields.get("status"))
                current_status = str(status_payload.get("name") or "")
                _original_state["status"] = current_status  # save for teardown
            # Pick a non-current transition
            target_transition = None
            for t in transitions:
                if not isinstance(t, dict):
                    continue
                to_status = (t.get("to") or {}).get("name", "")
                if to_status and to_status.lower() != current_status.lower():
                    target_transition = t.get("name")
                    break
            if not target_transition and transition_names:
                target_transition = transition_names[0]

            if target_transition:
                reporter.step(f"TC-11  POST /jira/transitions/{ticket_key} → '{target_transition}' (+ restore)")
                status, body, _ = http_request(
                    f"{agent_url}/jira/transitions/{ticket_key}",
                    method="POST",
                    payload={
                        "transition": target_transition,
                        "permissions": _DEVELOPMENT_PERMISSIONS,
                    },
                )
                reporter.show("transition", body)
                if status == 200 and body.get("transitionId"):
                    reporter.ok(f"Transitioned {ticket_key}: {body.get('result')}")
                    # Restore to original status
                    if current_status:
                        restore_status2, restore_body2, _ = http_request(
                            f"{agent_url}/jira/transitions/{ticket_key}",
                            method="POST",
                            payload={
                                "transition": current_status,
                                "permissions": _DEVELOPMENT_PERMISSIONS,
                            },
                        )
                        if restore_status2 == 200 and restore_body2.get("transitionId"):
                            reporter.ok(f"Status restored to: {current_status}")
                        else:
                            reporter.fail(
                                f"Restore to '{current_status}' failed",
                                f"status={restore_status2} body={restore_body2}",
                            )
                else:
                    reporter.fail(
                        f"POST /jira/transitions/{ticket_key} failed",
                        f"status={status} body={body}",
                    )
            else:
                reporter.skip(f"TC-11 transition", "No suitable non-current transition found")
        else:
            reporter.skip("TC-11 transition", "No transitions available")

    finally:
        _teardown_cleanup()
        if proc:
            proc.terminate()
            proc.wait(timeout=5)

    print(f"\nPassed: {reporter.passed}  Failed: {reporter.failed}  Skipped: {reporter.skipped}")
    return 0 if reporter.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
