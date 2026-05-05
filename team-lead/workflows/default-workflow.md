# Team Lead Default Workflow

## Purpose

This workflow defines the default development lifecycle Team Lead uses to plan, supervise, review, and close work performed by execution agents.

## Stages

1. Intake: confirm the request, scope, constraints, and missing information.
2. Planning: define execution phases, risks, dependencies, and owners.
3. Architecture: confirm solution boundaries, integration points, and notable tradeoffs.
4. Design: translate the approved plan into file-level or component-level work items.
5. Implementation: dispatch execution agents and monitor their progress.
6. Local Branching: ensure the execution agent created or reused a local development branch inside the cloned repo before editing files.
7. Testing: review local build/test commands, results, retries, and failed-path handling.
8. Design Audit: for UI work, compare design-reference evidence and implementation screenshots component by component.
9. Review: compare output against acceptance criteria and request rework if needed.
10. Wrap-up: summarize what changed, what was validated, and what risks remain.

## Checkpoints

- Each stage must define required evidence before the next stage starts.
- Repo-backed implementation must not skip the local-branch checkpoint: code changes are expected on a local branch inside the shared-workspace clone before any PR is raised.
- Feature and UI work must produce local build/test evidence before review.
- UI work with design context must produce both design-reference evidence and implementation screenshot evidence before review.
- `Architecture` and `Design` may be skipped only when Team Lead records why the skip is safe.
- Rework must state what is missing, what evidence is required, and what the next acceptance bar is.
- Implementation requests must include a Jira ticket URL or key before Team Lead moves past intake.
- During `GATHERING_INFO`, Team Lead should maintain an explicit pending-task list and iterate:
	analyze current context → choose the next registered capability to call → fetch context → re-analyze.
- The gather loop should prefer registered boundary-agent capabilities over user questions.
	Only after Jira/design/repo context has been exhausted may Team Lead pause for `INPUT_REQUIRED`.
- Agentic runtime output during the gather loop must stay structured: it may decide the next pending
	actions, but the Team Lead code remains responsible for executing A2A calls through Registry-discovered capabilities.
- When a web implementation request includes Jira/design context but no explicit tech stack, Team Lead must pause with `INPUT_REQUIRED` and ask the user to confirm the stack before planning or dispatch.
- If a design URL is known and the ticket names a page or screen but does not provide a node/screen ID, Team Lead should fetch design context by page or screen name before asking the user for more design detail.
- After an `INPUT_REQUIRED` resume, Team Lead must continue the same task context and carry the confirmed constraints into the subsequent plan and downstream dispatch metadata.
- Review feedback should explicitly call out missing branch/test/screenshot/Jira evidence so the next revision cycle closes those gaps, not just source-code gaps.

## Rework Limits

- Execution rework loops must have an explicit cap.
- When the cap is exceeded, Team Lead must either request user input or fail the task.

## Parallel Work Rules

- Parallel execution may start only after planning is approved.
- Every parallel branch needs its own evidence trail and review outcome.
