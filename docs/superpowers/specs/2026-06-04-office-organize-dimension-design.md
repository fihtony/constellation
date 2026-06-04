# Office Agent — Dimension-Driven Organization (2026-06-04)

## Background

The Office agent is supposed to be a generic, schema-driven agent that
handles arbitrary office work. In practice, the `organize` capability
encodes business-specific assumptions about how a folder *should* be
grouped — most visibly a bias toward "people/identity" grouping that
breaks ordinary requests such as *"organize this folder by file size"*.

The proximate cause of the f460933b2801 failure is a chain of hardcoded
behaviours:

- `VALID_CATEGORIES` in `agents/office/office_tools.py` includes
  `"students"` and the organize path treats that as a valid bucket.
- `_extract_primary_entity` and `IDENTITY_PREFIXES` cause the agent to
  re-derive "Student Yan" / "Student Liam" as the high-confidence
  `primary_entity` for any text file with a `>>> Student XXX` header.
- `OrganizeMoveFileTool` enforces the destination tail to match that
  entity when `primary_entity_confidence == "high"`.
- The organize prompt coaches the LLM to use `primary_entity` as the
  grouping key, with examples such as
  `entities/Entity_A/YYYY-MM/...`.

The deeper cause is a methodology gap: the agent was never required to
ask *what dimension* the user wanted, and so it fell back to whatever
the metadata hinted at.

This spec removes every business-specific hardcode in the office agent
and replaces the LLM-bias with a deterministic dimension-driven
contract. The Office agent remains a generic agent — no specific test
fixture, dataset, or test-case wording is referenced anywhere in code,
prompts, skills, or tests after this change.

## Goals

1. **Generic, dimension-driven `organize`.** The agent groups files
   along a user-specified dimension; it never invents a dimension.
2. **Six built-in dimensions, all zero-LLM and deterministic:**
   `size`, `type`, `created_time`, `modified_time`, `accessed_time`,
   `filename`.
3. **Fail-closed clarification when the dimension is missing.** The
   agent must surface a structured `needs_clarification` payload and
   stop, never silently guess.
4. **No business hardcodes anywhere in the Office agent.** No
   `students`, no `by-student`, no `primary_entity`, no `ID-prefix =
   student/author/writer/...`. The agent does not know which
   population's essays it is processing.
5. **The plan-output gate stays dimension-agnostic.** It continues to
   verify the contract (every source file materialized exactly once)
   without baking in any specific bucket vocabulary.

## Non-Goals

- Not changing `summarize` or `analyze` capabilities beyond a
  hardcode audit (they already operate on inferred schema; we only
  confirm and remove any latent business wording).
- Not changing the `compass` agent's user-facing clarification UI
  here. We deliver a structured `needs_clarification` payload and
  let the orchestrator surface it.
- Not introducing new environment variables or new configuration
  files. The dimension is read from existing metadata
  (`metadata.organizeGroupBy`) or from user text.
- Not adding a new plan-output gate retry budget or a new retry loop.
  The dimension contract is enforced up front (in
  `analyze_request`); once materialized, the existing gate runs.

## Architecture

### Two resolution paths for `organizeGroupBy`

```
compass (or external caller)
  └─ A2A message { metadata: { capability: "organize", organizeGroupBy: ? } }
       └─ office.handle_message
            └─ receive_task  ──► dimension (organizeGroupBy | keyword | empty)
                 └─ analyze_request
                      ├── dimension == "" → fail_closed: needs_clarification
                      │                       payload, abort task
                      └── dimension ∈ {size, type, created_time,
                          modified_time, accessed_time, filename}
                          └─ execute_office_work
                               ├── tool match: organize_by_{dimension}
                               │   (zero-LLM, deterministic)
                               └── fallback: agentic LLM plan
                                   (still dimension-aware, still gated)
                            └─ plan-output gate (unchanged contract)
                       └─ report_result
```

### Dimension contract

`VALID_DIMENSIONS = {"size", "type", "created_time", "modified_time",
"accessed_time", "filename"}` lives in
`agents/office/dimensions.py`. Both the metadata reader and the
keyword reader must round-trip through this set.

Resolution order (first non-empty wins):

1. `metadata.organizeGroupBy` (lower-cased, validated against
   `VALID_DIMENSIONS`).
2. Keyword scan over `user_request` using a neutral multilingual
   mapping (English + Chinese + a small set of common synonyms; no
   test-case-specific phrases).
