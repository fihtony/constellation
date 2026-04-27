#!/usr/bin/env python3
"""GitHub REST API provider tests — direct GitHubProvider class + SCM agent A2A.

Tests the GitHubProvider class (scm/providers/github.py) directly, covering every
SCM capability required by the Constellation system.  Also validates the same
operations through the SCM agent A2A interface when --agent-url is provided.

Test Cases
----------
TC-01  Token auth        Verify GitHub token resolves to a user.
TC-02  Repo inspect      get_repo() returns expected metadata.
TC-03  Branch list       list_branches() returns at least one branch.
TC-04  Branch create     create_branch() creates a feature branch.
TC-05  Push files        push_files() commits files to the branch.
TC-06  PR create         create_pr() opens a pull request.
TC-07  PR get            get_pr() fetches the created PR.
TC-08  PR list           list_prs() includes the created PR.
TC-09  PR comment        add_pr_comment() posts a general comment.
TC-10  PR comment list   list_pr_comments() returns the posted comment.
TC-11  Repo search       search_repos() returns results for a query.
TC-12  A2A health        GET /health via SCM agent (requires --agent-url).

Run
---
  python3 tests/test_github_rest.py              # provider-direct tests only
  python3 tests/test_github_rest.py -v           # verbose
  python3 tests/test_github_rest.py --agent-url http://localhost:8020
"""

from __future__ import annotations

import argparse
import os
import sys
import time

