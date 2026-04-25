#!/usr/bin/env python3
"""Constellation multi-agent end-to-end validation for the folder-based agent layout."""

from __future__ import annotations

import json
import os
import sys
import textwrap
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))
COMPASS_URL = "http://localhost:8080"
REGISTRY_URL = "http://localhost:9000"
JIRA_URL = "http://localhost:8010"
SCM_URL = "http://localhost:8020"

VERBOSE = "-v" in sys.argv or "--verbose" in sys.argv


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


passed = 0
failed = 0
errors = []


def section(title):
    print(f"\n{Colors.BOLD}{'═' * 60}{Colors.RESET}")
    print(f"{Colors.BOLD}  {title}{Colors.RESET}")
    print(f"{Colors.BOLD}{'═' * 60}{Colors.RESET}")


def step(desc):
    print(f"\n  {Colors.CYAN}→{Colors.RESET} {desc}")


def ok(msg):
    global passed
    passed += 1
    print(f"  {Colors.GREEN}✅ PASS{Colors.RESET} — {msg}")


def fail(msg, detail=""):
    global failed
    failed += 1
    errors.append(msg)
    print(f"  {Colors.RED}❌ FAIL{Colors.RESET} — {msg}")
    if detail:
        print(f"         {detail}")


def info(msg):
    print(f"  {Colors.YELLOW}ℹ{Colors.RESET}  {msg}")


def show_json(label, data):
    if VERBOSE:
        formatted = json.dumps(data, ensure_ascii=False, indent=2)
        print(f"     {label}:")
        print(textwrap.indent(formatted, "       "))


def wait_for(predicate, timeout=15, interval=0.5):
    deadline = time.time() + timeout
    last_value = None
    while time.time() < deadline:
        last_value = predicate()
        if last_value:
            return last_value
        time.sleep(interval)
    return last_value


def http_json(url, method="GET", payload=None, timeout=30):
    data = None
    headers = {}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    try:
        request = Request(url, data=data, headers=headers, method=method)
        with urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
            return response.status, body
    except HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {"raw": raw}
        return error.code, body
    except (URLError, OSError) as error:
        return 0, {"error": str(error)}


def send_message(text, requested_capability=None, timeout=30):
    payload = {
        "message": {
            "messageId": f"test-{int(time.time() * 1000)}",
            "role": "ROLE_USER",
            "parts": [{"text": text}],
        }
    }
    if requested_capability:
        payload["requestedCapability"] = requested_capability
    return http_json(
        f"{COMPASS_URL}/message:send",
        method="POST",
        payload=payload,
        timeout=timeout,
    )


def task_state(body):
    return body.get("task", {}).get("status", {}).get("state")


def task_id(body):
    return body.get("task", {}).get("id")


def task_agent(body):
    return body.get("task", {}).get("agentId")


def task_artifacts(body):
    return body.get("task", {}).get("artifacts", [])


def agent_ids_from_artifacts(artifacts):
    return [artifact.get("metadata", {}).get("agentId") for artifact in artifacts]


def android_instances():
    status, body = http_json(f"{REGISTRY_URL}/agents/android-agent/instances")
    if status == 200 and isinstance(body, list):
        return body
    return []


def test_0_prerequisites():
    section("Scenario 0: Prerequisites Check")

    for label, url in (
        ("Registry", f"{REGISTRY_URL}/health"),
        ("Compass", f"{COMPASS_URL}/health"),
        ("Jira Agent", f"{JIRA_URL}/health"),
        ("SCM Agent", f"{SCM_URL}/health"),
    ):
        step(f"Check {label} health")
        status, body = http_json(url)
        if status == 200:
            ok(f"{label} is healthy")
            show_json(label, body)
        else:
            fail(f"{label} is not reachable", f"status={status}")
            return False
    return True


def test_1_agent_card_discovery():
    section("Scenario 1: Agent Card Discovery")
    for label, url, expected_name in (
        ("Compass", f"{COMPASS_URL}/.well-known/agent-card.json", "Compass Agent"),
        ("Jira Agent", f"{JIRA_URL}/.well-known/agent-card.json", "Jira Agent"),
        ("SCM", f"{SCM_URL}/.well-known/agent-card.json", "SCM Agent"),
    ):
        step(f"Fetch {label} agent card")
        status, body = http_json(url)
        show_json(label, body)
        if status == 200 and body.get("name") == expected_name:
            ok(f"{label} agent card returned correctly")
        else:
            fail(f"Unexpected {label} agent card", f"status={status}")


