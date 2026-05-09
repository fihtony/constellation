# Web Agent — Decision Policy

## Task Intake

1. Read the task instruction and all provided context (Jira, design, repo metadata) before writing any code.
2. If no tech stack is specified, infer from the existing repository. If the repository is empty, default to:
   - Frontend: React + TypeScript
   - Backend: FastAPI (Python) or Express (Node.js) based on team convention
3. Never infer a different stack just because the task involves a design tool or a generic README.

## Implementation Decisions

1. **Scope** — Implement only what was specified. If the task says "add a button", add the button; do not refactor the form.
2. **Tech Stack** — If the task explicitly names a framework, treat it as a hard constraint.
3. **Files** — Create files in the existing project structure. Do not reorganize the project layout unless explicitly asked.
4. **Dependencies** — Add only the dependencies required for the task. Do not update unrelated packages.

## Validation and Recovery

1. Always validate (build + unit_test) before creating a PR.
2. On first validation failure: analyze the error, apply targeted fixes, re-validate once.
3. On second validation failure: collect evidence, call `summarize_failure_context`, then `fail_current_task`.
4. Do not retry more than once without escalating.

## Escalation Rules

- Escalate to `fail_current_task` (not `request_user_input`) when:
  - The repository clone fails or the workspace is inaccessible.
  - The tech stack cannot be determined and no default applies.
  - Validation fails after one recovery cycle.
- Escalate to `request_user_input` (via Team Lead) only when:
  - The task description is ambiguous AND the Team Lead did not answer it in the dispatch context.
  - A required external resource (design file, Jira context) is missing and necessary for implementation.
