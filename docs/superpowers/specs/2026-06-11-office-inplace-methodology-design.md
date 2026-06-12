# Office Agent — In-Place Output Methodology (2026-06-11)

## Background

The Office agent already supports two output modes, `workspace` and
`inplace`. The user-side selection ("in place", "inplace", "in the
source", "原地" …) is correctly recognised by
`agents.compass.agent._scan_output_mode_from_text`, the dispatcher
mounts the user's source directory `RW` via
`agents.compass.tools._office_mount_plan`, and the office workflow
gates writes behind `OFFICE_ALLOW_INPLACE_WRITES`. The skill spec at
`.github/skills/office-agent-workflow/SKILL.md` already names the
in-place contract:

> **IN-PLACE mode** (source bind is read-write):
>   - Write `analysis.md` to the **same directory** as the target file.

Two specific bugs prevent this methodology from being honoured for the
first office task the user reports — *"please analyze the sales data
in /Users/aibot/projects/constellation/tests/data/csv/ folder and
create report"* with the user reply *"in place"*:

**Bug A — `_build_analyze_prompt` advertises the wrong target path for
directory inputs in `inplace` mode** (`agents/office/nodes.py:2066-2124`)

```python
# inplace branch
target_lines.append(f"- Source: {path}\n  Target path: {path}.analysis.md")
```

When the validated source is a directory like `/data`, this string
folds to `Target path: /data.analysis.md` — a **sibling** of the
directory, not a file *inside* it. The verification helper
`_expected_output_paths`, however, expects
`/data/data.analysis.md` (a *child* of the directory). The LLM
follows the prompt, the verifier looks for the file in the wrong
place, and the task is reported as failed even though the report was
written to a perfectly valid in-place location.

**Bug B — `_build_organize_prompt` ships a literal, un-interpolated
placeholder in `inplace` mode** (`agents/office/nodes.py:2133`)

```python
"3. Write the organization plan using write_file tool to: "
"{source_folder}/organization-plan.md"
```

The token `{source_folder}` is not inside an f-string; the LLM sees
the literal text `{source_folder}/organization-plan.md` and either
errors, ignores the directive, or writes the plan to an unintended
location. The verification step expects
`<source_dir>/organization-plan.md` and the two disagree.

**Root cause (methodology):** the prompt templates and the
verification helper both encode *"where should this deliverable
live?"* as **independent code paths**. Each capability (`analyze`,
`summarize`, `organize`) re-implements the same string formatting in
two different places, and the two implementations have drifted. The
right fix is to extract one helper, give it a name, and have both
sides consume it.

Two existing pieces of infrastructure already support the in-place
contract correctly and **do not need to change**:

- `OFFICE_SOURCE_ROOT` — the sandbox boundary that compass sets to
  `/app/userdata` (the parent of every `input-N` mount). Read by
  `agents/office/office_tools.py::_validate_path`.
- `OFFICE_ALLOWED_BASE_PATHS` — the colon-separated list of input
  mounts the LLM is allowed to touch. Also consumed by
  `_validate_path` and used by `WriteFileTool.execute_sync` as the
  second layer of the write grant. Both are already in place.

## Goals

1. **In-place delivers land where the methodology says they should.**
   For directory inputs, the final report and the organization plan
   live **inside** the source directory. The LLM prompt and the
   verifier agree by construction.
2. **One source of truth for "where does this deliverable go?"** The
   three prompt builders and `_expected_output_paths` all consume a
   single helper. New capabilities cannot repeat the same drift bug
   because the helper is the only place that knows the rule.
3. **No regression in `workspace` mode.** Every existing test under
   `tests/unit/agents/test_office_*` must still pass without
   modification, including `test_expected_output_paths_for_*` and the
   `test_office_*_prompt` families.
4. **No new permissions, no new mounts.** The existing
   `OFFICE_ALLOW_INPLACE_WRITES` switch and the existing
   `OFFICE_ALLOWED_BASE_PATHS` whitelist are sufficient; the
   methodology fix is purely on the *target-path computation* side.

## Non-Goals

- Not introducing a new write-file path whitelist layer. `write_file`
  already consults `_validate_path`, which already enforces both
  `OFFICE_SOURCE_ROOT` and `OFFICE_ALLOWED_BASE_PATHS`. Adding a
  third check would be redundant.
- Not changing the compass-side mount plan or any of the
  `extra_binds` structure. The mount is already `RW` for `inplace`
  and `RO` for `workspace`, which is exactly what the user asked
  for.