def test_2_registry_state():
    section("Scenario 2: Registry State Verification")
    step("List all definitions")
    status, body = http_json(f"{REGISTRY_URL}/agents")
    show_json("Definitions", body)
    if status != 200 or not isinstance(body, list):
        fail("Failed to list registry definitions")
        return

    agent_ids = {item.get("agent_id") for item in body}
    expected = {"jira-agent", "scm-agent", "android-agent"}
    if expected.issubset(agent_ids):
        ok("All expected agent definitions are present")
    else:
        fail("Registry definitions are incomplete", f"found={sorted(agent_ids)}")

    for agent_id in ("jira-agent", "scm-agent"):
        step(f"Check instances for {agent_id}")
        status, instances = http_json(f"{REGISTRY_URL}/agents/{agent_id}/instances")
        show_json(agent_id, instances)
        if status == 200 and isinstance(instances, list) and instances:
            ok(f"{agent_id} has a live instance")
        else:
            fail(f"{agent_id} has no live instance")

    step("Android agent should start with no persistent instances")
    instances = android_instances()
    if not instances:
        ok("Android agent has no idle container before first launch")
    else:
        fail("Android agent unexpectedly started before launch", f"instances={instances}")


def test_3_jira_capability():
    section("Scenario 3: Jira Capability Routing")
    step("Route a Jira ticket request explicitly")
    status, body = send_message("Please analyze RIM-13175", requested_capability="jira.ticket.fetch")
    show_json("Response", body)
    if status == 200 and task_state(body) == "TASK_STATE_COMPLETED" and task_agent(body) == "jira-agent":
        ok("Jira capability request completed through jira-agent")
    else:
        fail("Tracker capability request failed", f"status={status}, state={task_state(body)}, agent={task_agent(body)}")


def test_4_scm_capability():
    section("Scenario 4: SCM Capability Routing")
    step("Route a SCM repository request explicitly")
    status, body = send_message(
        "Find the Android repository in SCM and summarize where to start.",
        requested_capability="scm.repo.inspect",
    )
    show_json("Response", body)
    if status == 200 and task_state(body) == "TASK_STATE_COMPLETED" and task_agent(body) == "scm-agent":
        ok("SCM capability request completed through scm-agent")
    else:
        fail("SCM capability request failed", f"status={status}, state={task_state(body)}, agent={task_agent(body)}")


def test_5_multi_agent_workflow():
    section("Scenario 5: Automatic Multi-Agent Workflow")
    step("Send a Tracker + SCM + Android request without forcing capability")
    status, body = send_message(
        "Analyze RIM-13175 and prepare the Android implementation plan using the CSM Android repo.",
        timeout=90,
    )
    show_json("Response", body)

    if status != 200:
        fail("Workflow request failed", f"HTTP {status}")
        return None

    if task_state(body) == "TASK_STATE_COMPLETED" and task_agent(body) == "android-agent":
        ok("Workflow completed and ended on android-agent")
    else:
        fail("Workflow did not end on android-agent", f"state={task_state(body)}, agent={task_agent(body)}")

    artifacts = task_artifacts(body)
    artifact_agent_ids = set(filter(None, agent_ids_from_artifacts(artifacts)))
    expected = {"tracker-agent", "scm-agent", "android-agent"}
    if expected.issubset(artifact_agent_ids):
        ok("Workflow artifacts include Tracker, SCM, and Android outputs")
    else:
        fail("Workflow artifacts are missing one or more agent outputs", f"agents={sorted(artifact_agent_ids)}")

    history = body.get("task", {}).get("history", [])
    states = [item.get("state") for item in history]
    if "STEP_COMPLETED" in states:
        ok("Task history records intermediate workflow steps")
    else:
        fail("Workflow history did not record intermediate steps", f"history={states}")

    return task_id(body)


def test_6_android_launch_lifecycle():
    section("Scenario 6: On-Demand Android Launch Lifecycle")
    step("Wait for Android per-task instances to be cleaned up after workflow completion")
    result = wait_for(lambda: True if not android_instances() else False, timeout=20, interval=1.0)
    if result:
        ok("Android per-task container cleaned up after task completion")
    else:
        current = android_instances()
        fail("Android per-task instance did not clean up in time", f"instances={current}")


