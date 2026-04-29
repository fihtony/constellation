# UI Design Agent Default Workflow

## Purpose

This workflow defines how the UI Design agent resolves design lookups and returns normalized design context to the rest of Constellation.

## Stages

1. Validate Input: confirm source system and explicit design target.
2. Fetch Metadata: load project, file, page, or screen context.
3. Fetch Detail: retrieve the exact node, frame, screen, or asset requested.
4. Normalize Output: summarize the result into stable internal fields.
5. Report: return structured references and any downloaded artifact paths.

## Checkpoints

- Never hide ambiguity in target selection.
- Never return raw provider output without normalization.