3. If both are empty: `dimension = ""`, triggers clarification.

### Built-in dimension tools

Six new tools, registered alongside `organize_folder` /
`organize_move_file`. Each is a deterministic function (no LLM), runs
on the in-process runtime, and produces both `organization-plan.md`
and the materialized `organized-output/files/<bucket>/<file>` tree.

| Tool | Bucket key source | Bucket naming | Stats used |
|---|---|---|---|
| `organize_by_size` | quartile thresholds computed over the source tree's real sizes | `small/`, `medium/`, `large/` | `stat.st_size` |
| `organize_by_type` | `_categorize_extension` result | `documents/`, `text/`, `data/`, `images/`, `presentations/`, `code/`, `other/` | extension |
| `organize_by_created_time` | local-tz `YYYY-MM` derived from birth time | `2026-01/`, … | `stat.st_birthtime`, fallback `st_mtime` with `inferred_from=mtime` flag in plan |
| `organize_by_modified_time` | local-tz `YYYY-MM` from mtime | `2026-01/`, … | `stat.st_mtime` |
| `organize_by_accessed_time` | local-tz `YYYY-MM` from atime | `2026-01/`, … | `stat.st_atime` |
| `organize_by_filename` | uppercase first character of basename (letters only) | `A/`, `B/`, …, `Z/`, `_other/` | filename only |

Every dimension tool writes `organization-plan.md` containing:

- A "Bucket rules" section that lists the threshold / rule used and
  the file count per bucket.
- A canonical `Source Path | Destination` table covering every
  non-hidden source file.
- An "Assumptions" section for caveats (e.g. `created_time` falling
  back to `mtime` on filesystems without birthtime).

### Clarification payload

When `dimension == ""` and `capability == "organize"`,
`analyze_request` returns:

```python
{
    "error": "missing_organize_dimension",
    "needs_clarification": {
        "missing": "organizeGroupBy",
        "options": [
            {"id": "size", "label": "File size"},
            {"id": "type", "label": "File type / extension"},
            {"id": "created_time", "label": "Created time"},
            {"id": "modified_time", "label": "Modified time"},
            {"id": "accessed_time", "label": "Accessed time"},
            {"id": "filename", "label": "Filename first character"},
        ],
        "user_message": (
            "Office organize needs a grouping dimension. "
            "Available dimensions: size, type, created_time, modified_time, "
            "accessed_time, filename."
        ),
    },
}
```

`OfficeAgent.handle_message` upgrades this into a `failed` task with
the structured payload attached to the task metadata, and the
callback to the orchestrator (compass or A2A peer) carries the same
payload. The agent **does not** continue execution and **does not**
invent a default.

## Components

### `agents/office/dimensions.py` (new)

Pure-Python module exporting:

- `VALID_DIMENSIONS: frozenset[str]`
- `parse_dimension(metadata, user_text) -> str` (returns one of
  `VALID_DIMENSIONS` or `""`).
- `KEYWORD_TO_DIMENSION: dict[str, str]` mapping neutral
  multilingual keywords. No test-specific phrases.

Keyword mapping is built from generic terms:

- size: `size`, `file size`, `by size`, `大小`, `按大小`, `按文件大小`
- type: `type`, `file type`, `extension`, `by type`, `类型`, `扩展名`
- created_time: `created time`, `creation time`, `ctime`, `birthtime`,
  `创建时间`, `按创建时间`
- modified_time: `modified time`, `mtime`, `last modified`, `修改时间`,
  `按修改时间`
- accessed_time: `accessed time`, `atime`, `last access`, `访问时间`,
  `按访问时间`
- filename: `filename`, `name`, `by name`, `按文件名`, `按名称`

### `agents/office/organize_by_dimension.py` (new)

Holds the six deterministic tool classes plus their shared
helpers (bucket name normalization, plan-writer, copy executor).
Each tool returns a `ToolResult` that the existing
`execute_office_work` step wires into the same gate as the
agentic-runtime path. A single entry point
`run_dimension_tool(dimension, source_root, output_root, output_mode)`
exists for the bounded (zero-LLM) path inside
`execute_office_work`, mirroring `_run_bounded_folder_summarize`.

### `agents/office/office_tools.py` (modified)

- **Delete** `VALID_CATEGORIES`, `WRAPPER_PREFIXES`,
  `IDENTITY_PREFIXES`, `_extract_primary_entity`,
  `_clean_entity_candidate`, `_looks_like_person_name`,
  `primary_entity`-bearing fields in
  `collect_organize_file_inventory` / `_build_file_metadata`.
