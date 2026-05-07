# Android Agent — Available Tools

## Local Workspace Tools

| Tool | Purpose |
|------|---------|
| `read_local_file` | Read a file in the local workspace |
| `write_local_file` | Write or overwrite a file in the workspace |
| `edit_local_file` | Replace a specific string/block within a file |
| `list_local_dir` | List contents of a directory |
| `search_local_files` | Grep/search for patterns across workspace files |
| `run_local_command` | Execute shell commands (gradlew, adb, etc.) |

## SCM Tools

| Tool | Purpose |
|------|---------|
| `scm_clone_repo` | Clone the target Android repository |
| `scm_create_branch` | Create feature branch |
| `scm_push_files` | Push changes to remote branch |
| `scm_create_pr` | Create pull request |
| `scm_get_pr_details` | Get PR metadata |

## Validation Tools

| Tool | Purpose |
|------|---------|
| `run_validation_command` | Run Gradle build/test/lint |
| `collect_task_evidence` | Collect build logs, test results as evidence |
| `check_definition_of_done` | Evaluate task completion |
| `summarize_failure_context` | Structured failure analysis |

## Jira Tools

| Tool | Purpose |
|------|---------|
| `jira_get_ticket` | Fetch Jira ticket details when task context needs refreshing |
| `jira_add_comment` | Add implementation notes or status comments to the Jira ticket |
| `jira_search` | Search related Jira tickets by JQL |
| `jira_transition` | Transition a Jira ticket to a new status |

## Control Tools

| Tool | Purpose |
|------|---------|
| `report_progress` | Report a major step to the orchestrator |
| `complete_current_task` | Mark this task as completed |
| `fail_current_task` | Mark this task as failed with reason |
| `get_task_context` | Get current task metadata and workspace path |

## Tool Usage Order (Standard Path)

1. `get_task_context` — read task metadata, Jira context, design context
2. `scm_clone_repo` — clone the Android repository
3. `list_local_dir` / `read_local_file` — understand existing code structure
4. `write_local_file` / `edit_local_file` — implement changes
5. `run_validation_command` (build) — Gradle assembleDebug
6. `run_validation_command` (unit_test) — Gradle testDebugUnitTest
7. `scm_create_branch` → `scm_push_files` → `scm_create_pr`
8. `collect_task_evidence` — capture build logs, test results
9. `complete_current_task` — signal completion
