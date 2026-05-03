"""Local Android agent runner for direct Jira, Bitbucket, and Figma access."""

from __future__ import annotations

import argparse
import json
import os

os.environ.setdefault("OPENAI_BASE_URL", "http://localhost:1288/v1")
os.environ.setdefault("ALLOW_MOCK_FALLBACK", "0")

from android.workflow import load_test_targets, run_android_workflow


def parse_args(argv=None):
    defaults = load_test_targets()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "task",
        nargs="?",
        default="",
        help="Natural-language request for the Android agent. Defaults to the shared test target ticket and repo.",
    )
    parser.add_argument("--ticket", default=defaults["ticketKey"], help="Jira ticket key")
    parser.add_argument("--ticket-url", default=defaults["ticketUrl"], help="Jira browse URL")
    parser.add_argument("--repo-url", default=defaults["repoUrl"], help="Bitbucket browse URL")
    parser.add_argument("--workspace", default="", help="Workspace directory for repo clone and outputs")
    parser.add_argument("--branch", default="", help="Base branch to clone and target for the PR")
    parser.add_argument("--json", action="store_true", help="Print the full workflow result as JSON")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    task_text = args.task or f"Implement {args.ticket} using {args.repo_url}".strip()

    print(f"[android-agent] LLM endpoint: {os.environ.get('OPENAI_BASE_URL')}")
    print(f"[android-agent] Ticket: {args.ticket or '(from defaults)'}")
    print(f"[android-agent] Repo URL: {args.repo_url or '(from defaults)'}")

    result = run_android_workflow(
        task_text,
        ticket_key=args.ticket,
        ticket_url=args.ticket_url,
        repo_url=args.repo_url,
        workspace_path=args.workspace,
        base_branch=args.branch,
    )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(result.get("summary", ""))
        if result.get("workspacePath"):
            print(f"\nWorkspace: {result['workspacePath']}")
        if result.get("prUrl"):
            print(f"PR: {result['prUrl']}")

    return 0 if result.get("status") == "completed" else 1


if __name__ == "__main__":
    raise SystemExit(main())