def test_7_task_query_and_artifacts(task_identifier):
    section("Scenario 7: Task Query and Artifact Persistence")
    if not task_identifier:
        fail("No workflow task id available for query")
        return

    step("Query the Compass task")
    status, body = http_json(f"{COMPASS_URL}/tasks/{task_identifier}")
    show_json("Task", body)
    if status == 200 and body.get("task", {}).get("id") == task_identifier:
        ok(f"Task {task_identifier} can be queried")
    else:
        fail("Task query failed", f"status={status}")

    step("Query persisted artifacts")
    status, body = http_json(f"{COMPASS_URL}/tasks/{task_identifier}/artifacts")
    show_json("Artifacts", body)
    artifacts = body.get("artifacts", []) if isinstance(body, dict) else []
    if status == 200 and len(artifacts) >= 3:
        ok("Persisted artifact list returned with multiple entries")
    else:
        fail("Persisted artifact query returned too few entries", f"status={status}, count={len(artifacts)}")

    task_artifact_dir = os.path.join(PROJECT_ROOT, "artifact", task_identifier)
    if os.path.isdir(task_artifact_dir) and os.listdir(task_artifact_dir):
        ok("Artifacts were written to the mounted local artifact directory")
    else:
        fail("Mounted artifact directory is missing task outputs", task_artifact_dir)


def test_8_missing_capability():
    section("Scenario 8: Missing Capability")
    step("Request an unregistered capability")
    status, body = send_message("Please inspect the OpenShift cluster.", requested_capability="openshift.cluster.inspect")
    show_json("Response", body)
    if status == 200 and task_state(body) == "NO_CAPABLE_AGENT":
        ok("Missing capability returns NO_CAPABLE_AGENT")
    else:
        fail("Missing capability was not reported correctly", f"status={status}, state={task_state(body)}")


def test_9_deregister_reregister_jira():
    section("Scenario 9: Deregister and Re-Register Jira Agent")
    step("Deregister jira-agent")
    status, body = http_json(f"{REGISTRY_URL}/agents/jira-agent", method="DELETE")
    if status == 200 and body.get("status") == "deregistered":
        ok("jira-agent definition deregistered")
    else:
        fail("jira-agent deregistration failed", f"status={status}")
        return

    step("Request jira capability after deregistration")
    status, body = send_message("Please analyze RIM-13175", requested_capability="jira.ticket.fetch")
    if status == 200 and task_state(body) == "NO_CAPABLE_AGENT":
        ok("jira capability is rejected after deregistration")
    else:
        fail("jira capability should have been unavailable", f"state={task_state(body)}")

    step("Re-register jira-agent")
    status, body = http_json(
        f"{REGISTRY_URL}/agents",
        method="POST",
        payload={
            "agentId": "jira-agent",
            "version": "1.0.0",
            "displayName": "Jira Agent",
            "description": "Long-running Tracker integration agent.",
            "cardUrl": "http://jira:8010/.well-known/agent-card.json",
            "capabilities": ["jira.ticket.fetch"],
            "executionMode": "persistent",
            "scalingPolicy": {
                "maxInstances": 1,
                "perInstanceConcurrency": 1,
                "idleTimeoutSeconds": 300,
            },
            "registeredBy": "test-suite",
        },
    )
    if status == 201 and body.get("status") == "active":
        ok("jira-agent definition re-registered")
    else:
        fail("jira-agent re-registration failed", f"status={status}")
        return

    step("Request jira capability again")
    status, body = send_message("Please analyze RIM-13175", requested_capability="jira.ticket.fetch")
    if status == 200 and task_state(body) == "TASK_STATE_COMPLETED":
        ok("jira capability works again after re-registration")
    else:
        fail("jira capability did not recover after re-registration", f"state={task_state(body)}")


def test_10_busy_capacity():
    section("Scenario 10: Busy Capacity Handling")
    step("Load jira-agent instances")
    status, instances = http_json(f"{REGISTRY_URL}/agents/jira-agent/instances")
    if status != 200 or not isinstance(instances, list) or not instances:
        fail("Unable to load jira-agent instances for busy-capacity test")
        return

    instance_id = instances[0]["instance_id"]
    step(f"Mark jira-agent instance {instance_id} busy")
    status, _ = http_json(
        f"{REGISTRY_URL}/agents/jira-agent/instances/{instance_id}",
        method="PUT",
        payload={"status": "busy", "current_task_id": "manual-busy-simulation"},
    )
    if status != 200:
        fail("Failed to mark tracker-agent busy")
        return

    try:
        step("Request jira capability while the only instance is busy")
        status, body = send_message("Please analyze RIM-13175", requested_capability="jira.ticket.fetch")
        show_json("Response", body)
        if status == 200 and task_state(body) == "CAPACITY_EXHAUSTED":
            ok("Busy persistent agent returns CAPACITY_EXHAUSTED")
        else:
            fail("Busy persistent agent returned the wrong state", f"state={task_state(body)}")
    finally:
        http_json(
            f"{REGISTRY_URL}/agents/jira-agent/instances/{instance_id}",
            method="PUT",
            payload={"status": "idle", "current_task_id": None},
        )