_HERE = os.path.dirname(__file__)
_PROJECT_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from agent_test_support import (
    Reporter,
    http_request,
    load_env_file,
    unique_suffix,
)
from agent_test_targets import scm_owner, scm_repo_slug


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _load_token() -> str:
    tests_env = load_env_file("tests/.env")
    token = tests_env.get("TEST_GITHUB_TOKEN", "").strip()
    if not token:
        raise SystemExit("ERROR: TEST_GITHUB_TOKEN not set in tests/.env — cannot run tests")
    return token


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent-url", default="", help="Optional SCM agent A2A base URL")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv=None):
    args = parse_args(argv)
    reporter = Reporter(verbose=args.verbose)

    owner = scm_owner()
    repo = scm_repo_slug()
    token = _load_token()

    reporter.section(f"GitHub REST Provider — {owner}/{repo}")

    if not token:
        reporter.fail("GitHub token missing — set TEST_GITHUB_TOKEN in tests/.env")
        return _exit(reporter)
    if not owner or not repo:
        reporter.fail("Repo target missing — set TEST_GITHUB_REPO_URL in tests/.env")
        return _exit(reporter)

    # Import provider here (after path setup)
    from scm.providers.github import GitHubProvider
    p = GitHubProvider(token=token)

    suffix = unique_suffix()
    branch = f"agent/rest-test/{suffix}"
    pr_id = None
    pr_url = ""

    # TC-01  Token auth -------------------------------------------------------
    reporter.step("TC-01  Verify GitHub token identity")
    from urllib.request import Request, urlopen
    from urllib.error import HTTPError
    import json
    req = Request("https://api.github.com/user",
                  headers={"Authorization": f"Bearer {token}",
                           "Accept": "application/vnd.github+json"})
    try:
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        login = data.get("login", "")
        if login:
            reporter.ok(f"Token authenticated as: {login}")
        else:
            reporter.fail("Token returned no login field", str(data)[:100])
    except HTTPError as exc:
        reporter.fail(f"GitHub auth HTTP {exc.code}", exc.read().decode()[:100])

    # TC-02  Repo inspect -----------------------------------------------------
    reporter.step("TC-02  get_repo() metadata")
    repo_info, status = p.get_repo(owner, repo)
    if status == "ok" and repo_info.get("fullName"):
        default_branch = repo_info.get("defaultBranch", "main")
        reporter.ok(f"Repo: {repo_info['fullName']} | default branch: {default_branch}")
    else:
        reporter.fail(f"get_repo() returned status={status!r}", str(repo_info)[:100])
        return _exit(reporter)  # nothing else will work without repo info

    # TC-03  Branch list ------------------------------------------------------
    reporter.step("TC-03  list_branches()")
    branches, status = p.list_branches(owner, repo)
    if status == "ok" and branches:
        names = [b["name"] for b in branches]
        reporter.ok(f"Branches: {len(branches)} — {names[:5]}")
    else:
        reporter.fail(f"list_branches() status={status!r}", str(branches)[:100])

    # TC-04  Branch create ----------------------------------------------------
    reporter.step(f"TC-04  create_branch() → {branch}")
    result, status = p.create_branch(owner, repo, branch, default_branch)
    if "created" in status or status == "ok":
        reporter.ok(f"Branch created: {result.get('htmlUrl', branch)}")
    else:
        reporter.fail(f"create_branch() status={status!r}", str(result)[:150])
        return _exit(reporter)

    # TC-05  Push files -------------------------------------------------------
    reporter.step("TC-05  push_files() — commit test file")
    files = [{"path": f"agent-tests/{suffix}/rest-provider.txt",
               "content": f"REST provider test — {suffix}\nTimestamp: {time.strftime('%Y-%m-%dT%H:%M:%SZ')}"}]
    result, status = p.push_files(owner, repo, branch, default_branch, files,
                                  f"chore: REST provider test {suffix}")
    if status == "pushed":
        reporter.ok(f"Files pushed: {result.get('htmlUrl', '')}")
    else:
        reporter.fail(f"push_files() status={status!r}", str(result)[:150])
        return _exit(reporter)

    # TC-06  PR create --------------------------------------------------------
    reporter.step("TC-06  create_pr()")
    pr, status = p.create_pr(owner, repo, branch, default_branch,
                              f"[REST Test] {suffix}",
                              "Automated REST provider test PR — safe to close.")
    if "created" in status and pr.get("id"):
        pr_id = pr["id"]
        pr_url = pr.get("htmlUrl", "")
        reporter.ok(f"PR #{pr_id}: {pr_url}")
    else:
        reporter.fail(f"create_pr() status={status!r}", str(pr)[:150])
        return _exit(reporter)

    # TC-07  PR get -----------------------------------------------------------
    reporter.step(f"TC-07  get_pr({pr_id})")
    pr_fetched, status = p.get_pr(owner, repo, pr_id)
    if status == "ok" and pr_fetched.get("id") == pr_id:
        reporter.ok(f"PR #{pr_id} fetched — state={pr_fetched.get('state')}, "
                    f"from={pr_fetched.get('fromBranch')}")
    else:
        reporter.fail(f"get_pr() status={status!r}", str(pr_fetched)[:100])

    # TC-08  PR list ----------------------------------------------------------
    reporter.step("TC-08  list_prs() includes new PR")
    prs, status = p.list_prs(owner, repo, "open")
    ids = [x["id"] for x in prs]
    if status == "ok" and pr_id in ids:
        reporter.ok(f"list_prs(): {len(prs)} open PRs — #{pr_id} found")
    else:
        reporter.fail(f"list_prs() did not include #{pr_id}", f"status={status!r} ids={ids}")

    # TC-09  PR comment -------------------------------------------------------
    reporter.step("TC-09  add_pr_comment()")
    comment_body = f"[REST Test] Automated comment from test_github_rest.py — {suffix}"
    comment, status = p.add_pr_comment(owner, repo, pr_id, comment_body)
    if "created" in status and comment.get("id"):
        reporter.ok(f"Comment #{comment['id']}: {comment.get('htmlUrl','')}")
    else:
        reporter.fail(f"add_pr_comment() status={status!r}", str(comment)[:100])

    # TC-10  PR comment list --------------------------------------------------
    reporter.step("TC-10  list_pr_comments()")
    comments, status = p.list_pr_comments(owner, repo, pr_id)
    bodies = [c.get("body", "") for c in comments]
    if status == "ok" and any(suffix in b for b in bodies):
        reporter.ok(f"list_pr_comments(): {len(comments)} comment(s) — test comment found")
    else:
        reporter.fail(f"list_pr_comments() did not find test comment",
                      f"status={status!r} bodies={bodies[:2]}")

    # TC-11  Repo search ------------------------------------------------------
    reporter.step("TC-11  search_repos()")
    results, status = p.search_repos(f"repo:{owner}/{repo}", limit=5)
    if status == "ok" and any(r.get("repo") == repo for r in results):
        reporter.ok(f"search_repos(): found {owner}/{repo}")
    else:
        reporter.fail(f"search_repos() did not find {owner}/{repo}",
                      f"status={status!r} results={[r.get('repo') for r in results]}")

    # TC-12  A2A health (optional) -------------------------------------------
    if args.agent_url:
        reporter.section("SCM Agent A2A — health check")
        reporter.step("TC-12  GET /health")
        status_code, body, _ = http_request(f"{args.agent_url.rstrip('/')}/health")
        if status_code == 200 and body.get("status") == "ok":
            reporter.ok(f"Health OK — provider={body.get('provider','?')}")
        else:
            reporter.fail(f"/health returned HTTP {status_code}", str(body)[:100])
    else:
        reporter.info("TC-12  A2A health skipped (no --agent-url)")

    return _exit(reporter)


def _exit(reporter: Reporter) -> int:
    print(f"\nPassed: {reporter.passed}  Failed: {reporter.failed}  Skipped: {reporter.skipped}")
    return 0 if reporter.failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
