"""Local Android workflow runner built on direct Jira, Bitbucket, and Figma clients."""

from __future__ import annotations

import json
import os
import re
import time

from android import prompts as _prompts
from bitbucket import BitbucketClient, extract_repo_url, parse_repo_target
from common.devlog import debug_log
from common.env_utils import load_dotenv
from common.llm_client import generate_text
from figma import FigmaClient, extract_figma_url
from jira import JiraClient, extract_adf_text, extract_ticket_key

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

ANDROID_DEFAULT_BRANCH = os.environ.get("ANDROID_DEFAULT_BRANCH", "").strip()
README_PREVIEW_CHARS = int(os.environ.get("README_PREVIEW_CHARS", "3000"))
DEFAULT_WORKSPACE_ROOT = os.environ.get("ANDROID_WORKSPACE_ROOT", "/tmp/android-agent-workspaces")
TARGET_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "tests",
    "agent_test_targets.json",
)


def load_test_targets(config_path: str = TARGET_CONFIG_PATH) -> dict:
    defaults = {
        "ticketKey": "",
        "ticketUrl": "",
        "repoUrl": "",
    }
    if not os.path.isfile(config_path):
        return defaults
    with open(config_path, encoding="utf-8") as handle:
        config = json.load(handle)
    jira_ticket = ((config.get("jira") or {}).get("primaryTicket") or {})
    bitbucket_repo = ((config.get("bitbucket") or {}).get("primaryRepo") or {})
    return {
        "ticketKey": str(jira_ticket.get("ticketKey") or "").strip(),
        "ticketUrl": str(jira_ticket.get("browseUrl") or "").strip(),
        "repoUrl": str(bitbucket_repo.get("browseUrl") or "").strip(),
    }


def _write_workspace_file(workspace_path: str, relative_name: str, content: str) -> None:
    if not workspace_path:
        return
    os.makedirs(workspace_path, exist_ok=True)
    target_path = os.path.join(workspace_path, relative_name)
    os.makedirs(os.path.dirname(target_path), exist_ok=True)
    with open(target_path, "w", encoding="utf-8") as handle:
        handle.write(content)


