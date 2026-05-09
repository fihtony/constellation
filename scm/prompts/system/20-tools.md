# SCM Agent — Available Tools

## Remote Read-Only Tools (no local clone required)

- `scm_repo_inspect` — Inspect repository metadata (default branch, languages, topics, size).
- `scm_list_branches` — List all branches in a remote repository.
- `scm_get_default_branch` — Get the default branch and protected branches of a repository.
- `scm_get_branch_rules` — Get branch protection rules for a repository (local policy + remote settings).
- `scm_read_file` — Read a file from a remote repo + branch without cloning.
- `scm_list_dir` — List directory contents in a remote repo without cloning.
- `scm_search_code` — Search code across a remote repository.
- `scm_compare_refs` — Compare two refs (branches/tags/commits) for diff, ahead/behind stats.
- `scm_get_pr_details` — Fetch PR metadata (title, description, status, reviewers, merge status).
- `scm_get_pr_diff` — Fetch the unified diff for a PR.
- `scm_list_prs` — List pull requests for a repository (filter by state: open/closed/merged).
- `scm_list_pr_comments` — List review comments on a pull request.

## Write Tools

- `scm_create_branch` — Create a new branch from a base ref.
- `scm_push_files` — Push file changes to a remote branch (commit + push).
- `scm_create_pr` — Create a pull request.
- `scm_add_pr_comment` — Add a review comment to a PR (general or inline with file+line).

## Clone Tool

- `scm_clone_repo` — Clone a repository into the shared workspace for local operations.

## Common Tools

- `todo_write` — Record a short plan or checklist for multi-step SCM work.
- `report_progress` — Report progress for long-running operations.
- `complete_current_task` — Mark the current task as complete.
- `fail_current_task` — Mark the current task as failed with a structured error.
- `get_task_context` — Inspect the current task metadata and permissions snapshot.
- `get_agent_runtime_status` — Inspect the current runtime backend and readiness.
- `load_skill` — Load an SCM workflow skill for guidance.
- `list_skills` — List available skills before loading one dynamically.
