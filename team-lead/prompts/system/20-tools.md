# Team Lead Agent — Tools

## Gathering & Context Tools

| Tool | Purpose |
|------|---------|
| `jira_get_ticket` | Fetch Jira ticket details (summary, description, acceptance criteria, attachments) |
| `jira_add_comment` | Add a structured comment to a Jira ticket |
| `design_fetch_figma_screen` | Fetch design specs from Figma |
| `design_fetch_stitch_screen` | Fetch design specs from Google Stitch |
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
| `read_file` | Read files from the workspace |
| `write_file` | Write files to the workspace |
| `glob` | Find files by pattern |
| `grep` | Search file contents |

## Tool Usage Rules

- Always call `dispatch_agent_task` before `wait_for_agent_task` — they are a pair.
- Always call `ack_agent_task` after all review cycles for a per-task agent are complete.
- Use `registry_query` to discover boundary agent URLs at runtime.
- Use `report_progress` at the start of each major workflow step.
- Use `jira_get_ticket` to gather context instead of dispatching to the Jira agent for simple fetches.
- Use `request_user_input` only after exhausting all other sources of clarification.

## Disabled / Forbidden Tool Patterns

- Do NOT use raw HTTP calls to call boundary agents — always use the A2A dispatch tools.
- Do NOT use file system tools to write outside the shared workspace path.
- Do NOT use shell execution tools to implement product code changes.
- Do NOT hardcode agent URLs — always discover via Registry.