def _discover_files_to_read(ticket_context: dict, ticket_key: str, repo_tree: str, readme_content: str):
    prompt = _prompts.FILE_DISCOVERY_PROMPT.format(
        ticket_key=ticket_key or "unknown",
        ticket_title=ticket_context.get("title") or f"Implement {ticket_key}",
        ticket_description=ticket_context.get("description") or f"Implement {ticket_key}",
        repo_project=ticket_context.get("repo_project") or "",
        repo_name=ticket_context.get("repo_name") or "repository",
        repo_tree=repo_tree or "(not available)",
        readme_chars=README_PREVIEW_CHARS,
        readme_content=(readme_content or "(no README found)")[:README_PREVIEW_CHARS],
    )
    raw = generate_text(prompt, "Android Agent - File Discovery")
    clean = (raw or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```[a-zA-Z]*\n?", "", clean)
        clean = re.sub(r"\n?```$", "", clean.strip())
    try:
        result = json.loads(clean)
        if isinstance(result, dict) and isinstance(result.get("files_to_read"), list):
            return result
    except Exception:
        pass
    return {"files_to_read": [], "analysis": raw[:500]}


def _generate_implementation_with_context(ticket_context: dict, ticket_key: str, repo_tree: str, file_contents: dict):
    description = ticket_context.get("description") or f"Implement Jira ticket {ticket_key}"
    repo_name = ticket_context.get("repo_name") or "repository"
    if file_contents:
        files_block = "\n\n".join(
            f"### {path}\n```\n{content[:8000]}\n```"
            for path, content in file_contents.items()
            if content
        )
    else:
        files_block = "(no files read — working from directory structure and ticket description only)"

    prompt = _prompts.IMPLEMENTATION_GENERATION_PROMPT.format(
        ticket_key=ticket_key or "unknown",
        ticket_title=ticket_context.get("title") or f"Implement {ticket_key}",
        ticket_description=description,
        repo_project=ticket_context.get("repo_project") or "",
        repo_name=repo_name,
        repo_url=ticket_context.get("repo_url") or "",
        file_contents=files_block,
        repo_tree_summary=(repo_tree or "")[:2000],
        additional_context=ticket_context.get("additional_context") or "(none)",
    )

    raw = generate_text(prompt, "Android Agent - Implementation")
    clean = (raw or "").strip()
    if clean.startswith("```"):
        clean = re.sub(r"^```[a-zA-Z]*\n?", "", clean)
        clean = re.sub(r"\n?```$", "", clean.strip())
    try:
        result = json.loads(clean)
        if isinstance(result, dict) and isinstance(result.get("files"), list):
            normalized_files = []
            for file_spec in result["files"]:
                if isinstance(file_spec, dict) and file_spec.get("path") and file_spec.get("content") is not None:
                    normalized_files.append({"path": file_spec["path"], "content": file_spec["content"]})
            result["files"] = normalized_files
            return result
    except Exception:
        pass

    raise RuntimeError(
        f"LLM returned unparseable JSON for implementation of {ticket_key}. Raw response (first 500 chars): {clean[:500]}"
    )


def _safe_pr_url(response_body: dict) -> str:
    if not isinstance(response_body, dict):
        return ""
    detail = response_body.get("detail") if isinstance(response_body.get("detail"), dict) else response_body
    if not isinstance(detail, dict):
        return ""
    links = detail.get("links") or {}
    self_links = links.get("self") or []
    if isinstance(self_links, list) and self_links and isinstance(self_links[0], dict):
        return str(self_links[0].get("href") or "")
    return str(detail.get("prUrl") or detail.get("url") or "")


def _safe_pr_id(response_body: dict):
    if not isinstance(response_body, dict):
        return None
    detail = response_body.get("detail") if isinstance(response_body.get("detail"), dict) else response_body
    if not isinstance(detail, dict):
        return None
    return detail.get("id")


def _build_pr_completion_adf(impl_goal: str, branch_name: str, pr_url: str, pr_id, impl_files, files_to_delete=None):
    pr_display = pr_url or (f"PR#{pr_id}" if pr_id else "Bitbucket PR")
    content = [
        {
            "type": "paragraph",
            "content": [{"type": "text", "text": "[Android Agent] Implementation complete — PR raised."}],
        },
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Goal: ", "marks": [{"type": "strong"}]},
                {"type": "text", "text": impl_goal},
            ],
        },
        {
            "type": "paragraph",
            "content": [
                {"type": "text", "text": "Branch: ", "marks": [{"type": "strong"}]},
                {"type": "text", "text": branch_name, "marks": [{"type": "code"}]},
            ],
        },
    ]

    pr_inline = [{"type": "text", "text": "PR: ", "marks": [{"type": "strong"}]}]
    if pr_url:
        pr_inline.append({
            "type": "text",
            "text": pr_display,
            "marks": [{"type": "link", "attrs": {"href": pr_url}}],
        })
    else:
        pr_inline.append({"type": "text", "text": pr_display})
    content.append({"type": "paragraph", "content": pr_inline})

    if impl_files:
        content.append({
            "type": "paragraph",
            "content": [{"type": "text", "text": "Files changed:", "marks": [{"type": "strong"}]}],
        })
        content.append({
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [{
                        "type": "paragraph",
                        "content": [{"type": "text", "text": file_spec["path"], "marks": [{"type": "code"}]}],
                    }],
                }
                for file_spec in impl_files
            ],
        })

    if files_to_delete:
        content.append({
            "type": "paragraph",
            "content": [{"type": "text", "text": "Files deleted:", "marks": [{"type": "strong"}]}],
        })
        content.append({
            "type": "bulletList",
            "content": [
                {
                    "type": "listItem",
                    "content": [{
                        "type": "paragraph",
                        "content": [{"type": "text", "text": path, "marks": [{"type": "code"}]}],
                    }],
                }
                for path in files_to_delete
            ],
        })

    return {"type": "doc", "version": 1, "content": content}


