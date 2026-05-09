# Team Lead Agent — Task Intake

When a new task arrives, gather context in this order before planning:

1. **Parse the request** — extract task type, platform hints, Jira key, design URL, repo URL, and acceptance criteria.
2. **Fetch Jira ticket** (if key present) — extract: summary, description, acceptance criteria, reporter, labels, priority.
3. **Fetch design context** (if Figma/Stitch URL present) — extract: screen names, component specs, layout notes.
4. **Inspect repository** (if repo URL present) — extract: tech stack, existing architecture, open PRs on related branches.
5. **Determine platform** — use gathered context to set platform (android/ios/web/unknown).
6. **Check for missing info** — only ask the user if platform is still unknown or acceptance criteria are incomplete.

## Intake Output Format

```json
{
  "task_type": "feature|bug_fix|improvement|question|other",
  "platform": "android|ios|web|unknown",
  "jira_key": "PROJ-123 or null",
  "repo_url": "https://... or null",
  "design_url": "https://... or null",
  "acceptance_criteria": ["..."],
  "missing_info": ["..."],
  "question_for_user": "null or a single clear question"
}
```
