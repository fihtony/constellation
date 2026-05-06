# Web Agent — Available Tools

## Local Workspace Tools

| Tool | Purpose |
|------|---------|
| `read_local_file` | Read a file in the local workspace |
| `write_local_file` | Write or overwrite a file in the workspace |
| `edit_local_file` | Replace a specific string/block within a file |
| `list_local_dir` | List contents of a directory |
| `search_local_files` | Grep/search for patterns across workspace files |
| `run_local_command` | Execute shell commands (build, test, npm, gradle) |

## SCM Remote Tools

| Tool | Purpose |
|------|---------|
| `scm_read_file` | Read a file from a remote repo branch (no clone needed) |
| `scm_list_dir` | List a remote repo directory |
| `scm_list_branches` | List branches in a remote repository |
| `scm_create_branch` | Create a new branch from a base |
| `scm_push_files` | Push file changes to a remote branch |
| `scm_create_pr` | Create a pull request |
| `scm_get_pr_details` | Get PR metadata and status |
| `scm_get_pr_diff` | Get the diff for a PR |
| `scm_clone_repo` | Clone a repository into the shared workspace |

## Validation Tools

| Tool | Purpose |
|------|---------|
| `run_validation_command` | Run build/test/lint/e2e checks |
| `collect_task_evidence` | Collect logs, diffs, screenshots as evidence |
| `check_definition_of_done` | Evaluate task completion against DoD criteria |
| `summarize_failure_context` | Produce structured failure analysis |

## Control Tools

| Tool | Purpose |
|------|---------|
| `report_progress` | Report a major step to the orchestrator |
| `complete_current_task` | Mark this task as completed |
| `fail_current_task` | Mark this task as failed with reason |
| `request_user_input` | Ask the user a blocking question (escalate upward) |
| `get_task_context` | Get current task metadata and workspace path |
| `get_agent_runtime_status` | Check current backend and readiness |

## Tool Usage Order (Standard Path)

1. `get_task_context` — read task metadata, Jira context, design context
2. `scm_clone_repo` — clone repository if repo_url provided
3. `read_local_file` / `list_local_dir` — understand existing code
4. `write_local_file` / `edit_local_file` — implement changes
5. `run_validation_command` (build + unit_test) — validate locally
6. `scm_create_branch` → `scm_push_files` → `scm_create_pr`
7. `collect_task_evidence` — capture evidence
8. `complete_current_task` — signal completion