def _summarize_figma_context(figma_client: FigmaClient, *texts: str) -> tuple[str, str]:
    for text in texts:
        figma_url = extract_figma_url(text)
        if not figma_url:
            continue
        result, status = figma_client.fetch_from_url(figma_url)
        if status == "ok" and isinstance(result, dict):
            meta = result.get("fileMeta")
            meta = meta if isinstance(meta, dict) else {}
            file_name = meta.get("name") or "Unnamed Figma file"
            summary = f"Figma context: {file_name} ({figma_url})"
            return figma_url, summary
        return figma_url, f"Figma context could not be fetched from {figma_url}: {status}"
    return "", ""


def _failure_result(ticket_key: str, repo_url: str, workspace_path: str, steps_taken: list[str], error_text: str) -> dict:
    steps_text = "\n".join(f"- {step}" for step in steps_taken) if steps_taken else "- (no steps recorded)"
    summary = f"{error_text}\n\nSteps taken:\n\n{steps_text}"
    if workspace_path:
        _write_workspace_file(workspace_path, "android/android-workflow-summary.md", summary)
    return {
        "status": "failed",
        "ticketKey": ticket_key,
        "repoUrl": repo_url,
        "workspacePath": workspace_path,
        "summary": summary,
        "steps": list(steps_taken),
        "prUrl": "",
    }