- **Replace** `_normalize_organized_path` / `_is_under_organized_output`
  to only check the `organized-output/files/` prefix; no business
  category allowlist.
- **Replace** the `OrganizeMoveFileTool` confidence-check block with
  a generic containment + symlink check (no `primary_entity` reading).
- **Add** six new `organize_by_*` tool classes (one per dimension),
  registered via the same `register_office_tools` hook.
- **Keep** `_categorize_extension` and the `type` dimension mapping
  to `documents/text/data/images/presentations/code/other`. Remove
  any "students"-style residue.

### `agents/office/nodes.py` (modified)

- **In `analyze_request`:** when `capability == "organize"` and
  `dimension == ""`, return the `needs_clarification` payload (see
  Architecture). Otherwise validate paths and pass the dimension
  forward in state.
- **In `execute_office_work`:** branch on `dimension`:
  - When a matching `organize_by_*` tool is registered, call the
    bounded entry point instead of the agentic runtime, mirroring
    `_try_bounded_office_flow` for `summarize`.
  - When no dimension tool is registered (e.g. legacy runtime) or
    the LLM must still run, fall through to the existing
    `runtime.run_agentic` path with the dimension rewritten into the
    prompt and the tool allowlist.
- **In `_build_organize_prompt`:** drop all `primary_entity` /
  `entities/Entity_A/...` wording. Replace with a "use the dimension
  specified in `state['organize_dimension']`" sentence and reference
  the matching `organize_by_*` tool by name.
- **In `_canonical_organize_destination`:** remove the
  `primary_entity` / `date_bucket` / `category` auto-path logic.
  Use the destination recorded in the plan (or, for the bounded
  path, the destination computed by the dimension tool).
- **In `_expected_output_paths` and `_verify_organize_materialization`:** keep contract-level checks; bucket names are
  read from the plan rather than from a hardcoded set.

### `agents/office/prompts/organize.md` (rewritten)

- Removed: any mention of `primary_entity`, `entities/...`, business
  groupings.
- Added: explicit instruction to read
  `metadata.organizeGroupBy` and to fall back to a small set of
  generic keywords. Explicit instruction to return
  `needs_clarification` when neither source supplies a dimension.
- Added: tool list showing the six `organize_by_*` tools.

### `agents/office/prompts/system.md` (extended)

Add a single rule:

> "For the `organize` capability, the agent MUST NOT invent a
> grouping dimension. If neither the metadata nor the user text
> supplies one, the agent must return a structured
> `needs_clarification` payload and stop."

### `skills/office-generic-methodology/instructions.md` (modified)

Phase 3.C ("For Organization Tasks") is rewritten to:

1. Identify the dimension from user text + metadata.
2. If the dimension is missing, return `needs_clarification` and
   stop.
3. Use the dimension-specific tool to materialize the layout and
   the `organization-plan.md` with explicit bucket rules.
4. Never introduce business-specific folder names (no
   `students/`, no `by-entity/`, etc.).

The validation checklist in Phase 5 gains one item:

- "The chosen grouping dimension matches the user request."

### `tests/unit/agents/test_office_organize_schema.py` (modified)

Replace every `students/`, `Liam/`, `Ethan/`, `Yan/`, and
`by-student/` literal in assertions with neutral
`<bucket>/<file>` examples. The behaviour under test (path
normalization, wrapper stripping) is preserved; only the
identifiers change.

### `tests/unit/agents/test_office_organize_verification.py` (modified)

Same as above. The plan-output gate contract test keeps the
invariant "every source file is materialized exactly once" but
uses synthetic bucket names (e.g. `small/`, `medium/`, `large/`).

### `tests/unit/agents/test_office_organize_dimensions.py` (new)

Covers:

- Each of the six dimension tools produces the right buckets for a
  synthetic fixture (e.g. mixed sizes → `small/medium/large/`,
  mixed extensions → `documents/…`, etc.).
- `parse_dimension` resolves correctly for both metadata and
  keyword sources, and returns `""` for ambiguous or empty input.
- `analyze_request` returns the `needs_clarification` payload
  when the dimension is missing for `capability == "organize"`.
- The `organize` prompt no longer references `primary_entity`
  (string-level assertion against the rendered prompt).
- Bucket thresholds appear in the generated
  `organization-plan.md` (e.g. `small: < 256 B (3 files)`).

