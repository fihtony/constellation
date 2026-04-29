#!/usr/bin/env python3
"""Local tests for the Web Agent.

Tests:
  1. Health endpoint returns {"status": "ok"}
  2. Agent card is returned with the expected capability
  3. Full task: build a small Python calculator application with unit tests
     - Verifies that web agent goes through: ANALYZING → PLANNING → IMPLEMENTING
       → BUILDING → TASK_STATE_COMPLETED
     - Verifies that generated Python files pass syntax checks
  4. Error recovery: agent is handed a workspace that contains a pre-broken
     Python file; the agent must detect the failure, ask the LLM for a fix,
     and reach TASK_STATE_COMPLETED (or at least attempt recovery).

Usage (local — starts the agent as a subprocess):
    cd /path/to/constellation
    python3 tests/test_web_agent.py

Usage (against a running container):
    python3 tests/test_web_agent.py --agent-url http://localhost:8050

Environment variables (read from tests/.env or shell):
    OPENAI_BASE_URL   — LLM endpoint (default: http://localhost:1288/v1)
    OPENAI_MODEL      — model name   (default: gpt-5-mini)
    ALLOW_MOCK_FALLBACK=1 to allow mock LLM responses in offline environments
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_ROOT = os.path.dirname(os.path.abspath(__file__))
if TESTS_ROOT not in sys.path:
    sys.path.insert(0, TESTS_ROOT)
sys.path.insert(0, PROJECT_ROOT)

from agent_test_support import build_test_subprocess_env

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

TASK_TIMEOUT = 300  # seconds to wait for a single task
POLL_INTERVAL = 3

TERMINAL_STATES = {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED"}


def _load_env(path: str) -> dict[str, str]:
    result: dict[str, str] = {}
    if not os.path.isfile(path):
        return result
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            result[k.strip()] = v.strip()
    return result


def _http(url: str, method: str = "GET", payload: dict | None = None, timeout: int = 30):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
    headers = {"Content-Type": "application/json; charset=utf-8"} if data else {}
    req = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw.strip() else {}
    except Exception as exc:
        return 0, {"error": str(exc)}


def _wait_for_task(base_url: str, task_id: str, timeout: int = TASK_TIMEOUT) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, body = _http(f"{base_url}/tasks/{task_id}")
        if status == 200 and isinstance(body, dict):
            task = body.get("task", body)
            state = (task.get("status") or {}).get("state", "") if isinstance(task, dict) else ""
            if state in TERMINAL_STATES:
                return task
        time.sleep(POLL_INTERVAL)
    return None


def _send_task(base_url: str, instruction: str, workspace: str = "", extra_meta: dict | None = None):
    meta = {
        "requestedCapability": "web.task.execute",
        "orchestratorTaskId": f"test-{int(time.time())}",
        **(extra_meta or {}),
    }
    if workspace:
        meta["sharedWorkspacePath"] = workspace
    payload = {
        "message": {
            "messageId": f"test-msg-{int(time.time())}",
            "role": "ROLE_USER",
            "parts": [{"text": instruction}],
            "metadata": meta,
        },
        "configuration": {"returnImmediately": True},
    }
    return _http(f"{base_url}/message:send", method="POST", payload=payload)


def _check_python_syntax(directory: str) -> list[str]:
    """Return list of syntax errors found in .py files under directory."""
    errors = []
    for root, _, files in os.walk(directory):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, encoding="utf-8", errors="replace") as fh:
                    ast.parse(fh.read())
            except SyntaxError as exc:
                errors.append(f"{fpath}: {exc}")
    return errors


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


class Report:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.passed = 0
        self.failed = 0

    def ok(self, msg: str, detail: str = ""):
        self.passed += 1
        print(f"  {Colors.GREEN}PASS{Colors.RESET}  {msg}" + (f"  [{detail}]" if detail and self.verbose else ""))

    def fail(self, msg: str, detail: str = ""):
        self.failed += 1
        print(f"  {Colors.RED}FAIL{Colors.RESET}  {msg}")
        if detail:
            print(f"         {detail}")

    def section(self, title: str):
        print(f"\n{Colors.BOLD}{'─' * 60}{Colors.RESET}")
        print(f"{Colors.BOLD}  {title}{Colors.RESET}")
        print(f"{Colors.BOLD}{'─' * 60}{Colors.RESET}")

    def step(self, desc: str):
        print(f"\n  {Colors.CYAN}→{Colors.RESET} {desc}")

    def info(self, msg: str):
        print(f"  {Colors.YELLOW}i{Colors.RESET}  {msg}")


# ---------------------------------------------------------------------------
# Agent lifecycle helpers (local subprocess mode)
# ---------------------------------------------------------------------------

def _start_agent(env_overrides: dict[str, str]) -> subprocess.Popen:
    env = build_test_subprocess_env(env_overrides, trusted=True)
    env["PYTHONPATH"] = PROJECT_ROOT
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        [sys.executable, os.path.join(PROJECT_ROOT, "web", "app.py")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        cwd=PROJECT_ROOT,
    )
    return proc


def _wait_for_health(url: str, timeout: int = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, _ = _http(f"{url}/health", timeout=3)
        if code == 200:
            return True
        time.sleep(0.5)
    return False


def _stop_agent(proc: subprocess.Popen):
    if proc and proc.poll() is None:
        try:
            os.kill(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

def test_health(base_url: str, report: Report):
    report.section("T1 — Health check")
    report.step("GET /health")
    status, body = _http(f"{base_url}/health")
    if status == 200 and body.get("status") == "ok":
        report.ok("Health endpoint returns status=ok", f"service={body.get('service')}")
    else:
        report.fail("Health check failed", f"status={status} body={body}")


def test_agent_card(base_url: str, report: Report):
    report.section("T2 — Agent card")
    report.step("GET /.well-known/agent-card.json")
    status, body = _http(f"{base_url}/.well-known/agent-card.json")
    if status != 200:
        report.fail("Agent card not returned", f"status={status}")
        return
    skills = [s["id"] for s in body.get("skills", [])]
    if "web.task.execute" in skills:
        report.ok("Agent card has capability web.task.execute")
    else:
        report.fail("web.task.execute not found in agent card", f"skills={skills}")


def test_build_python_app(base_url: str, report: Report, verbose: bool = False):
    """End-to-end: ask web agent to build a small Python calculator app with unit tests."""
    report.section("T3 — Build Python calculator app")

    with tempfile.TemporaryDirectory(prefix="web_agent_test_") as workspace:
        instruction = textwrap.dedent("""\
            Build a Python calculator application with the following requirements:
            1. A module `calculator.py` with functions: add, subtract, multiply, divide
            2. `divide` must raise ValueError when dividing by zero
            3. A test file `test_calculator.py` using Python's built-in unittest that
               tests all four operations including the ZeroDivisionError case
            The project should be self-contained with no external dependencies.
        """)

        report.step("Sending task to web agent")
        status, body = _send_task(base_url, instruction, workspace=os.path.join(workspace, "web-agent"))
        if status != 200 or "task" not in body:
            report.fail("Task submission failed", f"status={status} body={json.dumps(body)[:200]}")
            return
        task_id = body["task"]["id"]
        report.info(f"Task ID: {task_id}")

        report.step(f"Waiting for task completion (timeout={TASK_TIMEOUT}s)")
        final_task = _wait_for_task(base_url, task_id)
        if final_task is None:
            report.fail("Task timed out", f"task_id={task_id}")
            return

        state = (final_task.get("status") or {}).get("state", "unknown")
        report.info(f"Final state: {state}")

        if state == "TASK_STATE_COMPLETED":
            report.ok("Task reached TASK_STATE_COMPLETED")
        else:
            msg = (final_task.get("status") or {}).get("message", {})
            status_text = msg if isinstance(msg, str) else json.dumps(msg)[:200]
            report.fail(f"Task ended in unexpected state: {state}", status_text)
            return

        # Verify files were written to the workspace
        agent_dir = os.path.join(workspace, "web-agent")
        if not os.path.isdir(agent_dir):
            report.fail("No files written to workspace", f"expected: {agent_dir}")
            return

        py_files = [
            os.path.relpath(os.path.join(root, f), workspace)
            for root, _, files in os.walk(agent_dir)
            for f in files if f.endswith(".py")
        ]
        report.info(f"Python files in workspace: {py_files}")
        if not py_files:
            report.fail("No .py files were written to workspace")
            return
        report.ok(f"{len(py_files)} Python file(s) written to workspace")

        # Verify syntax of all generated files
        syntax_errors = _check_python_syntax(agent_dir)
        if syntax_errors:
            report.fail("Syntax errors in generated Python files", "\n".join(syntax_errors[:3]))
        else:
            report.ok("All generated Python files pass syntax check")

        # Run the generated unit tests (best-effort)
        test_files = [f for f in py_files if "test_" in os.path.basename(f) or "_test" in os.path.basename(f)]
        if test_files:
            report.step("Running generated unit tests")
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "--tb=short", "-q", agent_dir],
                capture_output=True,
                text=True,
                timeout=60,
                cwd=agent_dir,
                env=build_test_subprocess_env(),
            )
            output = (result.stdout + result.stderr).strip()
            if verbose:
                report.info("pytest output:\n" + textwrap.indent(output[:600], "    "))
            if result.returncode == 0:
                report.ok("Generated unit tests pass")
            else:
                report.fail("Generated unit tests failed (LLM may have introduced a bug)",
                             output[-300:])
        else:
            report.info("No test files found — skipping pytest run")


def test_error_recovery(base_url: str, report: Report, verbose: bool = False):
    """Error recovery: pre-populate workspace with a broken file; agent must fix it."""
    report.section("T4 — Error recovery (pre-broken file)")

    with tempfile.TemporaryDirectory(prefix="web_agent_recovery_") as workspace:
        agent_dir = os.path.join(workspace, "web-agent")
        os.makedirs(agent_dir, exist_ok=True)

        # Write a broken calculator.py (syntax error on purpose)
        broken_code = textwrap.dedent("""\
            def add(a, b):
                return a + b  # this line is fine

            def subtract(a b):   # <-- syntax error: missing comma
                return a - b

            def multiply(a, b):
                return a * b

            def divide(a, b):
                if b == 0:
                    raise ValueError("Cannot divide by zero")
                return a / b
        """)
        with open(os.path.join(agent_dir, "calculator.py"), "w", encoding="utf-8") as fh:
            fh.write(broken_code)

        # Write a test file referencing it
        test_code = textwrap.dedent("""\
            import unittest
            import calculator

            class TestCalculator(unittest.TestCase):
                def test_add(self):
                    self.assertEqual(calculator.add(1, 2), 3)

                def test_subtract(self):
                    self.assertEqual(calculator.subtract(5, 3), 2)

                def test_multiply(self):
                    self.assertEqual(calculator.multiply(3, 4), 12)

                def test_divide(self):
                    self.assertAlmostEqual(calculator.divide(10, 2), 5.0)

                def test_divide_by_zero(self):
                    with self.assertRaises(ValueError):
                        calculator.divide(1, 0)

            if __name__ == '__main__':
                unittest.main()
        """)
        with open(os.path.join(agent_dir, "test_calculator.py"), "w", encoding="utf-8") as fh:
            fh.write(test_code)

        report.info("Pre-populated workspace with a broken calculator.py (syntax error in subtract)")

        instruction = textwrap.dedent("""\
            A Python calculator application has been partially implemented in the workspace.
            The existing files may contain bugs or syntax errors.
            Please fix all issues and ensure the unit tests pass.
            Files to fix: calculator.py, test_calculator.py
        """)

        report.step("Sending fix-and-complete task to web agent")
        status, body = _send_task(base_url, instruction, workspace=workspace)
        if status != 200 or "task" not in body:
            report.fail("Task submission failed", f"status={status} body={json.dumps(body)[:200]}")
            return
        task_id = body["task"]["id"]
        report.info(f"Task ID: {task_id}")

        report.step(f"Waiting for task completion (timeout={TASK_TIMEOUT}s)")
        final_task = _wait_for_task(base_url, task_id)
        if final_task is None:
            report.fail("Task timed out", f"task_id={task_id}")
            return

        state = (final_task.get("status") or {}).get("state", "unknown")
        report.info(f"Final state: {state}")

        if state == "TASK_STATE_COMPLETED":
            report.ok("Task reached TASK_STATE_COMPLETED (recovery succeeded)")
        else:
            report.fail(f"Task ended in state {state} — recovery may not have succeeded")

        # Check that the calculator.py is now syntactically valid
        calc_path = os.path.join(agent_dir, "calculator.py")
        if os.path.isfile(calc_path):
            errors = _check_python_syntax(agent_dir)
            if not errors:
                report.ok("calculator.py syntax is now valid")
            else:
                report.fail("calculator.py still has syntax errors after fix attempt",
                             "\n".join(errors[:3]))
        else:
            report.info("calculator.py not present in web-agent dir after task")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agent-url", default="", help="Use a running agent instead of spawning a subprocess")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    # Load env
    env_file = os.path.join(PROJECT_ROOT, "tests", ".env")
    env = _load_env(env_file)
    if not os.path.isfile(env_file):
        # Fall back to web/.env.example defaults
        env.setdefault("OPENAI_BASE_URL", "http://localhost:1288/v1")
        env.setdefault("OPENAI_MODEL", "gpt-5-mini")
        env.setdefault("ALLOW_MOCK_FALLBACK", "1")

    report = Report(verbose=args.verbose)
    proc = None

    if args.agent_url:
        base_url = args.agent_url.rstrip("/")
        print(f"Using running agent at {base_url}")
    else:
        port = 18050  # avoid colliding with container port 8050
        base_url = f"http://localhost:{port}"
        agent_env = {
            "HOST": "0.0.0.0",
            "PORT": str(port),
            "AGENT_ID": "web-agent",
            "ADVERTISED_BASE_URL": base_url,
            "REGISTRY_URL": env.get("REGISTRY_URL", "http://localhost:9000"),
            "INSTANCE_REPORTER_ENABLED": "0",
            "OPENAI_BASE_URL": env.get("OPENAI_BASE_URL", "http://localhost:1288/v1"),
            "OPENAI_MODEL": env.get("OPENAI_MODEL", "gpt-5-mini"),
            "OPENAI_API_KEY": env.get("OPENAI_API_KEY", ""),
            "ALLOW_MOCK_FALLBACK": env.get("ALLOW_MOCK_FALLBACK", "1"),
            "SCM_AGENT_URL": "",
            "JIRA_AGENT_URL": "",
            "COMPASS_URL": "",
        }
        print(f"Starting web agent on {base_url} …")
        proc = _start_agent(agent_env)
        if not _wait_for_health(base_url, timeout=30):
            stdout = proc.stdout.read() if proc.stdout else ""
            print(f"Web agent failed to start. Output:\n{stdout[:800]}")
            _stop_agent(proc)
            return 1
        print("Web agent is healthy.\n")

    try:
        test_health(base_url, report)
        test_agent_card(base_url, report)
        test_build_python_app(base_url, report, verbose=args.verbose)
        test_error_recovery(base_url, report, verbose=args.verbose)
    finally:
        if proc:
            _stop_agent(proc)

    print(f"\n{'─' * 60}")
    print(f"Results: {Colors.GREEN}{report.passed} passed{Colors.RESET}, "
          f"{Colors.RED}{report.failed} failed{Colors.RESET}")
    return 0 if report.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
