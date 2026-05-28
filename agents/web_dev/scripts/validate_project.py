#!/usr/bin/env python3
"""Mandatory validation runner for Web Dev tasks.

The Web Dev workflow calls this script after implementation and after every
repair cycle. It runs dependency installation, build, and tests in order and
returns a machine-readable JSON summary. The script never reads credentials
from URLs or host keychains; child Git commands are isolated through Git env
overrides so dependency installation cannot silently use local credential
helpers.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time


MAX_OUTPUT_CHARS = 20000


def _command_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "/bin/false",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "credential.helper",
        "GIT_CONFIG_VALUE_0": "",
    })
    return env


def _run(command: list[str], cwd: Path, timeout: int) -> dict:
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            env=_command_env(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        output = proc.stdout or ""
        return {
            "command": command,
            "returncode": proc.returncode,
            "duration_seconds": round(time.time() - started, 3),
            "output": output[-MAX_OUTPUT_CHARS:],
        }
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return {
            "command": command,
            "returncode": 124,
            "duration_seconds": round(time.time() - started, 3),
            "output": (output + "\nCommand timed out.")[-MAX_OUTPUT_CHARS:],
        }


def _load_package_json(repo_path: Path) -> dict:
    package_json = repo_path / "package.json"
    if not package_json.is_file():
        raise RuntimeError("package.json is required for Web Dev validation")
    with package_json.open(encoding="utf-8") as fh:
        return json.load(fh)


def _has_dependency(package: dict, name: str) -> bool:
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        deps = package.get(section) or {}
        if isinstance(deps, dict) and name in deps:
            return True
    return False


def _test_command(package: dict) -> list[str]:
    scripts = package.get("scripts") or {}
    test_script = str(scripts.get("test") or "")
    if "vitest" in test_script or _has_dependency(package, "vitest"):
        if test_script:
            if re.search(r"(^|\s)--run(\s|$)", test_script):
                return ["npm", "test"]
            return ["npm", "test", "--", "--run"]
        return ["npx", "vitest", "--run"]
    if "jest" in test_script or _has_dependency(package, "jest"):
        if test_script and re.search(r"(^|\s)--runInBand(\s|$)", test_script):
            return ["npm", "test"]
        return ["npm", "test", "--", "--runInBand"] if test_script else ["npx", "jest", "--runInBand"]
    if test_script:
        return ["npm", "test"]
    raise RuntimeError("package.json must define a test script or include a supported test runner")


def _parse_test_counts(output: str, tests_ok: bool) -> tuple[int, int]:
    passed = 0
    failed = 0
    for match in re.finditer(r"(\d+)\s+passed", output, re.IGNORECASE):
        passed = max(passed, int(match.group(1)))
    for match in re.finditer(r"(\d+)\s+failed", output, re.IGNORECASE):
        failed = max(failed, int(match.group(1)))
    if tests_ok and passed == 0:
        passed = 1
    if not tests_ok and failed == 0:
        failed = 1
    return passed, failed


def validate(repo_path: Path) -> dict:
    repo_path = repo_path.resolve()
    package = _load_package_json(repo_path)
    scripts = package.get("scripts") or {}
    if "build" not in scripts:
        raise RuntimeError("package.json must define a build script")

    commands: list[dict] = []
    errors: list[str] = []

    install = _run(["npm", "install"], repo_path, timeout=900)
    commands.append(install)
    install_ok = install["returncode"] == 0
    if not install_ok:
        errors.append("npm install failed")

    build_ok = False
    if install_ok:
        build = _run(["npm", "run", "build"], repo_path, timeout=600)
        commands.append(build)
        build_ok = build["returncode"] == 0
        if not build_ok:
            errors.append("npm run build failed")

    tests_ok = False
    if install_ok and build_ok:
        test = _run(_test_command(package), repo_path, timeout=900)
        commands.append(test)
        tests_ok = test["returncode"] == 0
        if not tests_ok:
            errors.append("test command failed")

    combined_output = "\n".join(str(command.get("output") or "") for command in commands)
    passed, failed = _parse_test_counts(combined_output, tests_ok)
    if not install_ok or not build_ok:
        failed = max(failed, 1)

    return {
        "install_ok": install_ok,
        "build_ok": build_ok,
        "test_ok": tests_ok,
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "commands": commands,
        "output": combined_output[-MAX_OUTPUT_CHARS:],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Run mandatory Web Dev validation")
    parser.add_argument("repo_path", help="Repository root to validate")
    parser.add_argument("--output", help="Optional JSON output file")
    args = parser.parse_args()

    try:
        summary = validate(Path(args.repo_path))
    except Exception as exc:  # noqa: BLE001
        summary = {
            "install_ok": False,
            "build_ok": False,
            "test_ok": False,
            "passed": 0,
            "failed": 1,
            "errors": [str(exc)],
            "commands": [],
            "output": str(exc),
        }

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)

    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary.get("failed", 1) == 0 and summary.get("build_ok") and summary.get("test_ok") else 1


if __name__ == "__main__":
    sys.exit(main())