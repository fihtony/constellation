MANDATORY development workflow — follow these steps in order:

1. When you start development: transition the Jira ticket to `In Progress`, assign it to the service account, and add a comment saying you started.
2. Clone the target repository into the shared workspace, create or reuse a local development branch inside that clone, and make all code changes on that local branch.
3. Implement the feature following the acceptance criteria and any design constraints.
4. Run the required build and test commands locally in the cloned repository. Record the commands, results, and any retries in workspace evidence before raising a PR.
5. Perform self-assessment against the acceptance criteria and, when design context exists, compare the implementation component-by-component against the original design. If anything is missing, wrong, redundant, or unverified, keep fixing and re-running validation.
6. For UI tasks, capture both the original design screenshot/reference and the implemented UI screenshot, save them in the workspace, commit PR-safe copies under `docs/evidence/` when the task is repo-backed, and include them in the PR description.
7. Push your implementation to the development branch and create a Pull Request targeting the repository default branch. Before creating the branch, list existing remote branches via the SCM agent (`scm.branch.list`). If the desired branch name already exists, append `_2`, `_3`, etc. until a unique name is found.
8. After the PR is created: request Jira comment/update work through the Jira agent, transition the Jira ticket to `In Review` or `Under Review`, and add a comment that includes the PR URL, branch name, test status, screenshot evidence, and a brief summary.
9. If Team Lead sends review feedback, revise in the same cloned repo and same branch, update the existing PR, and expect Team Lead to mirror key review comments back to Jira.

All steps are required. Skipping Jira/PR steps is not acceptable.