def test_11_direct_agent_communication():
    section("Scenario 11: Direct Downstream Agent Communication")
    for label, url, text in (
        ("Jira Agent", JIRA_URL, "Please analyze RIM-13175"),
        ("SCM", SCM_URL, "Find the Android repository in SCM."),
    ):
        step(f"Send a direct message to {label} agent")
        status, body = http_json(
            f"{url}/message:send",
            method="POST",
            payload={
                "message": {
                    "messageId": f"direct-{label.lower()}",
                    "role": "ROLE_USER",
                    "parts": [{"text": text}],
                }
            },
        )
        show_json(label, body)
        if status == 200 and body.get("task", {}).get("status", {}).get("state") == "TASK_STATE_COMPLETED":
            ok(f"Direct {label} agent communication works")
        else:
            fail(f"Direct {label} agent communication failed", f"status={status}")


def test_12_browser_ui():
    section("Scenario 12: Browser UI")
    step("Open the Constellation Compass console")
    try:
        request = Request(f"{COMPASS_URL}/", method="GET")
        with urlopen(request, timeout=10) as response:
            html = response.read().decode("utf-8")
            status = response.status
    except Exception as error:
        status = 0
        html = str(error)

    if status == 200 and "Compass Agent" in html:
        ok("Browser UI is served successfully")
    else:
        fail("Browser UI is not available", f"status={status}")


def test_13_malformed_request():
    section("Scenario 13: Malformed Request")
    step("Send an empty body to the Compass agent")
    status, body = http_json(f"{COMPASS_URL}/message:send", method="POST", payload={})
    if status == 400:
        ok("Malformed Compass request returns HTTP 400")
    else:
        fail("Malformed Compass request did not return 400", f"status={status}, body={body}")


def run_all():
    global passed, failed, errors

    print(f"\n{Colors.BOLD}{'═' * 60}{Colors.RESET}")
    print(f"{Colors.BOLD}  Constellation — End-to-End Test Suite{Colors.RESET}")
    print(f"{Colors.BOLD}{'═' * 60}{Colors.RESET}")
    print(f"  Compass:       {COMPASS_URL}")
    print(f"  Registry:      {REGISTRY_URL}")
    print(f"  Jira Agent:       {JIRA_URL}")
    print(f"  SCM:     {SCM_URL}")
    print(f"  Verbose:       {VERBOSE}")
    print(f"  Time:          {time.strftime('%Y-%m-%d %H:%M:%S')}")

    if not test_0_prerequisites():
        print(f"\n{Colors.RED}ABORTED: Services are not running.{Colors.RESET}")
        print("\nStart them with:")
        print("  docker compose up --build -d")
        sys.exit(1)

    test_1_agent_card_discovery()
    test_2_registry_state()
    test_3_jira_capability()
    test_4_scm_capability()
    workflow_task_id = test_5_multi_agent_workflow()
    test_6_android_launch_lifecycle()
    test_7_task_query_and_artifacts(workflow_task_id)
    test_8_missing_capability()
    test_9_deregister_reregister_jira()
    test_10_busy_capacity()
    test_11_direct_agent_communication()
    test_12_browser_ui()
    test_13_malformed_request()

    total = passed + failed
    print(f"\n{Colors.BOLD}{'═' * 60}{Colors.RESET}")
    if failed == 0:
        print(f"  {Colors.GREEN}{Colors.BOLD}ALL {total} TESTS PASSED{Colors.RESET}")
    else:
        print(f"  {Colors.GREEN}{passed} passed{Colors.RESET}, {Colors.RED}{failed} failed{Colors.RESET} (total: {total})")
        print("\n  Failed tests:")
        for error in errors:
            print(f"    {Colors.RED}✗{Colors.RESET} {error}")
    print(f"{Colors.BOLD}{'═' * 60}{Colors.RESET}\n")
    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    run_all()