# Office Organize — Dimension-Driven

The organize capability is dimension-driven. The grouping dimension
must come from the user (via `metadata.organizeGroupBy` or a generic
keyword in the request). The agent never invents a dimension.

## Workflow

1. **Resolve the dimension**
   - Read `metadata.organizeGroupBy` first.
   - Otherwise scan the user request for a generic keyword (size, type,
     created_time, modified_time, accessed_time, filename).
   - If neither source supplies a recognized dimension, return a
     structured `needs_clarification` payload and stop. Do not guess.

2. **Run the matching dimension tool**
   - `organize_by_size`
   - `organize_by_type`
   - `organize_by_created_time`
   - `organize_by_modified_time`
   - `organize_by_accessed_time`
   - `organize_by_filename`

   Each tool is zero-LLM: it walks the source tree, buckets files by
   the dimension, copies them under
   `organized-output/files/<bucket>/<file>`, and writes
   `organization-plan.md` with the bucket rules and a
   `Source Path | Destination` table.

3. **Verify the layout**
   - The plan-output gate already checks that every non-hidden source
     file is materialized exactly once. No business-specific bucket
     vocabulary is assumed.

## Bucket Naming

Bucket names come from the dimension tool. Examples:
- size: `small/`, `medium/`, `large/`
- type: `documents/`, `text/`, `data/`, `images/`, `presentations/`, `code/`, `other/`
- filename: `A/`, `B/`, …, `Z/`, `_other/`
- time-based dimensions: `YYYY-MM/` (e.g. `2026-01/`)

Never invent bucket names such as `students/`, `by-entity/`, or any
business-specific folder. The agent does not know which population's
documents it is processing.

## Output Format (`organization-plan.md`)

```
# Folder Organization Plan (dimension: <dimension>)

## Bucket rules
- <bucket>: <rule>

## Files Organized
| Source Path | Destination |
| --- | --- |
| <rel> | <bucket>/<rel> |
```

The `Source Path | Destination` table is the authoritative plan-output
contract used for validation.