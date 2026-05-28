# Constellation Development Agent Authoring

Use this skill when creating or reviewing a new development execution agent for Constellation, such as an Android agent, iOS agent, database agent, or another repo-backed implementation agent.

## Scope

This skill applies to development agents that:
- run as per-task execution agents
- work inside a shared workspace clone
- receive a parent-issued `executionContract`
- may call boundary agents such as Jira, SCM, or UI Design through registry-discovered capabilities

It does not apply to:
- persistent control-plane agents such as Compass or Team Lead
- pure boundary adapters such as Jira, SCM, or UI Design
- office/document processing agents unless they are being redesigned into repo-backed development agents

## Required Design Rules

1. Graph outside, ReAct inside.
- Model the macro lifecycle with workflow nodes such as setup, analyze, implement, validate, report.
- Use runtime reasoning only inside bounded nodes.

2. Child agents are fail-closed.
- Require `message.metadata.executionContract` on every child task.
- Resolve the contract against the local `permission_profile`.
- Reject the task if the contract is missing, invalid, or broader than the local profile.

3. Child permission handoff is explicit and child-scoped.
- Parent agents must attach `message.metadata.permissions` derived from the child contract/profile.
- Child agents may only use the permission snapshot passed in `message.metadata.permissions`.
- If `message.metadata.permissions` is missing, the child must not invent, inherit, or reconstruct permissions from the parent context.

4. Boundary access is registry-only.
- Discover Jira, SCM, UI Design, and any future boundary agent through the Capability Registry.
- Do not hardcode service URLs.
- Do not bypass A2A boundaries with direct product API calls from development agents.

5. Task work stays inside the shared workspace.
- Read upstream context from the shared workspace.
- Write implementation evidence, logs, and delivery artifacts under the child agent directory.
- Keep source changes inside the cloned repository tree, not inside the agent evidence folder.

## Required File Layout

Each new development agent should include at least:
- `agents/<agent_name>/agent.py`
- `agents/<agent_name>/nodes.py`
- `agents/<agent_name>/tools.py`
- `agents/<agent_name>/config.yaml`
- `agents/<agent_name>/Dockerfile`
- `agents/<agent_name>/prompts.py` or prompt assets under `prompts/`

Add supporting tests under:
- `tests/unit/agents/test_<agent_name>.py`
- integration or e2e coverage when the agent launches containers or touches live systems

## Permission Profile Rules

Define a dedicated maximum capability envelope in:
- `config/permissions/<agent-id>.yaml`

Rules for that file:
- `agent_id` must match the agent definition
- `allowed_tools` should contain only the tools that agent needs for its job
- `denied_tools` should explicitly block destructive or unrelated tools when appropriate
- `filesystem` should remain `workspace-only` for repo-backed execution agents
- `agent_launching` should stay `false` unless the agent truly launches other agents
- `allowed_agents` should be empty unless the design intentionally allows child dispatch

Guidance by agent type:
- Android or iOS agents usually need `read_file`, `write_file`, `edit_file`, `run_command`, search tools, selected SCM tools, and limited Jira transition/comment tools.
- Database agents may need repo editing tools plus migration/test commands; do not grant broad production database access by default.
- Frontend or backend web agents may need screenshot upload or PR update tools only if the delivery contract requires them.

## Tool Set Rules

Start from the minimum viable tool set.

Typical repo-backed development tools:
- `read_file`
- `write_file`
- `edit_file`
- `run_command`
- `search_code`
- `glob`
- `grep`

Add SCM tools only when the workflow truly needs them:
- `scm_push`
- `scm_create_pr`
- `scm_get_pr_diff`
- `scm_get_pr_info`
- `scm_upload_pr_image`
- `scm_update_pr`

Add Jira tools only when the workflow owns Jira state transitions or audit comments:
- `jira_transition`
- `jira_comment`
- `jira_list_transitions`
- `jira_get_token_user`

Do not grant:
- unrelated office tools
- direct network tools that bypass boundary agents
- local-system mutation tools outside the workspace contract

## Dockerfile Rules

Every per-task development agent Dockerfile must:
- copy `framework/`
- copy the agent package under `agents/<agent_name>/`
- copy `config/constellation.yaml`
- copy `config/permissions/<agent-id>.yaml`
- copy any required workflow and rule files
- copy `scripts/run_local.py`
- set `ARTIFACT_ROOT=/app/artifacts`
- run as non-root user `appuser`

If the agent uses workspace skills at runtime, also copy the required skill folders into `/app/skills/`.

## Logging And Evidence Requirements

Every task should produce:
- `artifacts/<task_id>/<agent-id>/agent.log`
- agent-specific evidence files such as validation summaries, self-assessment, PR evidence, or screenshots
- structured final artifact metadata for upstream completeness checks

Log at least:
- task receipt
- execution contract acceptance or rejection
- major workflow node transitions
- validation results
- boundary call failures
- final delivery summary

## Validation Requirements

Before accepting a new agent implementation, verify:
- unit tests cover execution-contract failure paths
- unit tests cover child-scoped permission handoff where the agent calls boundary tools
- unit tests cover evidence generation in the workspace
- the Dockerfile includes the local permission profile
- live or container-based tests prove the agent can finish a real task end-to-end

## Review Checklist For New Agents

Reject the design if any of the following are true:
- the agent can operate without `executionContract`
- the agent falls back to parent or locally generated permissions
- the agent hardcodes Jira, SCM, or UI Design URLs
- the agent writes outside the shared workspace
- the agent creates delivery evidence only in memory and not in workspace files
- the Dockerfile omits the agent permission profile or required workflow assets

Accept the design only when:
- the workflow stages are explicit
- the permission envelope is minimal and child-scoped
- boundary access is registry-discovered
- logs and artifacts are auditable
- unit and runtime validation both pass