- Not changing `_run_bounded_folder_summarize` or
  `_run_bounded_folder_organize`. Those paths are deterministic and
  already produce correct deliverables; only the LLM-driven prompt
  path is wrong.
- Not touching `_run_custom_dimension_path`. Its output lives under
  `<source_dir>/organized-output/files/`, which is a different
  deliverable contract that is not in scope here.
- Not introducing any test-case-specific wording, dataset-specific
  paths, or business assumptions into the new helper. The helper
  remains generic over capabilities and source shapes.

## Design

### New module: `agents/office/output_paths.py`

Single source of truth for *"where does an office deliverable live?"*.

```python
# Public API

def target_for_source(
    output_mode: str,        # "workspace" | "inplace"
    source_path: str,        # file or directory, may be the
                             # container-translated path
    artifacts_dir: str,      # office workspace root
    filename: str,           # bare filename, e.g. "data.analysis.md"
) -> str:
    """Return the absolute path a deliverable should be written to.

    - workspace: <artifacts_dir>/<filename>
    - inplace  + file: <dir_of(file)>/<filename>
    - inplace  + dir : <dir>/<filename>
    """


def target_with_suffix(
    output_mode: str,
    source_path: str,
    artifacts_dir: str,
    suffix: str,             # e.g. ".analysis.md"
) -> str:
    """Convenience wrapper; filename = <basename(source_path)><suffix>."""


def all_targets_for_capability(
    capability: str,         # "analyze" | "summarize" | "organize"
    validated_paths: list[str],
    output_mode: str,
    artifacts_dir: str,
) -> list[str]:
    """All required deliverable paths for the current office task.

    This is the single point that the prompt builders and the
    verifier both consult. Adding a new capability means adding a
    branch here; nothing else needs to change.
    """
```

The directory-vs-file decision in `target_for_source` uses
`os.path.isdir(source_path)`. For the LLM-driven prompt path the
input has already been validated, so this is reliable. The helper
does not consult `OFFICE_SOURCE_ROOT`, `OFFICE_ALLOWED_BASE_PATHS`,
or any other env var — its only inputs are the parameters above.
That decoupling keeps the helper easy to test and keeps "where do we
write?" orthogonal to "is this path authorised?".

### Refactor `agents/office/nodes.py`

1. Replace the body of `_target_output_file` and
   `_target_output_path` with thin re-exports of the new helper, so
   every existing call site continues to work:

   ```python
   from agents.office.output_paths import (
       target_for_source as _target_output_file,
       target_with_suffix as _target_output_path,
       all_targets_for_capability as _expected_output_paths,
   )
   ```

2. Rewrite the three prompt builders to call the helper:

   ```python
   # _build_analyze_prompt, inplace branch
   for path in paths:
       target = target_with_suffix(
           output_mode, path, artifacts_dir, ".analysis.md"
       )
       target_lines.append(
           f"- Source: {path}\n  Target path: {target}"
       )
   ```

   The same pattern is applied to `_build_summarize_prompt` and
   `_build_organize_prompt`. The verifier and the prompt see the
   same path, by construction.

3. Fix Bug B by turning the `write_rules` line in
   `_build_organize_prompt` into a real f-string:

   ```python
   write_rules = (
       "3. Write the organization plan using write_workspace tool "
       "with filename: organization-plan.md"
       if output_mode == "workspace"
       else
       f"3. Write the organization plan using write_file tool to: "
       f"{validated_paths[0]}/organization-plan.md"
   )
   ```

   The path is whatever the same helper would compute for a
   directory input, so the prompt, the verifier, and (in the bounded
   folder organize path) the actual write all converge on
   `<source_dir>/organization-plan.md`.

### Data flow — first office task with `inplace`

```
User → "please analyze the sales data in
        /Users/.../tests/data/csv/ folder and create report"
  ↓ Compass classify → office.data.analyze
  ↓ Compass detect folder, ask "workspace or inplace?"
User → "in place"
  ↓ Compass _resolve_office_resume_reply("office_output_mode", "in place")
  → output_mode = "inplace"
  ↓ Compass _dispatch_office_task_via_launcher(output_mode="inplace")
  → mount_plan.extra_binds =
      ["/Users/.../tests/data/csv:/app/userdata/input-0:rw"]
  → mount_plan.env =
      OFFICE_SOURCE_ROOT=/app/userdata
      OFFICE_ALLOWED_BASE_PATHS=/app/userdata/input-0
      OFFICE_ALLOW_INPLACE_WRITES=true
  ↓ Office container (RW mount)
  → receive_task       output_mode=inplace
  → analyze_request    validated_paths=["/app/userdata/input-0"]
  → execute_office_work
      prompt  = _build_analyze_prompt(...)  # helper-derived
        - Source:   /app/userdata/input-0
        - Target path: /app/userdata/input-0/csv.analysis.md
      LLM writes the report to that path via write_file
        _validate_path accepts the target
        (inside OFFICE_SOURCE_ROOT and OFFICE_ALLOWED_BASE_PATHS)
      expected_outputs =
        all_targets_for_capability("analyze", validated_paths,
                                   "inplace", artifacts_dir)
        = ["/app/userdata/input-0/csv.analysis.md"]
      verifier reads it back → ✓
  → report_result
      task-report.json  → /app/artifacts/<task>/...   (workspace)
      agentic-output.txt → /app/artifacts/<task>/...  (workspace)
      warnings.md       → /app/artifacts/<task>/...   (workspace)
```

