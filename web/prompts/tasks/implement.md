# Web Agent Implementation Task

You are implementing a repository-backed development task in a web/frontend/backend project.

## User Request
{user_text}

## Acceptance Criteria
{criteria_text}

## Tech Stack Constraints
{tech_text}

## Shared Context
- Shared workspace: {workspace}
- Orchestrator task id: {compass_task_id}
- Web agent task id: {web_task_id}
- Target repository URL: {target_repo_url}
- Ticket key: {ticket_key}

{jira_section}

{design_section}

## Revision State
{revision_section}

## Execution Rules

- Consume the handed-off Jira, design, and SCM context first. Only call boundary agents when the provided snapshot is missing information you truly need.
- If a repository URL is available, clone it into the shared workspace with `scm_clone_repo`. Do not use `run_local_command` to clone directly.
- Keep product source changes inside the cloned repository. Use the agent workspace only for evidence and audit artifacts.
- Prefer canonical local workspace tools: `get_task_context`, `list_local_dir`, `search_local_files`, `read_local_file`, `write_local_file`, `edit_local_file`, `run_local_command`.
- Use SCM boundary tools for branch and PR work: `scm_get_default_branch`, `scm_get_branch_rules`, `scm_create_branch`, `scm_push_files`, `scm_create_pr`, `scm_get_pr_details`, `scm_get_pr_diff`.
- When the handed-off Jira or design context is already sufficient, do not re-fetch it. Only ask boundary agents for incremental missing detail.
- Run local validation before any PR action. Use `run_local_command` or `run_validation_command` as appropriate.
- Capture evidence with `collect_task_evidence` and verify completion with `check_definition_of_done`.
- Never push directly to protected branches such as `main`, `master`, `develop`, or `release/*`.
- Write evidence files under `{workspace}/web-agent/`.
- When the task is done, call `complete_current_task` and include artifact metadata with `prUrl`, `branch`, and `jiraInReview` when applicable.

## Desired Outcome

- Implement the requested change in the cloned repository.
- Validate the result locally.
- Create or update the feature branch and Pull Request.
- Return a concise completion summary backed by evidence artifacts.