def run_android_workflow(
    request_text: str,
    *,
    ticket_key: str = "",
    ticket_url: str = "",
    repo_url: str = "",
    workspace_path: str = "",
    base_branch: str = "",
) -> dict:
    defaults = load_test_targets()
    jira_client = JiraClient()
    bitbucket_client = BitbucketClient()
    figma_client = FigmaClient()

    normalized_ticket_key = (ticket_key or "").strip()
    normalized_ticket_url = (ticket_url or "").strip()
    normalized_repo_url = (repo_url or "").strip()
    normalized_request = (request_text or "").strip()

    if not normalized_ticket_key:
        normalized_ticket_key = extract_ticket_key(normalized_ticket_url or normalized_request) or defaults["ticketKey"]
    if not normalized_ticket_url:
        normalized_ticket_url = defaults["ticketUrl"]
    if not normalized_repo_url:
        normalized_repo_url = extract_repo_url(normalized_request) or defaults["repoUrl"]
    if not normalized_request:
        normalized_request = f"Implement {normalized_ticket_key} using {normalized_repo_url}".strip()

    repo_target = parse_repo_target(normalized_repo_url)
    if not workspace_path:
        workspace_slug = normalized_ticket_key or f"android-task-{int(time.time())}"
        workspace_path = os.path.join(DEFAULT_WORKSPACE_ROOT, workspace_slug.lower())

    steps_taken = []
    pr_url = ""
    pr_id = None

    debug_log("android-local", "android.workflow.start", ticketKey=normalized_ticket_key, repoUrl=normalized_repo_url)

    if not normalized_ticket_key:
        return _failure_result(
            normalized_ticket_key,
            normalized_repo_url,
            workspace_path,
            steps_taken,
            "Android agent requires a Jira ticket key. Pass --ticket or keep tests/agent_test_targets.json populated.",
        )
    if not repo_target["project"] or not repo_target["repo"]:
        return _failure_result(
            normalized_ticket_key,
            normalized_repo_url,
            workspace_path,
            steps_taken,
            "Android agent requires a Bitbucket browse URL. Pass --repo-url or keep tests/agent_test_targets.json populated.",
        )

    ticket_status, ticket_body = jira_client.fetch_ticket(normalized_ticket_key)
    raw_issue = ticket_body.get("issue") if ticket_status == 200 and isinstance(ticket_body, dict) else {}
    fields = raw_issue.get("fields") if isinstance(raw_issue, dict) else {}
    title = str((fields or {}).get("summary") or "").strip()
    description_field = (fields or {}).get("description")
    if isinstance(description_field, str):
        description = description_field.strip()
    elif isinstance(description_field, dict):
        description = extract_adf_text(description_field).strip()
    else:
        description = ""

    if not description:
        description = normalized_request or f"Implement {normalized_ticket_key}"

    described_repo_url = extract_repo_url(description)
    if described_repo_url and (not normalized_repo_url or not repo_target["project"]):
        normalized_repo_url = described_repo_url
        repo_target = parse_repo_target(normalized_repo_url)

    figma_url, figma_summary = _summarize_figma_context(figma_client, description, normalized_request)
    additional_context_parts = []
    if normalized_request and normalized_request != description:
        additional_context_parts.append(normalized_request)
    if figma_summary:
        additional_context_parts.append(figma_summary)
    ticket_context = {
        "title": title or f"Implement {normalized_ticket_key}",
        "description": description,
        "repo_url": normalized_repo_url,
        "repo_project": repo_target["project"],
        "repo_name": repo_target["repo"],
        "additional_context": "\n\n".join(additional_context_parts),
        "figmaUrl": figma_url,
    }

    project = repo_target["project"]
    repo = repo_target["repo"]
    source_branch = base_branch or ANDROID_DEFAULT_BRANCH or bitbucket_client.get_default_branch(project, repo)
    branch_name = f"agent/feature/{normalized_ticket_key}"

    try:
        myself_status, myself_body = jira_client.get_myself()
        account_id = None
        display_name = None
        if myself_status == 200 and isinstance(myself_body, dict):
            user = myself_body.get("user") or myself_body
            if isinstance(user, dict):
                raw_account_id = user.get("accountId")
                account_id = str(raw_account_id).strip() if raw_account_id else None
                display_name = user.get("displayName") or user.get("name") or account_id

        if account_id:
            assign_status, _ = jira_client.change_assignee(normalized_ticket_key, account_id)
            if assign_status in (200, 204):
                steps_taken.append(f"Assigned {normalized_ticket_key} to {display_name or account_id}")

        transition_status, transition_body = jira_client.transition_issue(normalized_ticket_key, "In Progress")
        if transition_status in (200, 204):
            transition_result = transition_body.get("result", "In Progress") if isinstance(transition_body, dict) else "In Progress"
            steps_taken.append(f"Transition {normalized_ticket_key}: {transition_result}")

        jira_client.add_comment(
            normalized_ticket_key,
            text="[Android Agent] Ticket picked up for implementation. Cloning repository and analysing requirements…",
        )
        steps_taken.append(f"Added starting comment to {normalized_ticket_key}")

        clone_root = os.path.join(workspace_path, "repo-clone")
        clone_path, clone_result = bitbucket_client.clone_repo_to_workspace(project, repo, source_branch, clone_root)
        if not clone_path:
            return _failure_result(
                normalized_ticket_key,
                normalized_repo_url,
                workspace_path,
                steps_taken,
                f"Android agent failed to clone repo for {normalized_ticket_key}: {clone_result}",
            )
        steps_taken.append(f"Repo cloned to {clone_path} ({clone_result})")

        repo_tree = bitbucket_client.get_repo_tree(clone_path)
        readme_content = ""
        for readme_name in ("README.md", "readme.md", "Readme.md"):
            content, result = bitbucket_client.get_repo_file(clone_path, readme_name)
            if result == "ok":
                readme_content = content
                break
        steps_taken.append(f"Read repo tree ({len(repo_tree)} chars)")

        discovery = _discover_files_to_read(ticket_context, normalized_ticket_key, repo_tree, readme_content)
        files_to_read = discovery.get("files_to_read") or []
        steps_taken.append(f"LLM file discovery: {len(files_to_read)} files identified")

        file_contents = {}
        for file_path in files_to_read[:15]:
            content, result = bitbucket_client.get_repo_file(clone_path, file_path)
            if result == "ok":
                file_contents[file_path] = content
        if file_contents:
            steps_taken.append(
                f"Read {len(file_contents)} files: " + ", ".join(list(file_contents.keys())[:5])
            )

        implementation = _generate_implementation_with_context(
            ticket_context,
            normalized_ticket_key,
            repo_tree,
            file_contents,
        )
        impl_goal = implementation.get("goal") or f"Implement {normalized_ticket_key}"
        impl_files = implementation.get("files") or []
        impl_files_to_delete = implementation.get("files_to_delete") or []
        impl_pr_desc = implementation.get("pr_description") or f"## {normalized_ticket_key}\n\n{impl_goal}"
        if not impl_files and not impl_files_to_delete:
            return _failure_result(
                normalized_ticket_key,
                normalized_repo_url,
                workspace_path,
                steps_taken,
                f"Android agent could not generate implementation files for {normalized_ticket_key}.",
            )

        change_summary_parts = []
        if impl_files:
            change_summary_parts.append(f"{len(impl_files)} file(s): " + ", ".join(file_spec["path"] for file_spec in impl_files[:5]))
        if impl_files_to_delete:
            change_summary_parts.append(
                f"deleting {len(impl_files_to_delete)}: " + ", ".join(impl_files_to_delete[:3])
            )
        steps_taken.append("LLM generated: " + "; ".join(change_summary_parts))

        commit_message = f"feat({normalized_ticket_key}): {impl_goal} [android-agent]"
        push_status, push_body = bitbucket_client.push_files(
            project,
            repo,
            branch_name,
            base_branch=source_branch,
            files=impl_files,
            commit_message=commit_message,
            files_to_delete=impl_files_to_delete,
        )
        push_result = push_body.get("result", f"http_{push_status}") if isinstance(push_body, dict) else str(push_status)
        if push_result != "pushed":
            push_detail = push_body.get("detail", {}) if isinstance(push_body, dict) else {}
            push_output = push_detail.get("output", "") if isinstance(push_detail, dict) else ""
            error_text = f"Android agent failed to push files for {normalized_ticket_key}: {push_result}."
            if push_output:
                error_text += f" Git output: {push_output[:400]}"
            return _failure_result(
                normalized_ticket_key,
                normalized_repo_url,
                workspace_path,
                steps_taken,
                error_text,
            )
        steps_taken.append(f"Branch {branch_name}: pushed {len(impl_files)} file(s)")

        pr_title = f"[{normalized_ticket_key}] {impl_goal}"
        pr_status, pr_body = bitbucket_client.create_pull_request(
            project,
            repo,
            branch_name,
            to_branch=source_branch,
            title=pr_title,
            description=impl_pr_desc,
        )
        pr_result = pr_body.get("result", f"http_{pr_status}") if isinstance(pr_body, dict) else str(pr_status)
        if pr_result not in {"created", "existing"} and pr_status not in (200, 201):
            return _failure_result(
                normalized_ticket_key,
                normalized_repo_url,
                workspace_path,
                steps_taken,
                f"Android agent failed to create PR for {normalized_ticket_key}: {pr_result}",
            )
        pr_url = _safe_pr_url(pr_body)
        pr_id = _safe_pr_id(pr_body)
        steps_taken.append(f"PR created: {pr_url or pr_id or pr_result}")

        for review_name in ("Under Review", "under review", "In Review"):
            review_status, review_body = jira_client.transition_issue(normalized_ticket_key, review_name)
            if review_status in (200, 204) and isinstance(review_body, dict) and review_body.get("transitionId"):
                steps_taken.append(f"Transitioned {normalized_ticket_key} to {review_name}")
                break

        jira_client.add_comment(
            normalized_ticket_key,
            adf_body=_build_pr_completion_adf(impl_goal, branch_name, pr_url, pr_id, impl_files, impl_files_to_delete),
        )
        steps_taken.append(f"Added PR completion comment to {normalized_ticket_key}")

        steps_text = "\n".join(f"- {step}" for step in steps_taken)
        summary = (
            f"Android agent completed workflow for {normalized_ticket_key}.\n\n"
            f"Steps taken:\n\n{steps_text}"
        )
        if pr_url:
            summary += f"\n\nPR: {pr_url}"

        _write_workspace_file(workspace_path, "android/android-impl-goal.txt", impl_goal)
        _write_workspace_file(workspace_path, "android/android-workflow-summary.md", summary)
        for file_spec in impl_files:
            _write_workspace_file(workspace_path, os.path.join("android/files", file_spec["path"]), file_spec["content"])

        return {
            "status": "completed",
            "ticketKey": normalized_ticket_key,
            "repoUrl": normalized_repo_url,
            "workspacePath": workspace_path,
            "summary": summary,
            "steps": steps_taken,
            "prUrl": pr_url,
            "branch": branch_name,
            "figmaUrl": figma_url,
        }
    except Exception as error:
        debug_log("android-local", "android.workflow.failed", ticketKey=normalized_ticket_key, error=str(error))
        return _failure_result(
            normalized_ticket_key,
            normalized_repo_url,
            workspace_path,
            steps_taken,
            f"Android agent failed unexpectedly: {error}",
        )