#!/usr/bin/env python3
"""Dedicated Bitbucket agent integration test against a real repo."""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import time
from urllib.parse import urlencode

from agent_test_support import (
    PROJECT_ROOT,
    Reporter,
    agent_url_from_args,
    choose_base_branch,
    http_request,
    load_env_file,
    run_command,
    summary_exit_code,
    unique_suffix,
)
from agent_test_targets import (
    assert_bitbucket_write_allowed,
    bitbucket_clone_url,
    bitbucket_pr_url,
    bitbucket_project_key,
    bitbucket_repo_browse_url,
    bitbucket_repo_slug,
    bitbucket_search_query,
    bitbucket_write_root,
    jira_ticket_key,
)

BB_PROJECT = bitbucket_project_key()
BB_REPO = bitbucket_repo_slug()
SEARCH_QUERY = bitbucket_search_query()
DEFAULT_LOCAL_AGENT_PORT = 18020
CONTAINER_AGENT_URL = "http://127.0.0.1:8020"
LINKED_JIRA_KEY = jira_ticket_key()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-url", default="")
    parser.add_argument("--container", action="store_true")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def wait_for_agent(agent_url: str, timeout: int = 15) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _, _ = http_request(f"{agent_url}/health")
        if status == 200:
            return True
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
    venv_python = os.path.join(PROJECT_ROOT, "venv", "bin", "python")
    if not os.path.isfile(venv_python):
        return None

    port = str(agent_url.rsplit(":", 1)[1])
    env = os.environ.copy()
    env.update(
        {
            "HOST": "127.0.0.1",
            "PORT": port,
            "AGENT_ID": "bitbucket-agent",
            "ADVERTISED_BASE_URL": agent_url,
            "REGISTRY_URL": "http://127.0.0.1:9000",
            "INSTANCE_REPORTER_ENABLED": "0",
            "BITBUCKET_BASE_URL": env_values.get(
                "BITBUCKET_BASE_URL", "https://bitbucket.example.com/projects/MYPROJECT"
            ),
            "BITBUCKET_API_BASE_URL": env_values.get("BITBUCKET_API_BASE_URL", ""),
            "BITBUCKET_REST_API": env_values.get(
                "BITBUCKET_REST_API", "https://bitbucket.example.com/rest/api/1.0"
            ),
            "BITBUCKET_TOKEN": env_values.get("BITBUCKET_TOKEN", ""),
            "BITBUCKET_USERNAME": env_values.get("BITBUCKET_USERNAME", ""),
            "BITBUCKET_AUTH_MODE": env_values.get("BITBUCKET_AUTH_MODE", "auto"),
            "BITBUCKET_GIT_AUTHOR_NAME": env_values.get("BITBUCKET_GIT_AUTHOR_NAME", "Bitbucket Agent"),
            "BITBUCKET_GIT_AUTHOR_EMAIL": env_values.get(
                "BITBUCKET_GIT_AUTHOR_EMAIL", env_values.get("BITBUCKET_USERNAME", "bitbucket-agent@local")
            ),
            "CORP_CA_BUNDLE": ca_bundle,
            "ALLOW_MOCK_FALLBACK": "1",
            "PYTHONPATH": PROJECT_ROOT,
        }
    )
    return subprocess.Popen(
        [venv_python, "bitbucket/app.py"],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def main(argv=None):
    args = parse_args(argv)
    reporter = Reporter(verbose=args.verbose)
    requested_agent_url = agent_url_from_args(
        args,
        local_default=f"http://127.0.0.1:{DEFAULT_LOCAL_AGENT_PORT}",
        container_default=CONTAINER_AGENT_URL,
    )
    env_values = load_env_file("bitbucket/.env")
    token = env_values.get("BITBUCKET_TOKEN", "")
    ca_bundle = os.path.join(PROJECT_ROOT, "certs", "slf-ca-bundle.crt")
    clone_url = bitbucket_clone_url()
    agent_url = requested_agent_url.rstrip("/")
    proc = None

    reporter.section(f"Bitbucket Agent Integration — {BB_PROJECT}/{BB_REPO}")
    if not token:
        reporter.fail("BITBUCKET_TOKEN is missing in bitbucket/.env")
        return summary_exit_code(reporter)

    if not args.agent_url and not args.container:
        local_port = _pick_free_local_port()
        agent_url = f"http://127.0.0.1:{local_port}"
        reporter.section("Starting local Bitbucket agent subprocess")
        proc = start_local_agent(env_values, ca_bundle, agent_url)
        if not proc:
            reporter.fail("Could not start local agent", "Missing venv Python or subprocess launch failed")
            return summary_exit_code(reporter)
        reporter.info(f"Agent PID {proc.pid} — waiting for /health on {agent_url} ...")
        if wait_for_agent(agent_url):
            reporter.ok("Local Bitbucket agent became healthy")
        else:
            reporter.fail("Local agent did not become healthy in time")
            proc.terminate()
            return summary_exit_code(reporter)

    try:
        reporter.step("Validate direct Git auth using the token from bitbucket/.env")
        git_args = [
            "git",
            "-c",
            f"http.extraHeader=Authorization: Bearer {token}",
        ]
        if os.path.isfile(ca_bundle):
            git_args.extend(["-c", f"http.sslCAInfo={ca_bundle}"])
        git_args.extend(["ls-remote", clone_url, "HEAD"])
        code, stdout, stderr = run_command(
            git_args,
            cwd=PROJECT_ROOT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        if code == 0 and stdout:
            reporter.ok("Bitbucket token works for Git over HTTPS")
        else:
            reporter.fail("Bitbucket token cannot access Git over HTTPS", stderr or stdout)
            return summary_exit_code(reporter)

        reporter.step("Check Bitbucket agent health")
        status, body, _ = http_request(f"{agent_url}/health")
        if status == 200:
            reporter.ok("Bitbucket agent is healthy")
        else:
            reporter.fail("Bitbucket agent health check failed", f"status={status}, body={body}")
            return summary_exit_code(reporter)

        reporter.step("Read Bitbucket agent card")
        status, body, _ = http_request(f"{agent_url}/.well-known/agent-card.json")
        if status == 200 and body.get("name") == "Bitbucket Agent":
            reporter.ok("Bitbucket agent card is available")
        else:
            reporter.fail("Bitbucket agent card is missing or malformed", f"status={status}, body={body}")
            return summary_exit_code(reporter)

        reporter.step("Resolve the allowed repo from its browse URL")
        query = urlencode({"q": bitbucket_repo_browse_url(), "project": BB_PROJECT})
        status, body, _ = http_request(f"{agent_url}/bitbucket/repo-url?{query}")
        reporter.show("Repo URL resolve", body)
        if status == 200 and body.get("slug") == BB_REPO and body.get("browseUrl") == bitbucket_repo_browse_url():
            reporter.ok("Repo URL resolution returned the allowed repo")
        else:
            reporter.fail("Repo URL resolution returned an unexpected repo", f"status={status}, body={body}")
            return summary_exit_code(reporter)

        reporter.step("List repos in the allowed project")
        query = urlencode({"project": BB_PROJECT})
        status, body, _ = http_request(f"{agent_url}/bitbucket/repos?{query}")
        reporter.show("Repo list", body)
        repos = body.get("repos", []) if isinstance(body, dict) else []
        matched_repo = next((item for item in repos if isinstance(item, dict) and item.get("slug") == BB_REPO), {})
        if status == 200 and body.get("result") == "ok" and matched_repo:
            reporter.ok("Repo list contains the allowed repo")
        else:
            reporter.fail("Repo list did not contain the allowed repo", f"status={status}, body={body}")
            return summary_exit_code(reporter)

        reporter.step(f"Search repo with query '{SEARCH_QUERY}' in project {BB_PROJECT}")
        query = urlencode({"q": SEARCH_QUERY, "project": BB_PROJECT, "limit": 5})
        status, body, _ = http_request(f"{agent_url}/bitbucket/search/repos?{query}")
        reporter.show("Repo search", body)
        repos = body.get("repos", []) if isinstance(body, dict) else []
        top_slug = repos[0].get("slug") if repos else ""
        if status == 200 and top_slug == BB_REPO:
            reporter.ok("Repo search matched the allowed repo")
        else:
            reporter.fail("Repo search returned an unexpected repo", f"status={status}, topSlug={top_slug}, body={body}")
            return summary_exit_code(reporter)

        reporter.step("List branches to determine the base branch")
        query = urlencode({"project": BB_PROJECT, "repo": BB_REPO})
        status, body, _ = http_request(f"{agent_url}/bitbucket/branches?{query}")
        reporter.show("Branches", body)
        branches = body.get("branches", []) if isinstance(body, dict) else []
        branch_names = [item.get("displayId") for item in branches if isinstance(item, dict) and item.get("displayId")]
        base_branch = choose_base_branch(branch_names)
        if status == 200 and base_branch:
            reporter.info(f"Base branch: {base_branch}")
            reporter.ok("Branch list retrieved")
        else:
            reporter.fail("Could not determine a base branch", f"status={status}, body={body}")
            return summary_exit_code(reporter)

        suffix = unique_suffix()
        feature_branch = f"agent/test/{suffix}"
        file_path = f"{bitbucket_write_root()}{suffix}/bitbucket-agent.txt"
        comment_text = "[Agent Test] Inline comment on the generated test file."
        assert_bitbucket_write_allowed(BB_PROJECT, BB_REPO, file_path)

        reporter.step(f"Create remote branch {feature_branch}")
        status, body, _ = http_request(
            f"{agent_url}/bitbucket/branches",
            method="POST",
            payload={
                "project": BB_PROJECT,
                "repo": BB_REPO,
                "branch": feature_branch,
                "startPoint": base_branch,
            },
        )
        reporter.show("Create branch", body)
        if status == 201 and body.get("result") == "created":
            reporter.ok("Remote branch created")
        else:
            reporter.fail("Failed to create remote branch", f"status={status}, body={body}")
            return summary_exit_code(reporter)

        reporter.step("Push a real commit to the new branch")
        content = "\n".join(
            [
                "Bitbucket agent integration test file.",
                "This second line is the inline comment anchor.",
                "This third line confirms a real diff exists.",
                "",
            ]
        )
        status, body, _ = http_request(
            f"{agent_url}/bitbucket/git/push",
            method="POST",
            payload={
                "project": BB_PROJECT,
                "repo": BB_REPO,
                "branch": feature_branch,
                "baseBranch": base_branch,
                "commitMessage": f"[Agent Test] Add {file_path}",
                "files": [{"path": file_path, "content": content}],
            },
            timeout=180,
        )
        reporter.show("Git push", body)
        push_detail = body.get("detail", {}) if isinstance(body, dict) else {}
        commit_id = push_detail.get("commitId") if isinstance(push_detail, dict) else ""
        if status == 201 and body.get("result") == "pushed" and commit_id:
            reporter.info(f"Commit: {commit_id}")
            reporter.ok("Real commit pushed to remote")
        else:
            reporter.fail("Failed to push a real commit", f"status={status}, body={body}")
            return summary_exit_code(reporter)

        reporter.step("Create a real pull request")
        pr_title = f"{LINKED_JIRA_KEY}: [Agent Test] {feature_branch} -> {base_branch}"
        status, body, _ = http_request(
            f"{agent_url}/bitbucket/pull-requests",
            method="POST",
            payload={
                "project": BB_PROJECT,
                "repo": BB_REPO,
                "fromBranch": feature_branch,
                "toBranch": base_branch,
                "title": pr_title,
                "description": (
                    "This PR was created by the dedicated Bitbucket agent integration test. "
                    f"It contains a single agent test file under {bitbucket_write_root()} and links {LINKED_JIRA_KEY}."
                ),
            },
            timeout=120,
        )
        reporter.show("Create PR", body)
        pr_detail = body.get("detail", {}) if isinstance(body, dict) else {}
        pr_id = pr_detail.get("id") if isinstance(pr_detail, dict) else None
        pr_url = body.get("prUrl")
        if status == 201 and body.get("result") == "created" and pr_id:
            reporter.info(f"PR: {pr_url}")
            reporter.ok("Real pull request created")
        else:
            reporter.fail("Failed to create a real PR", f"status={status}, body={body}")
            return summary_exit_code(reporter)

        reporter.step("Parse the created PR URL via agent")
        query = urlencode({"url": pr_url or bitbucket_pr_url(pr_id or 0)})
        status, body, _ = http_request(f"{agent_url}/bitbucket/pull-requests/parse?{query}")
        reporter.show("Parse PR URL", body)
        parsed_pr = body.get("pullRequest", {}) if isinstance(body, dict) else {}
        if status == 200 and body.get("result") == "ok" and parsed_pr.get("project") == BB_PROJECT and parsed_pr.get("repo") == BB_REPO and parsed_pr.get("prId") == pr_id:
            reporter.ok("PR URL parsing returned the expected project, repo, and PR id")
        else:
            reporter.fail("PR URL parsing failed", f"status={status}, body={body}")
            return summary_exit_code(reporter)

        reporter.step("Fetch the created pull request via agent")
        query = urlencode({"project": BB_PROJECT, "repo": BB_REPO})
        status, body, _ = http_request(
            f"{agent_url}/bitbucket/pull-requests/{pr_id}?{query}",
            timeout=60,
        )
        reporter.show("Get PR", body)
        fetched_pr = body.get("pullRequest", {}) if isinstance(body, dict) else {}
        linked_keys = fetched_pr.get("linkedJiraIssues", []) if isinstance(fetched_pr, dict) else []
        if status == 200 and body.get("result") == "ok" and fetched_pr.get("id") == pr_id and LINKED_JIRA_KEY in linked_keys:
            reporter.ok("PR detail lookup returned linked Jira issues")
        else:
            reporter.fail("Failed to fetch PR details", f"status={status}, body={body}")

        reporter.step("List open pull requests via agent")
        query = urlencode({"project": BB_PROJECT, "repo": BB_REPO, "state": "OPEN", "limit": 25})
        status, body, _ = http_request(
            f"{agent_url}/bitbucket/pull-requests?{query}",
            timeout=60,
        )
        reporter.show("List PRs", body)
        pull_requests = body.get("pullRequests", []) if isinstance(body, dict) else []
        matched_pr = next((item for item in pull_requests if isinstance(item, dict) and item.get("id") == pr_id), {})
        if status == 200 and body.get("result") == "ok" and matched_pr and LINKED_JIRA_KEY in matched_pr.get("linkedJiraIssues", []):
            reporter.ok("PR list returned the created PR with linked Jira issues")
        else:
            reporter.fail("Failed to list PRs", f"status={status}, body={body}")

        reporter.step("Exercise the merge route safely with a stale version")
        status, body, _ = http_request(
            f"{agent_url}/bitbucket/pull-requests/{pr_id}/merge",
            method="POST",
            payload={
                "project": BB_PROJECT,
                "repo": BB_REPO,
                "version": -1,
            },
            timeout=60,
        )
        reporter.show("Merge stale version", body)
        if status == 502 and str(body.get("result", "")).startswith("merge_failed_"):
            reporter.ok("Merge route rejected a stale version without merging the PR")
        else:
            reporter.fail("Merge stale-version safety check failed", f"status={status}, body={body}")

        reporter.step("Add an inline comment to the changed file")
        status, body, _ = http_request(
            f"{agent_url}/bitbucket/pull-requests/comments",
            method="POST",
            payload={
                "project": BB_PROJECT,
                "repo": BB_REPO,
                "prId": pr_id,
                "text": comment_text,
                "filePath": file_path,
                "line": 2,
            },
            timeout=120,
        )
        reporter.show("PR comment", body)
        if status == 201 and body.get("result") == "created_inline":
            reporter.ok("Inline PR comment created")
        else:
            reporter.fail("Failed to create an inline PR comment", f"status={status}, body={body}")
            return summary_exit_code(reporter)

        reporter.step("List PR comments via agent")
        query = urlencode({"project": BB_PROJECT, "repo": BB_REPO})
        status, body, _ = http_request(
            f"{agent_url}/bitbucket/pull-requests/{pr_id}/comments?{query}",
            timeout=60,
        )
        reporter.show("PR comments", body)
        comments = body.get("comments", []) if isinstance(body, dict) else []
        matched_comment = next((item for item in comments if isinstance(item, dict) and item.get("text") == comment_text), {})
        if status == 200 and body.get("result") == "ok" and matched_comment:
            reporter.ok("PR comment listing returned the created comment")
        else:
            reporter.fail("Failed to list PR comments", f"status={status}, body={body}")

        reporter.step("Check duplicate inline comments via agent")
        status, body, _ = http_request(
            f"{agent_url}/bitbucket/pull-requests/comments/check-duplicates",
            method="POST",
            payload={
                "project": BB_PROJECT,
                "repo": BB_REPO,
                "prId": pr_id,
                "text": comment_text,
                "filePath": file_path,
                "line": 2,
            },
            timeout=60,
        )
        reporter.show("Duplicate comment check", body)
        if status == 200 and body.get("result") == "ok" and body.get("duplicate") is True:
            reporter.ok("Duplicate comment detection matched the existing inline comment")
        else:
            reporter.fail("Duplicate comment detection failed", f"status={status}, body={body}")

        reporter.step("Exercise the message interface")
        status, body, _ = http_request(
            f"{agent_url}/message:send",
            method="POST",
            payload={
                "message": {
                    "messageId": "bitbucket-agent-test",
                    "role": "ROLE_USER",
                    "parts": [
                        {
                            "text": (
                                "Inspect this Bitbucket repo and summarize it for downstream engineers: "
                                f"{bitbucket_repo_browse_url()}"
                            )
                        }
                    ],
                }
            },
        )
        reporter.show("Message send", body)
        task = body.get("task", {}) if isinstance(body, dict) else {}
        state = task.get("status", {}).get("state")
        if status == 200 and state == "TASK_STATE_COMPLETED":
            reporter.ok("Bitbucket message flow completed")
        else:
            reporter.fail("Bitbucket message flow failed", f"status={status}, body={body}")

        return summary_exit_code(reporter)
    finally:
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())