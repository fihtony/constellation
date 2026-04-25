#!/usr/bin/env python3
"""Comprehensive Jira agent test — auth diagnosis + all agent functions.

Sections
--------
A. Direct Jira REST auth diagnosis (no agent process needed).
B. Start the local Jira agent as a subprocess and test every function:
     fetch content, add/modify/delete comment, list transitions,
     change state (optional), change assignee (then restore).

Usage
-----
  python tests/test_jira_agent.py [-v] [--no-state-change] [--no-assignee-change]
  python tests/test_jira_agent.py --agent-url http://127.0.0.1:8010 (existing agent)
"""

from __future__ import annotations

import argparse
import base64
import os
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode

# Make sure we can import test support whether run from tests/ or project root
sys.path.insert(0, os.path.dirname(__file__))

from agent_test_support import (
    PROJECT_ROOT,
    Reporter,
    http_request,
    load_env_file,
    summary_exit_code,
)
from agent_test_targets import jira_ticket_key
from agent_test_targets import assert_jira_write_allowed

JIRA_TICKET = jira_ticket_key()
DEFAULT_LOCAL_AGENT_PORT = 18010
CONTAINER_AGENT_URL = "http://127.0.0.1:8010"
DEFAULT_EMAIL = "jira-user@example.com"
TEST_COMMENT_MARKER = "[jira-agent-test]"
TEST_LABEL = "jira-agent-test"


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _basic_header(email: str, token: str) -> str:
    encoded = base64.b64encode(f"{email}:{token}".encode()).decode("ascii")
    return f"Basic {encoded}"


def _bearer_header(token: str) -> str:
    return f"Bearer {token}"


def _jira_headers(mode: str, token: str, email: str) -> dict:
    if mode == "bearer":
        return {"Accept": "application/json", "Authorization": _bearer_header(token)}
    if mode == "basic":
        return {"Accept": "application/json", "Authorization": _basic_header(email, token)}
    raise ValueError(f"Unknown mode: {mode}")


def _looks_like_atlassian_cloud_site(url: str) -> bool:
    return ".atlassian.net" in (url or "").lower()


def _discover_cloud_id(jira_base: str, ca_bundle: str) -> str:
    status, body, _ = http_request(
        f"{jira_base.rstrip('/')}/_edge/tenant_info",
        ca_bundle=ca_bundle,
    )
    if status != 200 or not isinstance(body, dict):
        return ""
    return str(body.get("cloudId") or body.get("cloudid") or "").strip()


def _select_direct_api_base(
    reporter: Reporter,
    jira_base: str,
    configured_api_base: str,
    token: str,
    email: str,
    ca_bundle: str,
) -> tuple[str | None, dict | None, str | None, str]:
    site_api_base = configured_api_base.rstrip("/")

    reporter.step("A1. Auth: Bearer token only (no email)")
    status, body, _ = http_request(
        f"{site_api_base}/myself",
        headers=_jira_headers("bearer", token, email),
        ca_bundle=ca_bundle,
    )
    if status == 200:
        body_dict = _as_dict(body)
        reporter.info("Bearer auth accepted in this environment")
        reporter.info(f"  accountId={body_dict.get('accountId')} displayName={body_dict.get('displayName')}")
    else:
        reporter.ok(f"Bearer token-only auth rejected as expected (HTTP {status})")
        reporter.info(f"  body={body}")

    reporter.step("A2. Auth: Basic(email:token)")
    auth_headers = _jira_headers("basic", token, email)
    status, body, _ = http_request(
        f"{site_api_base}/myself",
        headers=auth_headers,
        ca_bundle=ca_bundle,
    )
    if status == 200:
        return site_api_base, auth_headers, "basic", "Basic(email:token) auth accepted"

    if status == 401 and _looks_like_atlassian_cloud_site(jira_base):
        cloud_id = _discover_cloud_id(jira_base, ca_bundle)
        if cloud_id:
            scoped_api_base = f"https://api.atlassian.com/ex/jira/{cloud_id}/rest/api/3"
            scoped_status, scoped_body, _ = http_request(
                f"{scoped_api_base}/myself",
                headers=auth_headers,
                ca_bundle=ca_bundle,
            )
            if scoped_status == 200:
                reporter.info("Site-scoped API token requires Atlassian API gateway")
                return scoped_api_base, auth_headers, "basic", (
                    "Scoped Basic(email:token) auth accepted via api.atlassian.com"
                )
            body = scoped_body
            status = scoped_status

    return None, None, None, f"HTTP {status} — {body}"


