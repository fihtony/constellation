"""
Copilot CLI container test agent.

Tests that GitHub Copilot CLI can be used non-interactively inside a Docker container
using the COPILOT_GITHUB_TOKEN environment variable for authentication.

Usage:
    docker run --rm \
        -e COPILOT_GITHUB_TOKEN=<your-fine-grained-pat> \
        constellation-copilot-cli-test

The PAT must have the "Copilot Requests" permission.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time


COPILOT_TOKEN = os.environ.get("COPILOT_GITHUB_TOKEN", "")
MODEL = os.environ.get("COPILOT_MODEL", "gpt-5-mini")


def log(msg: str):
    ts = time.strftime("%H:%M:%S")
    print(f"[copilot-cli-test][{ts}] {msg}", flush=True)


def run_copilot(prompt: str, model: str = MODEL, timeout: int = 60) -> tuple[bool, str]:
    """
    Run Copilot CLI non-interactively and return (success, response_text).

    Uses:
        copilot -sp "PROMPT"   (-s = silent mode: response only, -p = prompt)
    or with model selection:
        copilot --model MODEL -sp "PROMPT"
    """
    if not COPILOT_TOKEN:
        return False, "COPILOT_GITHUB_TOKEN is not set."

    cmd = ["copilot"]
    if model:
        cmd += ["--model", model]
    cmd += ["-sp", prompt]

    env = dict(os.environ)
    env["COPILOT_GITHUB_TOKEN"] = COPILOT_TOKEN

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        output = result.stdout.strip()
        stderr = result.stderr.strip()
        if result.returncode != 0:
            return False, f"Exit {result.returncode}: stdout={output[:500]} stderr={stderr[:500]}"
        return True, output
    except subprocess.TimeoutExpired:
        return False, f"Copilot CLI timed out after {timeout}s"
    except FileNotFoundError:
        return False, "copilot binary not found — is @github/copilot installed?"
    except Exception as exc:
        return False, f"Unexpected error: {exc}"


def test_basic_question():
    """Test: ask a simple math question."""
    log("Test 1: Basic question — what is 2+2?")
    ok, response = run_copilot("What is 2+2? Reply with only the number.")
    if ok and "4" in response:
        log(f"  PASS — response: {response[:100]}")
        return True
    log(f"  FAIL — ok={ok}, response: {response[:300]}")
    return False


def test_code_generation():
    """Test: ask Copilot to generate a small Python snippet."""
    log("Test 2: Code generation — write hello world in Python")
    ok, response = run_copilot(
        'Write a Python function called hello_world() that prints "Hello, World!". '
        "Return only the function code, no explanation."
    )
    if ok and "def hello_world" in response:
        log(f"  PASS — response preview: {response[:150]}")
        return True
    log(f"  FAIL — ok={ok}, response: {response[:300]}")
    return False


def test_model_selection():
    """Test: explicitly set model to gpt-5-mini."""
    log(f"Test 3: Model selection — using model '{MODEL}'")
    ok, response = run_copilot("Reply with only the word: READY", model=MODEL)
    if ok and response:
        log(f"  PASS — response: {response[:100]}")
        return True
    log(f"  FAIL — ok={ok}, response: {response[:300]}")
    return False


def check_copilot_version():
    """Print the installed Copilot CLI version."""
    try:
        result = subprocess.run(
            ["copilot", "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        version = (result.stdout + result.stderr).strip()
        log(f"Copilot CLI version: {version}")
    except Exception as exc:
        log(f"Could not get copilot version: {exc}")


def main():
    log("=== GitHub Copilot CLI Container Test ===")

    if not COPILOT_TOKEN:
        log("ERROR: COPILOT_GITHUB_TOKEN is not set.")
        log("  Set it with: docker run -e COPILOT_GITHUB_TOKEN=<your-pat> ...")
        sys.exit(1)

    log(f"Token prefix: {COPILOT_TOKEN[:12]}...")
    log(f"Model: {MODEL}")
    check_copilot_version()

    results = {}
    tests = [
        ("basic_question", test_basic_question),
        ("code_generation", test_code_generation),
        ("model_selection", test_model_selection),
    ]

    for name, fn in tests:
        try:
            results[name] = fn()
        except Exception as exc:
            log(f"  ERROR in {name}: {exc}")
            results[name] = False

    log("")
    log("=== Results ===")
    passed = sum(1 for v in results.values() if v)
    total = len(results)
    for name, ok in results.items():
        status = "PASS" if ok else "FAIL"
        log(f"  {status}  {name}")
    log(f"Passed: {passed}/{total}")

    if passed == total:
        log("All tests passed! Copilot CLI is working correctly in the container.")
        sys.exit(0)
    else:
        log("Some tests failed.")
        sys.exit(1)


if __name__ == "__main__":
    main()
