# Team Lead Agent — Tools

## Gathering & Context Tools

| Tool | Purpose |
|------|---------|
| `jira_get_ticket` | Fetch Jira ticket details (summary, description, acceptance criteria, attachments) |
| `jira_add_comment` | Add a structured comment to a Jira ticket |
| `jira_search` | Search Jira tickets by JQL query |
| `jira_transition` | Transition a Jira ticket to a new status (e.g. In Review) |
| `jira_assign` | Assign a Jira ticket to a user by account ID |
| `jira_get_transitions` | List available transitions for a Jira ticket |
| `jira_get_myself` | Get current authenticated Jira user info |
| `jira_create_issue` | Create a new Jira issue |
| `jira_update_fields` | Update arbitrary fields on a Jira ticket |
| `jira_validate_permissions` | Check if a Jira action is allowed by current permissions |
| `design_fetch_figma_screen` | Fetch design specs from Figma |
| `design_fetch_stitch_screen` | Fetch design specs from Google Stitch |
| `scm_clone_repo` | Clone the target repository into the shared workspace before dev dispatch |
| `scm_repo_inspect` | Inspect remote repo metadata (default branch, languages, build system) |
| `scm_read_file` | Read a remote repo file without cloning |
| `scm_list_dir` | Inspect a remote repo directory without cloning |
| `scm_search_code` | Search remote repo code without cloning |
| `scm_compare_refs` | Compare remote refs when investigating branch or revision state |
| `scm_get_default_branch` | Resolve default/protected branches before dispatching repo-backed work |
| `registry_query` | Query Registry for a specific capability |
| `list_available_agents` | List all registered agents and capabilities |
| `check_agent_status` | Check if a downstream agent is healthy |

## Orchestration Tools

| Tool | Purpose |
|------|---------|
| `dispatch_agent_task` | Send a task to a registered agent (async) |
| `wait_for_agent_task` | Poll until an agent task completes or times out |
| `ack_agent_task` | Send ACK to a per-task agent after review cycle is complete |
| `launch_per_task_agent` | Launch a per-task agent container when no idle instance exists |
| `report_progress` | Send a progress step to the orchestrator |
| `request_user_input` | Ask user for clarification (enters INPUT_REQUIRED state) |

## Lifecycle Tools

| Tool | Purpose |
|------|---------|
| `complete_current_task` | Signal task completion with summary and artifacts |
| `fail_current_task` | Signal task failure with error details |
| `get_task_context` | Get current task metadata, permissions, workspace |
| `get_agent_runtime_status` | Check current runtime backend status |

## Planning & Utility Tools

| Tool | Purpose |
|------|---------|
| `todo_write` | Write/update a structured task plan |
| `load_skill` | Load a skill playbook for domain guidance |
| `list_skills` | Discover available playbooks before loading one dynamically |
| `read_local_file` | Read files from the shared workspace |
| `write_local_file` | Write plan and summary files inside the shared workspace |
| `edit_local_file` | Update existing workspace files without rewriting the whole file |
| `list_local_dir` | Inspect workspace contents before deciding the next step |
| `search_local_files` | Search workspace logs, plans, and evidence |
| `scm_get_pr_details` | Inspect PR metadata during review |
| `scm_get_pr_diff` | Inspect PR diff content during review |
| `collect_task_evidence` | Aggregate workspace and artifact evidence before review decisions |
| `check_definition_of_done` | Validate the delivery against acceptance and workflow checks |
| `read_file` | Legacy alias for reading workspace files |
| `write_file` | Legacy alias for writing workspace files |
| `glob` | Legacy alias for file-pattern discovery |
| `grep` | Legacy alias for content search |

## Tool Usage Rules

- Always call `dispatch_agent_task` before `wait_for_agent_task` — they are a pair.
- Always call `ack_agent_task` after all review cycles for a per-task agent are complete.
- Use `registry_query` to discover boundary agent URLs at runtime.
- Use `report_progress` at the start of each major workflow step.
- Use `jira_get_ticket` to gather context instead of dispatching to the Jira agent for simple fetches.
- Use `scm_clone_repo` for repo-backed tasks before dispatching a development agent; the Team Lead owns the initial clone handoff.
- Use `jira_transition` to move tickets to "In Review" after a PR is created.
- Use `jira_validate_permissions` before performing write operations on Jira tickets.
- Use `request_user_input` only after exhausting all other sources of clarification.
- Pass `repoWorkspacePath`, `jiraContext`, and `designContext` downstream when the initial handoff is already complete; do not ask the execution agent to re-clone or re-fetch by default.
- Prefer the `*_local_*` tool names for shared-workspace artifacts and plans; keep legacy aliases only for compatibility with older prompts.

## Disabled / Forbidden Tool Patterns

- Do NOT use raw HTTP calls to call boundary agents — always use the A2A dispatch tools.
- Do NOT use file system tools to write outside the shared workspace path.
- Do NOT use shell execution tools to implement product code changes.
- Do NOT hardcode agent URLs — always discover via Registry.
