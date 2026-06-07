# Office Generic Methodology

Use this methodology for any office task. Do not assume fixed domains, fixed schemas, or fixed field names.

## Core Principles

1. Infer structure from observed data; never hardcode business-specific assumptions.
2. Choose tools by source type, not by task keywords.
3. Always state assumptions, confidence limits, and data quality caveats.
4. Keep output deterministic, auditable, and within authorized folders only.
5. Prefer authorized task metadata for source paths, capability, and output mode when it is available; do not depend on literal test paths or dataset-specific clues in the natural-language request.

## Phase 1: Task Framing

1. Identify requested outcome:
- Summary
- Analysis
- Organization
- Mixed output
2. Determine source set:
- Single file
- Multi-file set
- Folder tree
3. Determine output mode:
- `workspace`: write only with `write_workspace`
- `inplace`: write only with `write_file` under source root

## Phase 2: Source Profiling

For each source, inspect before interpreting:

- PDF: `read_pdf`
- DOCX: `read_docx`
- TXT/MD: `read_txt`
- CSV: `read_csv`
- XLSX: `read_xlsx`
- XLS: `read_xls`
- PPTX: `read_pptx`
- Folder: `list_directory` and/or `organize_folder`

Collect:
- Parseability status
- Detected fields/sections
- Approximate size/volume
- Missing/empty data signals

## Phase 3: Generic Reasoning Strategy

### A) For Analysis Tasks

1. Build inferred schema:
- Field names
- Candidate types (numeric, categorical, text, date-like, unknown)
- Missingness and parse caveats
2. Compute baseline statistics for numeric fields:
- Count, min, max, average
3. Build safe aggregations:
- Group categorical fields against numeric measures
- Prefer top-N summaries over exhaustive dumps
4. Report insights from observed distributions and relationships only.

### B) For Summary Tasks

1. Extract key sections/paragraphs/slides/pages.
2. Produce:
- Document info
- 3-5 key points
- Executive summary
3. If extraction is partial, say so clearly.

### C) For Organization Tasks

1. Identify the dimension from the user:
   - Read `metadata.organizeGroupBy` first.
   - Otherwise scan the user request for a generic keyword
     (size, type, created_time, modified_time, accessed_time,
     filename).
   - If no dimension is identified, return a structured
     `needs_clarification` payload and STOP. Never invent a
     dimension.
2. Use the matching dimension tool
   (`organize_by_size` / `organize_by_type` /
   `organize_by_created_time` / `organize_by_modified_time` /
   `organize_by_accessed_time` / `organize_by_filename`) to
   materialize the layout and write `organization-plan.md` with
   explicit bucket rules.
3. Bucket names come from the dimension tool. Never introduce
   business-specific folder names (no `students/`, no
   `by-entity/`, etc.).

## Phase 4: Output Contract

Every output should include:

1. What was read (source inventory)
2. What was inferred (schema/structure)
3. What was computed (statistics/groupings/summary logic)
4. Caveats and confidence boundaries

## Phase 5: Validation Checklist

Before finishing:

1. Confirm no writes happened outside authorized folder policy.
2. Confirm output filenames and locations match requested mode.
3. Confirm no credentials or secrets are included in output/log text.
4. Confirm no domain-specific field assumptions were hardcoded.
5. The chosen grouping dimension matches the user request.
