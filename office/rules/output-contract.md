# Office Agent Output Contract

## Artifact Schema

Every Office Agent artifact must include:

```json
{
  "name": "<office-summary|office-analysis|office-organize-report|office-preflight-report>",
  "artifactType": "text/plain",
  "parts": [{"text": "<markdown content>"}],
  "metadata": {
    "agentId": "office-agent",
    "capability": "<office.document.summarize|office.folder.summarize|office.data.analyze|office.folder.organize>",
    "taskId": "<office task id>",
    "orchestratorTaskId": "<compass task id>"
  }
}
```

## Output Files

### Workspace Mode

All outputs land under `artifacts/workspaces/<task-id>/office-agent/`:

| File | Purpose |
|------|---------|
| `summary.md` | Document or folder summary (Markdown) |
| `analysis.md` | Data analysis report (CSV/Excel tasks) |
| `organized-output/files/` | Organize task canonical output root |
| `organized-output/.office-agent-manifest.json` | Execution manifest |
| `operations-plan.json` | Validated plan, persisted BEFORE writes |
| `warnings.md` | Partial-failure warning summary |
| `stage-summary.json` | Phase log with runtime configuration |
| `command-log.txt` | Timestamped operation log |

### In-Place Mode

Final result files are written to the user directory; audit files remain in the workspace:

- User directory: `summary.md`, `analysis.md`, or `organized-output/files/` + `.office-agent-manifest.json`
- Workspace: `command-log.txt`, `stage-summary.json`, `operations-plan.json`, `warnings.md`

## LLM Response Schema

### Summarize / Analyze

```json
{
  "summary_markdown": "<Markdown report>",
  "warnings": ["<optional warning strings>"]
}
```

### Organize

```json
{
  "actions": [
    {"action": "mkdir", "destination": "files/<path>"},
    {"action": "write_text", "destination": "files/<path>", "content": "<text>"},
    {"action": "write_fragment", "destination": "files/<path>", "fragment_id": "<id>"}
  ],
  "summary_markdown": "<optional summary>",
  "warnings": ["<optional warning strings>"]
}
```

Only `mkdir`, `write_text`, and `write_fragment` actions are accepted. All destinations must be relative paths under `files/`.
