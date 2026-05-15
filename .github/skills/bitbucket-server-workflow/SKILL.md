---
name: bitbucket-server-workflow
description: 'Bitbucket Server workflow for repo search, repo URL resolution, project repo listing, branch creation, git push over HTTPS bearer auth, pull request creation, pull request URL parsing, pull request listing/detail lookup, merge preparation, linked Jira issue extraction, inline PR comments, PR comment listing, and duplicate-comment checks. Use when implementing or testing Bitbucket agent flows against your target project and repo.'
user-invocable: true
---

# Bitbucket Server Workflow

## When To Use

- Search a repository from a natural-language query inside a Bitbucket Server project.
- Resolve a full browse URL into a normalized repo payload.
- List repositories in a Bitbucket Server project.
- Create a branch in Bitbucket Server.
- Push a real commit over Git HTTPS using the Bitbucket token from `bitbucket/.env`.
- Raise a pull request.
- Parse a Bitbucket pull request URL into project/repo/id fields.
- Inspect a pull request or list open pull requests.
- Extract linked Jira issue keys from PR titles, descriptions, and source branch names.
- Merge a pull request when the user explicitly asks and merge checks pass.
- Add an inline comment on a changed line in a pull request.
- List existing PR comments and check for duplicate inline/general comments before posting.

## Authentication

- REST API: send `Authorization: Bearer <BITBUCKET_TOKEN>`.
- Git over HTTPS: use `git -c http.extraHeader="Authorization: Bearer <BITBUCKET_TOKEN>"`.
- Do not assume `x-token-auth` basic auth works in this environment.

## Runtime Packaging

- `bitbucket/app.py` reads this `SKILL.md` at runtime and injects it into the LLM prompt inside `process_message()`.
- When the Bitbucket agent runs in Docker, the image must contain `.github/skills/bitbucket-server-workflow/SKILL.md`.
- The current `bitbucket/Dockerfile` copies `.github/skills/` into `/app/.github/skills/` so prompt injection works in containers as well as local runs.

## URLs

- REST root: `https://bitbucket.example.com/rest/api/1.0`
- Git clone URL pattern: `https://bitbucket.example.com/scm/<project-lower>/<repo>.git`
- Browse URL pattern: `https://bitbucket.example.com/projects/<PROJECT>/repos/<repo>/browse`

## Repo Search

1. List repos in the target project.
2. Score the repo by normalized tokens from the user query.
3. Return both browse URL and Git clone URL.

## Branch And Push Workflow

1. Resolve the target repo and base branch.
2. Create a unique feature branch.
3. Clone the repo with bearer auth over HTTPS.
4. Check out the feature branch from the base branch.
5. Write the test file under a dedicated path such as `agent-tests/...`.
6. Commit with an explicit message.
7. Push the branch to origin.

## Pull Request Workflow

1. Create the PR from the feature branch to the base branch.
2. Capture the PR id and self URL.
3. Read the PR back and surface `linkedJiraIssues` from the title, description, and branch name.
4. Use the PR diff endpoint before posting inline comments.

## Pull Request Inspection Workflow

1. Use `GET /bitbucket/pull-requests/{id}?project=MYPROJECT&repo=sample-app` to inspect one PR.
2. Use `GET /bitbucket/pull-requests?project=MYPROJECT&repo=sample-app&state=OPEN` to list PRs.
3. Prefer the `pullRequest` or `pullRequests` summaries for workflow routing; keep the raw Bitbucket payload only when troubleshooting.
4. Treat `linkedJiraIssues` as hints derived from repo metadata, not as proof that the Jira issue is valid.

## Merge Workflow

1. Fetch the PR first to obtain its current `version`.
2. Merge only on explicit user intent.
3. Use `POST /bitbucket/pull-requests/{id}/merge` and pass the current version when needed.
4. If merge checks reject the request, surface the failure and stop; do not force alternative write paths.

## Useful Bitbucket Agent Endpoints

