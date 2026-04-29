# SCM Agent Output Contract

## Required Outputs

- Explicit repository reference used for the operation.
- Structured response for repo inspection, branch creation, clone, push, or pull request actions.
- Relevant identifiers such as branch name, pull request number, clone path, or commit SHA.
- Error category for auth, permission, validation, or repository-state failures.

## Write Evidence

- Record which branch or pull request was created or changed.
- Preserve any generated URLs, commit identifiers, or destination refs.

## Failure Output

- State whether the failure is retryable, permission-related, or blocked by repository policy.
