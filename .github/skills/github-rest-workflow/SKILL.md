---
name: github-rest-workflow
description: >
  GitHub REST API workflow for the SCM agent: repo search/inspect, branch list/create,
  git push via HTTPS with token-embedded URL, pull request create/get/list/comment.
  Use when implementing or testing GitHub REST API flows with GitHubProvider.
user-invocable: true
---

# GitHub REST API Workflow

## When To Use

- Search or inspect a GitHub repository by owner/repo slug.
- List or create branches via the GitHub REST API.
- Push files (commit + push) to a branch via git over HTTPS.
- Create or inspect pull requests.
- Post general or inline comments on pull requests.
- Test the SCM agent REST back-end end-to-end.

## Authentication

**Fine-grained Personal Access Token** (recommended):
- Create at: https://github.com/settings/tokens?type=beta
- Required repository permissions:
  - **Contents** → Read and write  (git clone, git push, branch fallback)
  - **Pull requests** → Read and write  (PR CRUD + inline review comments)
  - **Issues** → Read and write  (general PR comments use Issues API)
  - **Metadata** → Read-only  (automatically included)

**Classic PAT** (simpler, broader scope):
- Scope: `repo` (covers all SCM agent operations)

Set `SCM_TOKEN=<token>` in `scm/.env`.

Constellation runtime isolation rule:
- SCM agent git subprocesses must use only `SCM_TOKEN`-derived auth (`http.extraHeader` or tokenized HTTPS URL).
- Agent-side git commands are isolated from host credential helpers and host keychains; do not depend on macOS Keychain, `gh auth`, or user-level `~/.gitconfig` when validating container behavior.
- `GH_TOKEN`, `GITHUB_TOKEN`, and other ambient host GitHub credentials are not valid inputs for Constellation agents. Use only the dedicated token stored in `scm/.env`.
- Launchers or tests that intentionally inject file-backed credentials into child processes must mark them with `CONSTELLATION_TRUSTED_ENV=1`; this flag is only for values that already came from `scm/.env`, `common/.env`, or `tests/.env`.

## Provider Selection

```env
SCM_PROVIDER=github          # (default)
SCM_BACKEND=rest             # (default when SCM_PROVIDER=github)
```

Only `SCM_TOKEN` is required. No other config needed for GitHub.com.

## API Base URL

```
https://api.github.com
```

Every request must include:
```
Authorization: Bearer <SCM_TOKEN>
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
```

## Capability Map

| Constellation Skill | REST Call |
|---|---|
| `scm.repo.search` | `GET /search/repositories?q=<query>` |
| `scm.repo.inspect` | `GET /repos/{owner}/{repo}` |
| `scm.branch.list` | `GET /repos/{owner}/{repo}/branches` |
| `scm.branch.create` | `POST /repos/{owner}/{repo}/git/refs` → 403 fallback: git push |
| `scm.git.push` | git clone + commit + push via HTTPS (token in URL) |
| `scm.git.clone` | git clone via HTTPS (http.extraHeader) |
| `scm.pr.create` | `POST /repos/{owner}/{repo}/pulls` |
| `scm.pr.get` | `GET /repos/{owner}/{repo}/pulls/{number}` |
| `scm.pr.list` | `GET /repos/{owner}/{repo}/pulls?state=open` |
| `scm.pr.comment` | `POST /repos/{owner}/{repo}/issues/{number}/comments` |
| `scm.pr.comment.list` | `GET /repos/{owner}/{repo}/issues/{number}/comments` |

## Branch Creation — Fine-grained PAT Fallback

Some fine-grained PATs cannot use `POST /git/refs` (returns 403).
`GitHubProvider.create_branch()` automatically falls back to:

```python
git clone --depth=1 --branch <from_ref> https://x-access-token:<token>@github.com/<owner>/<repo>.git /tmp/...
git push https://x-access-token:<token>@... HEAD:refs/heads/<branch>
```

**Do NOT mix `http.extraHeader=Authorization` and token-in-URL on the same remote.**
Using both simultaneously sends conflicting Basic + Bearer headers that GitHub rejects.
`GitHubProvider._run_git_url_auth()` runs git without `http.extraHeader` when the
clone URL already contains the token.

## Git Push (scm.git.push)

`push_files()` always uses `_authed_clone_url()` (token embedded in URL) and
calls `_run_git_url_auth()` throughout — never `_run_git()`.

```python
clone_url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git"
# All git operations use this URL directly for push
git push clone_url HEAD:refs/heads/{branch}
```

## PR Create — Structured Payload

Pass `prPayload` in `message.metadata` to avoid text-parsing issues:
```json
{
  "metadata": {
    "prPayload": {
      "owner": "org",
      "repo": "my-repo",
      "fromBranch": "feature/my-branch",
      "toBranch": "main",
      "title": "My PR title",
      "description": "PR body text"
    }
  }
}
```

## PR Comment — General vs. Inline

- **General comment** (most common): `POST /repos/{owner}/{repo}/issues/{number}/comments`
  - `GitHubProvider.add_pr_comment(owner, repo, pr_id, text)` — no `file_path` / `line`
- **Inline review comment**: `POST /repos/{owner}/{repo}/pulls/{number}/comments`
  - Requires `commit_id`, `path`, `line` — only use when the review context is known

## Running Tests

```bash
# Direct provider tests (no running agent needed)
python3 tests/test_github_rest.py -v

# A2A agent tests (requires running SCM agent)
python3 tests/test_scm_agent.py --agent-url http://localhost:8020

# Container agent tests
python3 tests/test_scm_agent.py --agent-url http://localhost:8020
```

All GitHub REST tests must read credentials only from `tests/.env` (`TEST_GITHUB_TOKEN`). Do not rely on shell-exported `GH_TOKEN`, `GITHUB_TOKEN`, macOS Keychain entries, or `gh auth` state.

## Common Errors

| Error | Cause | Fix |
|---|---|---|
| `create_failed_403` on `/git/refs` | Fine-grained PAT restriction | Automatic fallback to git push |
| `remote: invalid credentials` | Token in URL AND `http.extraHeader` both set | Use `_run_git_url_auth()` (no extraHeader) when token is in URL |
| `Permission denied to <user>` | Token has no Contents write permission | Add Contents: Read and write to fine-grained PAT |
| `Bad credentials` | Stale token in container | `docker compose up -d --force-recreate scm` |
| `clone_failed` in `push_files` | Wrong branch name as `base_branch` | Sanitize Jira-key branch names to `"main"` |
