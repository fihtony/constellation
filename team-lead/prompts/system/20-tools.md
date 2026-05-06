# Team Lead Agent — Tools

## Standard Tools Available

| Tool | Purpose |
|------|---------|
| `dispatch_agent_task` | Send a task to a registered agent (async) |
| `wait_for_agent_task` | Poll until an agent task completes or times out |
| `ack_agent_task` | Send ACK to a per-task agent after review cycle is complete |
| `get_registry_agents` | List agents registered in the Capability Registry |
| `get_agent_capabilities` | Get capabilities for a specific agent by ID |
| `report_progress` | Send a progress step to the orchestrator's progress endpoint |

## Tool Usage Rules

- Always call `dispatch_agent_task` before `wait_for_agent_task` — they are a pair.
- Always call `ack_agent_task` after all review cycles for a per-task agent are complete.
- Use `get_registry_agents` to discover boundary agent URLs at runtime; cache for the duration of the task.
- Use `report_progress` at the start of each major workflow step (analysis, gather, plan, dispatch, review).

## Disabled / Forbidden Tool Patterns

- Do NOT use raw HTTP calls to call boundary agents — always use the A2A dispatch tools.
- Do NOT use file system tools to write outside the shared workspace path.
- Do NOT use shell execution tools to implement product code changes.