def _as_dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list_of_dicts(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _status_name_from_issue(issue_body) -> str:
    fields = _as_dict(_as_dict(issue_body).get("fields"))
    return _as_dict(fields.get("status")).get("name", "")


def _labels_from_issue(issue_body) -> list[str]:
    fields = _as_dict(_as_dict(issue_body).get("fields"))
    labels = fields.get("labels")
    if not isinstance(labels, list):
        return []
    return [label for label in labels if isinstance(label, str)]


def _transition_target_name(transition: dict) -> str:
    transition_dict = _as_dict(transition)
    target = _as_dict(transition_dict.get("to")).get("name", "")
    return target or transition_dict.get("name", "")


def _find_transition_to_status(transitions: list[dict], target_status: str) -> dict | None:
    target_lower = target_status.strip().lower()
    for transition in transitions:
        transition_name = transition.get("name", "")
        target_name = _transition_target_name(transition)
        if target_name.lower() == target_lower or transition_name.lower() == target_lower:
            return transition
    return None


def _pick_transition_for_test(current_status: str, transitions: list[dict]) -> dict | None:
    preferred_targets = (
        "Analysis",
        "In Progress",
        "Design",
        "Testing",
        "PO Review",
        "Ready for Sprint",
        "Sprint Backlog",
        "Refinement",
        "Next Priority",
        "Backlog",
        "Blocked",
    )
    terminal_targets = {"done", "cancelled", "deployed"}
    current_lower = current_status.strip().lower()
    candidates = [
        transition for transition in transitions
        if _transition_target_name(transition)
        and _transition_target_name(transition).lower() != current_lower
    ]
    for target_name in preferred_targets:
        match = _find_transition_to_status(candidates, target_name)
        if match is not None:
            return match
    for transition in candidates:
        if _transition_target_name(transition).lower() not in terminal_targets:
            return transition
    return candidates[0] if candidates else None


def _run_reversible_transition_test(
    reporter: Reporter,
    step_name: str,
    fetch_status,
    list_transitions,
    apply_transition,
) -> None:
    reporter.step(step_name)

    status_code, original_status, raw_body = fetch_status()
    if status_code != 200 or not original_status:
        reporter.fail(f"{step_name} failed", f"Could not read current status — HTTP {status_code} — {raw_body}")
        return

    transitions_code, transitions, transitions_body = list_transitions()
    if transitions_code != 200:
        reporter.fail(
            f"{step_name} failed",
            f"Could not list transitions — HTTP {transitions_code} — {transitions_body}",
        )
        return

    forward_transition = _pick_transition_for_test(original_status, transitions)
    if forward_transition is None:
        reporter.info(f"{step_name} skipped (no safe transition candidate from '{original_status}')")
        return

    forward_name = forward_transition.get("name", "")
    target_status = _transition_target_name(forward_transition)
    applied, apply_detail = apply_transition(forward_transition)
    if not applied:
        reporter.fail(
            f"{step_name} failed",
            f"Could not apply transition '{forward_name}' -> '{target_status}' — {apply_detail}",
        )
        return

    post_code, moved_status, post_body = fetch_status()
    if post_code == 200 and moved_status.lower() == target_status.lower():
        reporter.ok(f"State transitioned: '{original_status}' -> '{moved_status}' via '{forward_name}'")
    elif post_code == 200:
        reporter.fail(
            f"{step_name} failed",
            f"Expected status '{target_status}' after transition '{forward_name}', got '{moved_status}'",
        )
    else:
        reporter.fail(
            f"{step_name} failed",
            f"Could not verify post-transition status — HTTP {post_code} — {post_body}",
        )

    if post_code == 200 and moved_status.lower() == original_status.lower():
        return

    restore_code, restore_transitions, restore_body = list_transitions()
    if restore_code != 200:
        reporter.fail(
            "State restore failed",
            f"Could not list restore transitions — HTTP {restore_code} — {restore_body}",
        )
        return

    restore_transition = _find_transition_to_status(restore_transitions, original_status)
    if restore_transition is None:
        available = [_transition_target_name(transition) for transition in restore_transitions]
        reporter.fail(
            "State restore failed",
            f"No restore path back to '{original_status}'. Available targets: {available}",
        )
        return

    restore_name = restore_transition.get("name", "")
    restored, restore_detail = apply_transition(restore_transition)
    if not restored:
        reporter.fail(
            "State restore failed",
            f"Could not apply restore transition '{restore_name}' — {restore_detail}",
        )
        return

    final_code, final_status, final_body = fetch_status()
    if final_code == 200 and final_status.lower() == original_status.lower():
        reporter.ok(f"State restored: '{target_status}' -> '{final_status}' via '{restore_name}'")
    else:
        reporter.fail(
            "State restore verification failed",
            f"Expected final status '{original_status}', got '{final_status}' — HTTP {final_code} — {final_body}",
        )


def _run_reversible_label_update_test(
    reporter: Reporter,
    step_name: str,
    fetch_issue,
    apply_labels,
) -> None:
    reporter.step(step_name)

    status_code, issue_body = fetch_issue()
    if status_code != 200:
        reporter.fail(step_name, f"Could not read current labels — HTTP {status_code} — {issue_body}")
        return

    original_labels = sorted(set(_labels_from_issue(issue_body)))
    updated_labels = sorted(set(original_labels + [TEST_LABEL]))

    applied, apply_detail = apply_labels(updated_labels)
    if not applied:
        reporter.fail(step_name, f"Could not apply labels {updated_labels} — {apply_detail}")
        return

    verify_code, verify_issue = fetch_issue()
    verify_labels = sorted(set(_labels_from_issue(verify_issue))) if verify_code == 200 else []
    if verify_code == 200 and TEST_LABEL in verify_labels:
        reporter.ok(f"Label update applied: {TEST_LABEL}")
    else:
        reporter.fail(
            step_name,
            f"Expected labels to include '{TEST_LABEL}', got {verify_labels} — HTTP {verify_code} — {verify_issue}",
        )

    restored, restore_detail = apply_labels(original_labels)
    if not restored:
        reporter.fail("Label restore failed", f"Could not restore labels — {restore_detail}")
        return

    final_code, final_issue = fetch_issue()
    final_labels = sorted(set(_labels_from_issue(final_issue))) if final_code == 200 else []
    if final_code == 200 and final_labels == original_labels:
        reporter.ok(f"Labels restored: {final_labels}")
    else:
        reporter.fail(
            "Label restore verification failed",
            f"Expected labels {original_labels}, got {final_labels} — HTTP {final_code} — {final_issue}",
        )


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-url", default="",
                        help="Use an already-running agent instead of starting one.")
    parser.add_argument("--container", action="store_true",
                        help="Point at the containerised agent on port 8010.")
    parser.add_argument("--email", default=DEFAULT_EMAIL)
    parser.add_argument("--no-state-change", action="store_true",
                        help="Skip the transition (state change) write test.")
    parser.add_argument("--no-assignee-change", action="store_true",
                        help="Skip the assignee change write test.")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Section A — direct Jira REST API tests