The `RW` mount means the in-container write to
`/app/userdata/input-0/csv.analysis.md` shows up at
`/Users/.../tests/data/csv/csv.analysis.md` on the host. The plan
and final deliverable landed in the user's source folder; the
intermediate scratch files, the task report, and the agentic
transcript all stayed in the office workspace.

## Error handling

| Situation | Behaviour |
|---|---|
| Prompt and verifier agree (the new default) | Normal flow, no change |
| LLM writes to a path outside `OFFICE_ALLOWED_BASE_PATHS` | `write_file` returns `target path X is not in OFFICE_ALLOWED_BASE_PATHS`; the LLM self-corrects; if it fails repeatedly, the office task fails closed and compass surfaces the error |
| LLM writes to a directory that is not `os.path.isdir` (and the helper thought it was) | `os.makedirs(os.path.dirname(...))` raises `FileNotFoundError`; the existing `execute_office_work` retry / failure path takes over |
| Existing in-place tests that depended on the old sibling-path prompt | None found in the current `tests/unit/agents/test_office_*.py` corpus. The new helper preserves the directory-input semantics already pinned by `test_expected_output_paths_for_inplace_directory_analyze` |

## Testing

### New unit tests

`tests/unit/agents/test_office_output_paths.py` — pin the helper on
the full capability × mode × source-shape grid:

- `output_mode ∈ {"workspace", "inplace"}`
- `source_path` is a file or a directory
- `capability ∈ {"analyze", "summarize", "organize"}`

Key invariants to assert:

- `inplace + dir + analyze` → `<dir>/<basename>.analysis.md`
  (not `<dir>.analysis.md` — this is Bug A's regression guard)
- `inplace + file + analyze` → `<dir_of(file)>/<basename>.analysis.md`
- `inplace + dir + organize` → `<dir>/organization-plan.md`
- `workspace + any` → `<artifacts_dir>/<filename>`

`tests/unit/agents/test_office_prompt_target_paths.py` — pin the
prompt builders to consume the helper:

- `_build_analyze_prompt(["/data"], "inplace", ...)` contains
  `/data/data.analysis.md` and does **not** contain
  `/data.analysis.md` (Bug A's regression guard)
- `_build_organize_prompt(["/data"], "inplace", ...)` contains
  `/data/organization-plan.md` and does **not** contain the literal
  token `{source_folder}` (Bug B's regression guard)
- `_build_summarize_prompt(["/data/x.csv"], "inplace", ...)`
  contains `/data/x.csv.summary.md`

### Existing tests

- `test_expected_output_paths_for_inplace_directory_analyze` —
  already asserts the helper output matches the new helper; should
  pass without modification.
- All other `test_office_*` unit tests — no behaviour change in
  `workspace` mode, and `inplace` tests have always relied on the
  helper (which we are keeping compatible).

### Manual end-to-end check

A live e2e is not required for this change because the methodology
fix is on the prompt/verifier path that unit tests already cover.
The full 1069 unit tests passing is the bar.

## Compatibility and risk

- The three private function names `_target_output_file`,
  `_target_output_path`, and `_expected_output_paths` are kept as
  shims re-exporting the new helper. Any out-of-tree reference still
  works.
- `_build_*_prompt` is a private helper. No external caller.
- `OFFICE_SOURCE_ROOT` and `OFFICE_ALLOWED_BASE_PATHS` env names and
  semantics are untouched.
- `WriteFileTool`'s `_validate_path` consumption is untouched — the
  two-layer sandbox / whitelist remains the only write grant.
- Risk: a future prompt author who bypasses the helper and writes a
  new `Target path:` line by hand could re-introduce Bug A. We
  mitigate by adding a docstring comment at the top of each prompt
  builder pointing to `target_with_suffix` as the only legal way to
  produce a target path.
