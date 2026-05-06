# SCM Agent — Available Tools

## Remote Read-Only Tools (no local clone required)

- `scm_read_file` — Read a file from a remote repo + branch.
- `scm_list_dir` — List directory contents in a remote repo.
- `scm_search_code` — Search code across a remote repository.
- `scm_list_branches` — List all branches in a remote repository.
- `scm_get_pr_details` — Fetch PR metadata (title, description, status, reviewers).
- `scm_get_pr_diff` — Fetch the unified diff for a PR.
- `scm_get_default_branch` — Get the default branch name for a repository.
- `scm_compare_refs` — Compare two refs (branches/tags/commits) for diff.
- `scm_repo_inspect` — Inspect repository metadata (languages, topics, size).

## Write Tools

- `scm_create_branch` — Create a new branch from a base ref.
- `scm_push_files` — Push file changes to a remote branch.
- `scm_clone_repo` — Clone a repository into the shared workspace.
- `scm_create_pr` — Create a pull request.
- `scm_add_pr_comment` — Add a review comment to a PR.

## Common Tools

- `report_progress` — Report progress for long-running operations.
- `complete_current_task` — Mark the current task as complete.
- `fail_current_task` — Mark the current task as failed.