# ---------------------------------------------------------------------------

def run_direct_tests(reporter: Reporter, jira_base: str, jira_api_base: str,
                     token: str, email: str, ca_bundle: str, skip_state_change: bool,
                     skip_assignee_change: bool) -> str | None:
    """Test auth modes and all Jira functions directly. Returns accepted auth mode."""

    reporter.section("Section A — Direct Jira REST API Tests")

    if not token:
        reporter.fail("JIRA_TOKEN is missing in jira/.env")
        return None

    api_base_url, auth_headers, accepted_mode, auth_message = _select_direct_api_base(
        reporter,
        jira_base,
        jira_api_base,
        token,
        email,
        ca_bundle,
    )
    if not api_base_url or not auth_headers or not accepted_mode:
        reporter.fail("Basic(email:token) auth rejected", auth_message)
        reporter.info("Cannot proceed with function tests — no working auth mode.")
        return None
    status, body, _ = http_request(f"{api_base_url}/myself", headers=auth_headers, ca_bundle=ca_bundle)
    body_dict = _as_dict(body)
    reporter.ok(auth_message)
    my_account_id = body_dict.get("accountId", "")
    my_display_name = body_dict.get("displayName", "")
    reporter.info(f"  apiBase={api_base_url}")
    reporter.info(f"  accountId={my_account_id} displayName={my_display_name}")

    # --- A3. Fetch ticket content ---
    reporter.step(f"A3. Fetch ticket content: {JIRA_TICKET}")
    status, body, _ = http_request(
        f"{api_base_url}/issue/{JIRA_TICKET}",
        headers=auth_headers,
        ca_bundle=ca_bundle,
    )
    body_dict = _as_dict(body)
    if status == 200 and body_dict.get("key") == JIRA_TICKET:
        fields = _as_dict(body_dict.get("fields"))
        title = fields.get("summary", "")
        original_status = _status_name_from_issue(body_dict)
        description = fields.get("description")
        due_date = fields.get("duedate")
        assignee = _as_dict(fields.get("assignee"))
        comments = _as_dict(fields.get("comment")).get("comments", [])
        attachments = fields.get("attachment", []) if isinstance(fields.get("attachment"), list) else []
        reporter.ok(f"Ticket fetched: '{title}'")
        reporter.info(f"  status={original_status}  due={due_date}  assignee={assignee.get('displayName','(none)')}"
                      f"  comments={len(comments)}  attachments={len(attachments)}")
        if reporter.verbose:
            reporter.show("description", description)
        original_assignee_id = assignee.get("accountId")
    else:
        reporter.fail("Ticket fetch failed", f"HTTP {status}")
        return accepted_mode

    def fetch_issue_direct():
        status, body, _ = http_request(
            f"{api_base_url}/issue/{JIRA_TICKET}?fields=labels,status,assignee,comment,attachment,summary,description,duedate",
            headers=auth_headers,
            ca_bundle=ca_bundle,
        )
        return status, body

    def apply_labels_direct(labels: list[str]):
        status, body, _ = http_request(
            f"{api_base_url}/issue/{JIRA_TICKET}",
            method="PUT",
            payload={"fields": {"labels": labels}},
            headers=auth_headers,
            ca_bundle=ca_bundle,
        )
        if status in (200, 204):
            return True, f"HTTP {status}"
        return False, f"HTTP {status} — {body}"

    _run_reversible_label_update_test(
        reporter,
        "A4. Update labels directly (then restore)",
        fetch_issue_direct,
        apply_labels_direct,
    )

    # --- A5. Add comment ---
    reporter.step("A5. Add comment")
    comment_text = f"{TEST_COMMENT_MARKER} auth test — please ignore"
    payload = {
        "body": {
            "type": "doc", "version": 1,
            "content": [{"type": "paragraph",
                         "content": [{"type": "text", "text": comment_text}]}],
        }
    }
    status, body, _ = http_request(
        f"{api_base_url}/issue/{JIRA_TICKET}/comment",
        method="POST", payload=payload, headers=auth_headers, ca_bundle=ca_bundle,
    )
    if status == 201:
        new_comment_id = body.get("id")
        reporter.ok(f"Comment added: id={new_comment_id}")
    else:
        reporter.fail("Add comment failed", f"HTTP {status} — {body}")
        new_comment_id = None

    # --- A6. Modify comment ---
    if new_comment_id:
        reporter.step("A6. Modify comment")
        updated_text = f"{TEST_COMMENT_MARKER} auth test (updated) — please ignore"
        updated_payload = {
            "body": {
                "type": "doc", "version": 1,
                "content": [{"type": "paragraph",
                             "content": [{"type": "text", "text": updated_text}]}],
            }
        }
        status, body, _ = http_request(
            f"{api_base_url}/issue/{JIRA_TICKET}/comment/{new_comment_id}",
            method="PUT", payload=updated_payload, headers=auth_headers, ca_bundle=ca_bundle,
        )
        if status == 200:
            reporter.ok(f"Comment updated: id={new_comment_id}")
        else:
            reporter.fail("Update comment failed", f"HTTP {status} — {body}")

    # --- A7. List transitions ---
    reporter.step("A7. List available transitions (state changes)")
    status, body, _ = http_request(
        f"{api_base_url}/issue/{JIRA_TICKET}/transitions",
        headers=auth_headers, ca_bundle=ca_bundle,
    )
    if status == 200:
        transitions = _as_list_of_dicts(_as_dict(body).get("transitions"))
        names = [t.get("name") for t in transitions]
        reporter.ok(f"Transitions: {names}")
    else:
        reporter.fail("List transitions failed", f"HTTP {status}")
        transitions = []

    def fetch_status_direct():
        status, body, _ = http_request(
            f"{api_base_url}/issue/{JIRA_TICKET}?fields=status",
            headers=auth_headers,
            ca_bundle=ca_bundle,
        )
        return status, _status_name_from_issue(body), body

    def list_transitions_direct():
        status, body, _ = http_request(
            f"{api_base_url}/issue/{JIRA_TICKET}/transitions",
            headers=auth_headers,
            ca_bundle=ca_bundle,
        )
        return status, _as_list_of_dicts(_as_dict(body).get("transitions")), body

    def apply_transition_direct(transition: dict):
        transition_id = transition.get("id")
        if not transition_id:
            return False, "transition_missing_id"
        status, body, _ = http_request(
            f"{api_base_url}/issue/{JIRA_TICKET}/transitions",
            method="POST",
            payload={"transition": {"id": transition_id}},
            headers=auth_headers,
            ca_bundle=ca_bundle,
        )
        if status in (200, 204):
            return True, f"id={transition_id}"
        return False, f"HTTP {status} — {body}"

    # --- A8. Change state, then restore ---
    if skip_state_change:
        reporter.info("A8. Skipped (--no-state-change)")
    else:
        _run_reversible_transition_test(
            reporter,
            "A8. Change state directly (then restore)",
            fetch_status_direct,
            list_transitions_direct,
            apply_transition_direct,
        )

    # --- A9. Change assignee to self, then restore ---
    if skip_assignee_change:
        reporter.info("A9. Skipped (--no-assignee-change)")
    else:
        reporter.step("A9. Change assignee (to self, then restore original)")
        status, body, _ = http_request(
            f"{api_base_url}/issue/{JIRA_TICKET}/assignee",
            method="PUT",
            payload={"accountId": my_account_id},
            headers=auth_headers,
            ca_bundle=ca_bundle,
        )
        if status in (200, 204):
            reporter.ok(f"Assignee changed to self ({my_display_name})")
            restore_payload = {"accountId": original_assignee_id} if original_assignee_id else {"accountId": None}
            status2, _, _ = http_request(
                f"{api_base_url}/issue/{JIRA_TICKET}/assignee",
                method="PUT",
                payload=restore_payload,
                headers=auth_headers,
                ca_bundle=ca_bundle,
            )
            if status2 in (200, 204):
                reporter.ok("Assignee restored to original")
            else:
                reporter.fail("Assignee restore failed", f"HTTP {status2}")
        else:
            reporter.fail("Change assignee failed", f"HTTP {status} — {body}")

    # --- Cleanup: delete test comment ---
    if new_comment_id:
        reporter.step("A10. Delete test comment (cleanup)")
        status, _, _ = http_request(
            f"{api_base_url}/issue/{JIRA_TICKET}/comment/{new_comment_id}",
            method="DELETE", headers=auth_headers, ca_bundle=ca_bundle,
        )
        if status in (200, 204):
            reporter.ok("Test comment deleted")
        else:
            reporter.fail("Delete comment failed", f"HTTP {status}")

    reporter.step("A11. Search issues directly with JQL")
    status, body, _ = http_request(
        f"{api_base_url}/search/jql",
        method="POST",
        payload={"jql": f"key = {JIRA_TICKET}", "maxResults": 5, "fields": ["summary", "status"]},
        headers=auth_headers,
        ca_bundle=ca_bundle,
    )
    issues = _as_list_of_dicts(_as_dict(body).get("issues"))
    matched_keys = [issue.get("key") for issue in issues]
    if status == 200 and JIRA_TICKET in matched_keys:
        reporter.ok("JQL search returned the expected issue")
    else:
        reporter.fail("JQL search failed", f"HTTP {status} — {body}")

    return accepted_mode


