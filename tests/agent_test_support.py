#!/usr/bin/env python3
"""Shared helpers for agent integration scripts."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import os
import ssl
import subprocess
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

PROJECT_ROOT = os.path.dirname(os.path.dirname(__file__))


class Colors:
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


@dataclass
class Reporter:
    verbose: bool = False
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    def section(self, title):
        print(f"\n{Colors.BOLD}{'═' * 60}{Colors.RESET}")
        print(f"{Colors.BOLD}  {title}{Colors.RESET}")
        print(f"{Colors.BOLD}{'═' * 60}{Colors.RESET}")

    def step(self, desc):
        print(f"\n  {Colors.CYAN}→{Colors.RESET} {desc}")

    def ok(self, msg):
        self.passed += 1
        print(f"  {Colors.GREEN}✅ PASS{Colors.RESET} — {msg}")

    def fail(self, msg, detail=""):
        self.failed += 1
        self.errors.append(msg)
        print(f"  {Colors.RED}❌ FAIL{Colors.RESET} — {msg}")
        if detail:
            print(f"         {detail}")

    def info(self, msg):
        print(f"  {Colors.YELLOW}ℹ{Colors.RESET}  {msg}")

    def skip(self, msg, reason=""):
        self.skipped += 1
        print(f"  {Colors.YELLOW}⏭ SKIP{Colors.RESET} — {msg}" + (f" — {reason}" if reason else ""))

    def show(self, label, data):
        if self.verbose:
            formatted = json.dumps(data, ensure_ascii=False, indent=2)
            print(f"     {label}:")
            for line in formatted.splitlines():
                print(f"       {line}")


def load_env_file(relative_path):
    path = os.path.join(PROJECT_ROOT, relative_path)
    values = {}
    if not os.path.isfile(path):
        return values
    with open(path, encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


def ssl_context(ca_bundle=None):
    ctx = ssl.create_default_context()
    if ca_bundle and os.path.isfile(ca_bundle):
        ctx.load_verify_locations(ca_bundle)
    return ctx


def http_request(url, method="GET", payload=None, headers=None, timeout=60, ca_bundle=None):
    data = None
    request_headers = dict(headers or {})
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json; charset=utf-8")
    request = Request(url, data=data, headers=request_headers, method=method)
    try:
        with urlopen(request, timeout=timeout, context=ssl_context(ca_bundle)) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                body = {"raw": raw[:1000]}
            return response.status, body, raw
    except HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            body = json.loads(raw) if raw.strip() else {}
        except json.JSONDecodeError:
            body = {"raw": raw[:1000]}
        return error.code, body, raw
    except (URLError, OSError) as error:
        return 0, {"error": str(error)}, str(error)


def run_command(args, cwd=None, env=None, timeout=120):
    completed = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return completed.returncode, completed.stdout.strip(), completed.stderr.strip()


def agent_url_from_args(args, *, local_default, container_default):
    if getattr(args, "agent_url", ""):
        return args.agent_url.rstrip("/")
    if getattr(args, "container", False):
        return container_default.rstrip("/")
    return local_default.rstrip("/")


def choose_base_branch(branch_names):
    for candidate in ("develop", "main", "master"):
        if candidate in branch_names:
            return candidate
    return branch_names[0] if branch_names else "develop"


def unique_suffix():
    return time.strftime("%Y%m%d%H%M%S", time.gmtime())


def summary_exit_code(reporter):
    print(f"\nPassed: {reporter.passed}")
    print(f"Failed: {reporter.failed}")
    return 0 if reporter.failed == 0 else 1