## Data Flow

### Happy path (size)

1. User: "请按文件大小整理 `/path/to/dir`".
2. compass → office: `metadata.organizeGroupBy` is empty, but
   `parse_dimension` matches `大小` → `size`.
3. `analyze_request` validates the path and forwards
   `dimension="size"`.
4. `execute_office_work` matches the `organize_by_size` tool and
   runs the bounded path.
5. `organization-plan.md` lists three buckets with thresholds
   derived from the real size distribution; copies are made under
   `organized-output/files/small|medium|large/`.
6. plan-output gate runs unchanged; passes.
7. `report_result` writes `task-report.json`; callback fires.

### Clarification path

1. User: "整理 `/path/to/dir`" (no dimension).
2. compass → office: `metadata.organizeGroupBy` empty, no
   keyword match → `dimension == ""`.
3. `analyze_request` returns
   `error="missing_organize_dimension"` plus
   `needs_clarification` payload. The task is failed
   immediately; no LLM is called.
4. Compass (or another orchestrator) reads the structured payload
   and prompts the user.

### Legacy / agentic fallback path

1. User explicitly asks for an unusual grouping the six built-in
   tools cannot express.
2. compass → office: `metadata.organizeGroupBy="custom"` (not in
   `VALID_DIMENSIONS`). `parse_dimension` returns `""` after
   validation.
3. The clarification path triggers unless the orchestrator agrees
   to drop the dimension and let the LLM decide. We do not provide
   such a fallback in this spec — the LLM is *not* an
   alternative decision-maker for the dimension itself. If the
   dimension is unsupported, we say so explicitly.

## Error Handling

- **Missing dimension (organize)**: `analyze_request` fails the
  task with `error="missing_organize_dimension"` and
  `needs_clarification` payload. The agent does not continue.
- **Unsupported dimension (after validation)**: same as missing
  (the keyword reader only maps to `VALID_DIMENSIONS`).
- **`OFFICE_SOURCE_ROOT` / `OFFICE_WORKSPACE_ROOT` missing**:
  preserved behaviour.
- **Filesystem without `st_birthtime`**: `organize_by_created_time`
  falls back to `st_mtime` and records
  `inferred_from: mtime` in the plan's assumptions section.
- **Bucket collisions (e.g. two files both mapping to `A/`)**:
  handled by `_safe_path_segment` on the basename; the
  `Source Path | Destination` table makes collisions auditable.
- **Container vs local execution**: the new tools use only
  `os`, `pathlib`, `shutil`, and `stat` calls that already work
  in both contexts. No new environment variables.

## Testing

### Unit tests (in `tests/unit/agents/`)

- `test_office_organize_dimensions.py` (new) — six tools,
  `parse_dimension`, `analyze_request` clarification path,
  prompt no-longer-references-`primary_entity` assertion, plan
  threshold rendering.
- `test_office_organize_schema.py` (modified) — replace
  `students/`, `by-student/` literals with neutral bucket names;
  behaviour unchanged.
- `test_office_organize_verification.py` (modified) — same.
- `test_office_plan_output_gate.py` (existing) — unchanged; the
  gate still has zero business hardcodes.

### Integration / E2E

- A new integration test (`tests/integration/`) drives a
  synthetic task through office with `organizeGroupBy=size` and
  asserts:
  1. `organized-output/files/small|medium|large/` exist with the
     expected counts.
  2. `organization-plan.md` lists thresholds and a
     `Source Path | Destination` table covering every source
     file.
  3. `task-report.json` reports `success=true`.
- A second integration test sends an `organize` task with no
  dimension and asserts the task ends in `failed` with the
  `needs_clarification` payload attached.

### Local + container

All new tools use the same in-process and A2A paths as the
existing office agent, so the existing local and container
launch scripts apply unchanged. The `Dockerfile` for `office`
needs no rebuild for code changes only.

## Out of Scope

- A new "smart dimension suggester" LLM call. The agent never
  invents a dimension.
- Multi-dimensional grouping (e.g. group by size *and* type in
  one pass). Future work.
- Changing compass to drive a clarification round-trip. The
  orchestrator can read the structured payload as it does any
  other `error` field.
- Replacing `OrganizeMoveFileTool`. The move tool remains the
  way the LLM-path (when used) materializes files; the new
  dimension tools simply call into it.

## Open Questions

None at spec time. Implementation will discover concrete test
fixture naming, but no business-specific identifiers are
expected to leak into the codebase.