# ---------------------------------------------------------------------------
# Section B — agent-based tests
# ---------------------------------------------------------------------------

def wait_for_agent(agent_url: str, ca_bundle: str, timeout: int = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _, _ = http_request(f"{agent_url}/health", ca_bundle=ca_bundle)
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def _pick_free_local_port(start_port: int = DEFAULT_LOCAL_AGENT_PORT) -> int:
    for port in range(start_port, start_port + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_local_agent(env_values: dict, ca_bundle: str, agent_url: str) -> subprocess.Popen | None:
    """Spawn the Jira agent locally. Returns the Popen object or None on failure."""
    venv_python = os.path.join(PROJECT_ROOT, "venv", "bin", "python")
    if not os.path.isfile(venv_python):
        return None

    port = str(agent_url.rsplit(":", 1)[1])

    env = os.environ.copy()
    env.update({
        "HOST": "127.0.0.1",
        "PORT": port,
        "ADVERTISED_BASE_URL": agent_url,
        "REGISTRY_URL": "http://127.0.0.1:9000",
        "JIRA_BASE_URL": env_values.get("JIRA_BASE_URL", "https://your-org.atlassian.net"),
        "JIRA_API_BASE_URL": env_values.get("JIRA_API_BASE_URL",
                                            "https://your-org.atlassian.net/rest/api/3"),
        "JIRA_CLOUD_ID": env_values.get("JIRA_CLOUD_ID", ""),
        "JIRA_TOKEN": env_values.get("JIRA_TOKEN", ""),
        "JIRA_EMAIL": env_values.get("JIRA_EMAIL", ""),
        "JIRA_AUTH_MODE": env_values.get("JIRA_AUTH_MODE", "basic"),
        "CORP_CA_BUNDLE": ca_bundle,
        "ALLOW_MOCK_FALLBACK": "1",
        "PYTHONPATH": PROJECT_ROOT,
    })

    proc = subprocess.Popen(
        [venv_python, "jira/app.py"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def run_agent_tests(reporter: Reporter, agent_url: str, ca_bundle: str,
                    skip_state_change: bool, skip_assignee_change: bool,
                    env_values: dict) -> None:

    reporter.section(f"Section B — Agent Tests ({agent_url})")

    # B1. Health
    reporter.step("B1. Agent health check")
    status, body, _ = http_request(f"{agent_url}/health", ca_bundle=ca_bundle)
    if status == 200:
        reporter.ok(f"Agent healthy: {body.get('agent_id', '?')}")
    else:
        reporter.fail("Agent not healthy", f"HTTP {status}")
        return

    # B2. Agent card
    reporter.step("B2. Agent card (.well-known)")
    status, body, _ = http_request(f"{agent_url}/.well-known/agent-card.json", ca_bundle=ca_bundle)
    if status == 200 and body.get("name"):
        reporter.ok(f"Agent card: {body['name']}")
    else:
        reporter.fail("Agent card missing or malformed", f"HTTP {status}")

    # B3. Get authenticated user (myself)
    reporter.step("B3. Get authenticated user via agent")
    status, body, _ = http_request(f"{agent_url}/jira/myself", ca_bundle=ca_bundle)
    body_dict = _as_dict(body)
    if status == 200 and body_dict.get("result") == "ok":
        user = _as_dict(body_dict.get("user"))
        my_account_id = user.get("accountId", "")
        my_display = user.get("displayName", "")
        reporter.ok(f"Authenticated as: {my_display} (accountId={my_account_id})")
    else:
        reporter.fail("Could not get authenticated user", f"HTTP {status} — {body}")
        my_account_id = ""

    # B4. Fetch ticket
    reporter.step(f"B4. Fetch ticket {JIRA_TICKET} via agent")
    status, body, _ = http_request(
        f"{agent_url}/jira/tickets/{JIRA_TICKET}", ca_bundle=ca_bundle
    )
    body_dict = _as_dict(body)
    if status == 200 and body_dict.get("status") == "fetched":
        issue = _as_dict(body_dict.get("issue"))
        fields = _as_dict(issue.get("fields"))
        title = fields.get("summary", "(no title)")
        due = fields.get("duedate", "none")
        assignee = _as_dict(fields.get("assignee"))
        assignee_name = assignee.get("displayName", "(unassigned)")
        original_assignee_id = assignee.get("accountId")
        comment_count = len(_as_dict(fields.get("comment")).get("comments", []))
        attachments = fields.get("attachment", []) if isinstance(fields.get("attachment"), list) else []
        attachment_count = len(attachments)
        original_labels = _labels_from_issue(issue)
        reporter.ok(
            f"Ticket: '{title}' | due={due} | assignee={assignee_name}"
            f" | comments={comment_count} | attachments={attachment_count}"
        )
    else:
        reporter.fail("Ticket fetch failed via agent", f"HTTP {status} — {body}")
        original_assignee_id = None
        original_labels = []

    def fetch_issue_via_agent():
        status, body, _ = http_request(
            f"{agent_url}/jira/tickets/{JIRA_TICKET}",
            ca_bundle=ca_bundle,
        )
        issue = _as_dict(_as_dict(body).get("issue"))
        return status, issue

    def apply_labels_via_agent(labels: list[str]):
        status, body, _ = http_request(
            f"{agent_url}/jira/tickets/{JIRA_TICKET}",
            method="PUT",
            payload={"fields": {"labels": labels}},
            ca_bundle=ca_bundle,
        )
        if status == 200 and _as_dict(body).get("result") == "updated":
            return True, body
        return False, f"HTTP {status} — {body}"

    _run_reversible_label_update_test(
        reporter,
        "B5. Update labels via agent (then restore)",
        fetch_issue_via_agent,
        apply_labels_via_agent,
    )

    # B6. Add comment via agent
    reporter.step("B6. Add comment via agent")
    status, body, _ = http_request(
        f"{agent_url}/jira/comments/{JIRA_TICKET}",
        method="POST",
        payload={"text": f"{TEST_COMMENT_MARKER} agent test — please ignore"},
        ca_bundle=ca_bundle,
    )
    if status == 201 and body.get("commentId"):
        new_comment_id = body["commentId"]
        reporter.ok(f"Comment added: id={new_comment_id}")
    else:
        reporter.fail("Add comment via agent failed", f"HTTP {status} — {body}")
        new_comment_id = None

    # B7. Update comment via agent
    if new_comment_id:
        reporter.step("B7. Update comment via agent")
        status, body, _ = http_request(
            f"{agent_url}/jira/comments/{JIRA_TICKET}/{new_comment_id}",
            method="PUT",
            payload={"text": f"{TEST_COMMENT_MARKER} agent test (updated) — please ignore"},
            ca_bundle=ca_bundle,
        )
        if status == 200 and body.get("result") == "updated":
            reporter.ok("Comment updated via agent")
        else:
            reporter.fail("Update comment via agent failed", f"HTTP {status} — {body}")

    # B8. List transitions via agent
    reporter.step("B8. List transitions via agent")
    status, body, _ = http_request(
        f"{agent_url}/jira/transitions/{JIRA_TICKET}", ca_bundle=ca_bundle
    )
    body_dict = _as_dict(body)
    if status == 200 and body_dict.get("result") == "ok":
        transitions = _as_list_of_dicts(body_dict.get("transitions"))
        names = [t.get("name") for t in transitions]
        reporter.ok(f"Transitions: {names}")
    else:
        reporter.fail("List transitions via agent failed", f"HTTP {status} — {body}")

    def fetch_status_via_agent():
        status, body, _ = http_request(
            f"{agent_url}/jira/tickets/{JIRA_TICKET}",
            ca_bundle=ca_bundle,
        )
        issue = _as_dict(_as_dict(body).get("issue"))
        return status, _status_name_from_issue(issue), body

    def list_transitions_via_agent():
        status, body, _ = http_request(
            f"{agent_url}/jira/transitions/{JIRA_TICKET}",
            ca_bundle=ca_bundle,
        )
        body_dict = _as_dict(body)
        return status, _as_list_of_dicts(body_dict.get("transitions")), body

    def apply_transition_via_agent(transition: dict):
        transition_name = transition.get("name") or _transition_target_name(transition)
        if not transition_name:
            return False, "missing_transition_name"
        status, body, _ = http_request(
            f"{agent_url}/jira/transitions/{JIRA_TICKET}",
            method="POST",
            payload={"transition": transition_name},
            ca_bundle=ca_bundle,
        )
        if status == 200 and _as_dict(body).get("transitionId"):
            return True, _as_dict(body).get("result", "ok")
        return False, f"HTTP {status} — {body}"

    # B9. Change ticket state via agent, then restore
    if skip_state_change:
        reporter.info("B9. Skipped (--no-state-change)")
    else:
        _run_reversible_transition_test(
            reporter,
            "B9. Change state via agent (then restore)",
            fetch_status_via_agent,
            list_transitions_via_agent,
            apply_transition_via_agent,
        )

    # B10. Change assignee via agent (then restore)
    if not skip_assignee_change and my_account_id:
        reporter.step("B10. Change assignee via agent (to self, then restore)")
        status, body, _ = http_request(
            f"{agent_url}/jira/assignee/{JIRA_TICKET}",
            method="PUT",
            payload={"accountId": my_account_id},
            ca_bundle=ca_bundle,
        )
        if status == 200 and body.get("result") == "assigned":
            reporter.ok(f"Assignee changed to self via agent")
            # Restore original
            restore_id = original_assignee_id
            status2, body2, _ = http_request(
                f"{agent_url}/jira/assignee/{JIRA_TICKET}",
                method="PUT",
                payload={"accountId": restore_id},
                ca_bundle=ca_bundle,
            )
            if status2 == 200 and body2.get("result") == "assigned":
                reporter.ok("Assignee restored via agent")
            else:
                reporter.fail("Assignee restore via agent failed", f"HTTP {status2} — {body2}")
        else:
            reporter.fail("Change assignee via agent failed", f"HTTP {status} — {body}")
    elif skip_assignee_change:
        reporter.info("B10. Skipped (--no-assignee-change)")
    else:
        reporter.info("B10. Skipped (no accountId resolved)")

    # B11. Cleanup: delete test comment
    if new_comment_id:
        reporter.step("B11. Delete test comment via agent (cleanup)")
        status, body, _ = http_request(
            f"{agent_url}/jira/comments/{JIRA_TICKET}/{new_comment_id}",
            method="DELETE",
            ca_bundle=ca_bundle,
        )
        if status == 200 and body.get("result") == "deleted":
            reporter.ok("Test comment deleted via agent")
        else:
            reporter.fail("Delete comment via agent failed", f"HTTP {status} — {body}")

    reporter.step("B12. Search issues via agent with JQL")
    query = urlencode({"jql": f"key = {JIRA_TICKET}", "maxResults": 5, "fields": "summary,status"})
    status, body, _ = http_request(
        f"{agent_url}/jira/search?{query}",
        ca_bundle=ca_bundle,
    )
    search_body = _as_dict(_as_dict(body).get("search"))
    issues = _as_list_of_dicts(search_body.get("issues"))
    matched_keys = [issue.get("key") for issue in issues]
    if status == 200 and _as_dict(body).get("result") == "ok" and JIRA_TICKET in matched_keys:
        reporter.ok("JQL search via agent returned the expected issue")
    else:
        reporter.fail("JQL search via agent failed", f"HTTP {status} — {body}")

    reporter.step("B13. Validate create-issue guard rails via agent")
    status, body, _ = http_request(
        f"{agent_url}/jira/tickets",
        method="POST",
        payload={"summary": "should-not-create-in-shared-tests"},
        ca_bundle=ca_bundle,
    )
    if status == 400 and _as_dict(body).get("error") == "missing projectKey":
        reporter.ok("Create endpoint rejected incomplete input without creating a new ticket")
    else:
        reporter.fail("Create endpoint validation failed", f"HTTP {status} — {body}")

    reporter.step("B14. Exercise the message interface")
    status, body, _ = http_request(
        f"{agent_url}/message:send",
        method="POST",
        payload={
            "message": {
                "messageId": "jira-agent-test",
                "role": "ROLE_USER",
                "parts": [{"text": f"Summarize Jira ticket {JIRA_TICKET} for implementation planning."}],
            }
        },
        ca_bundle=ca_bundle,
    )
    task = _as_dict(body).get("task", {}) if isinstance(body, dict) else {}
    state = _as_dict(task.get("status")).get("state")
    if status == 200 and state == "TASK_STATE_COMPLETED":
        reporter.ok("Jira message flow completed")
    else:
        reporter.fail("Jira message flow failed", f"HTTP {status} — {body}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    args = parse_args(argv)
    reporter = Reporter(verbose=args.verbose)

    env_values = load_env_file("jira/.env")
    jira_base = env_values.get("JIRA_BASE_URL", "https://your-org.atlassian.net").rstrip("/")
    jira_api_base = env_values.get("JIRA_API_BASE_URL", f"{jira_base}/rest/api/3").rstrip("/")
    token = env_values.get("JIRA_TOKEN", "")
    email = args.email or env_values.get("JIRA_EMAIL", DEFAULT_EMAIL)
    ca_bundle = os.path.join(PROJECT_ROOT, "certs", "slf-ca-bundle.crt")
    assert_jira_write_allowed(JIRA_TICKET)

    # Section A — direct REST tests
    run_direct_tests(
        reporter,
        jira_base,
        jira_api_base,
        token,
        email,
        ca_bundle,
        skip_state_change=args.no_state_change,
        skip_assignee_change=args.no_assignee_change,
    )

    # Determine agent URL
    if args.agent_url:
        agent_url = args.agent_url.rstrip("/")
        proc = None
    elif args.container:
        agent_url = CONTAINER_AGENT_URL
        proc = None
    else:
        agent_url = f"http://127.0.0.1:{_pick_free_local_port()}"
        reporter.section("Starting local Jira agent subprocess")
        proc = start_local_agent(env_values, ca_bundle, agent_url)
        if proc is None:
            reporter.fail("Could not start local agent — venv not found or launch failed")
            proc = None
        else:
            reporter.info(f"Agent PID {proc.pid} — waiting for /health ...")
            if wait_for_agent(agent_url, ca_bundle):
                reporter.ok("Local agent is up")
            else:
                reporter.fail("Local agent did not become healthy in time")
                proc.terminate()
                proc.wait()
                proc = None

    # Section B — agent function tests
    if proc is not None or args.agent_url or args.container:
        run_agent_tests(
            reporter, agent_url, ca_bundle,
            skip_state_change=args.no_state_change,
            skip_assignee_change=args.no_assignee_change,
            env_values=env_values,
        )

    # Clean up subprocess
    if proc is not None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    return summary_exit_code(reporter)


if __name__ == "__main__":
    raise SystemExit(main())