# Jira Agent Default Workflow

## Purpose

This workflow defines how the Jira agent resolves explicit ticket requests, performs optional mutations, and returns structured results.

## Stages

1. Validate Input: confirm ticket keys, project scope, or search criteria.
2. Fetch Context: read the current issue state or search result set.
3. Mutate When Requested: apply the approved write action.
4. Verify Result: confirm the issue now reflects the intended state.
5. Report: return normalized output and mutation evidence.

## Checkpoints

- Never mutate Jira state without an explicit target.
- Never claim success without confirming the resulting issue state.