- `GET /bitbucket/repos?project=MYPROJECT`
- `GET /bitbucket/repo-url?q=https://bitbucket.example.com/projects/MYPROJECT/repos/sample-app/browse&project=MYPROJECT`
- `GET /bitbucket/search/repos?q=sample+app&project=MYPROJECT&limit=5`
- `GET /bitbucket/branches?project=MYPROJECT&repo=sample-app`
- `POST /bitbucket/branches`
- `POST /bitbucket/git/push`
- `POST /bitbucket/pull-requests`
- `GET /bitbucket/pull-requests/parse?url=https://bitbucket.example.com/projects/MYPROJECT/repos/sample-app/pull-requests/123`
- `GET /bitbucket/pull-requests/{id}?project=MYPROJECT&repo=sample-app`
- `GET /bitbucket/pull-requests?project=MYPROJECT&repo=sample-app&state=OPEN&limit=25`
- `GET /bitbucket/pull-requests/{id}/comments?project=MYPROJECT&repo=sample-app`
- `POST /bitbucket/pull-requests/{id}/merge`
- `POST /bitbucket/pull-requests/comments`
- `POST /bitbucket/pull-requests/comments/check-duplicates`

## Inline Comment Workflow

1. Fetch the structured PR diff from `/pull-requests/{id}/diff`.
2. Find the target file path and destination line inside the diff hunks.
3. Build the anchor with:
   - `line`
   - `lineType`
   - `fileType: TO`
   - `path` as a string
   - `diffType: EFFECTIVE`
   - `fromHash` and `toHash` when available
4. Post the comment to `/pull-requests/{id}/comments`.
5. If the inline anchor is rejected, fall back to a general PR comment.

## Comment Safety Workflow

1. Read existing PR comments through `/pull-requests/{id}/comments` when you need to avoid duplicate posts.
2. Use `POST /bitbucket/pull-requests/comments/check-duplicates` with the proposed text and optional `filePath`/`line` before bulk posting.
3. Treat a stale-version merge check as a safe way to validate merge-route wiring without merging into the default branch.

## Test Scope

- Use the project and repo values from `tests/.env` (`TEST_SCM_REPO_URL`, `TEST_SCM_TOKEN`) for all live Bitbucket Server integration tests.
- The integration regression (`tests/test_scm_agent.py`) covers all 21 TCs:
  - TC-01  Git auth: `git ls-remote` with Bearer token
  - TC-02  Health: `/health`
  - TC-03  Agent card: `/.well-known/agent-card.json`
  - TC-04  Repo inspect: `GET /scm/repo`
  - TC-05  Branch list: `GET /scm/branches`
  - TC-06  Branch create: `POST /scm/branches`
  - TC-07  File push: `POST /scm/git/push`
  - TC-08  PR create: `POST /scm/pull-requests`
  - TC-09  PR get: `GET /scm/pull-requests/{id}`
  - TC-10  PR list: `GET /scm/pull-requests`
  - TC-11  PR comment: `POST /scm/pull-requests/comments`
  - TC-12  PR comment list: `GET /scm/pull-requests/{id}/comments`
  - TC-13  Remote file read: `GET /scm/remote/file`
  - TC-14  Remote dir list: `GET /scm/remote/dir`
  - TC-15  Code search: `GET /scm/remote/search` (returns `not_supported` for Bitbucket)
  - TC-16  Ref comparison: `GET /scm/refs/compare`
  - TC-17  Default branch: `GET /scm/branch/default`
  - TC-18  Branch rules: `GET /scm/branch/rules`
  - TC-19  Inline PR comment: `POST /scm/pull-requests/comments` with `filePath`+`line` anchor
  - TC-20  A2A lifecycle: `POST /message:send` + `GET /tasks/{id}` poll to terminal state
  - TC-21  Git clone async: `POST /scm/git/clone` async task + poll
- Keep all test writes isolated under `agent-tests/`.
- Use unique branch names and PR titles so real test artifacts are traceable.
- Centralize the allowed repo and write root under `tests/agent_test_targets.py`.

## Reliability Notes

- Bitbucket Server may return HTTP 500 transiently under load. The `BitbucketProvider` retries
  up to 3 times (exponential back-off 2s, 4s) for all 5xx responses on REST API calls,
  `push_files` clone, `push_files` git-push, and `read_remote_file`.
- `test_scm_agent.py` TC-01 (`git ls-remote`) also retries up to 3 times for transient RPC 500s.
- The A2A task lifecycle (`_run_task_async`) transitions any task that is still
  `TASK_STATE_WORKING` after the agentic runtime loop exits to `TASK_STATE_COMPLETED`,
  ensuring the task always reaches a terminal state.