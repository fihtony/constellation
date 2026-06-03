# Office Plan-Output Gate — Design

**Date:** 2026-06-03
**Status:** Revised draft
**Author:** Constellation engineering

## 1. Problem

The Office agent already follows a plan-then-materialize shape:

- `organize` writes `organization-plan.md` and copies files into `organized-output/files/`
- `summarize` writes per-document summaries and may also write `combined-summary.md`
- `analyze` writes per-source `*.analysis.md`

However, there is currently no deterministic contract that proves the final output still matches what the Office agent planned to deliver. This creates three classes of failure:

1. The plan exists, but the materialized output tree disagrees with it.
2. The output files exist, but the plan is missing, incomplete, or unparseable.
3. A later deterministic repair/canonicalization step mutates already-generated outputs without re-validating against the plan.

When any of the above happens, the user receives an apparently completed Office task whose outputs do not match the declared plan, and Compass has no major-step signal explaining the mismatch or the repair attempt.

## 2. Goal

After **each materialization round is fully finished**, including any deterministic post-processing for that round, a pure plan-output gate validates the Office task's output against the plan contract for that capability.

If the gate finds any of the following, the task is returned to the LLM for reconciliation:

- missing mandatory plan
- unparseable or structurally invalid plan
- missing deliverables
- unexpected deliverables
- plan-specific mismatches (for example, `analyze` committed-field facts that do not match the produced report)

The reconciliation loop is capped at three rounds. If the gate is still not clean after the final round, the task may complete with warnings, but it must not silently present a clean success:

- Compass major steps must show the validation and reconciliation process explicitly.
- `warnings.md` and `task-report.json` must include the degraded outcome.
- `plan-output-gate-report.json` must persist the final discrepancy report.

The gate applies to all three Office capabilities: `analyze`, `summarize`, and `organize`.

## 3. Non-goals

- Replacing the LLM. The gate is deterministic; reconciliation remains an LLM action.
- Choosing the organization/analysis/summarization strategy. The plan remains the source of truth for that decision.
- Cross-task validation. The gate validates one Office task's declared plan against that same task's output tree.
- Regenerating a different plan during retry. The retry loop fixes the output to match the existing plan contract.
- Embedding task-specific examples, fixture paths, sample column names, or test-only hints into agent prompts or gate logic.

## 4. Architecture

### 4.1 Round contract

One Office materialization round consists of:

1. LLM writes or updates the required plan artifact for the capability.
2. LLM materializes or repairs deliverables using the authorized Office tools.
3. Deterministic post-processing for that round completes.
4. The plan-output gate validates the final state of the round.

No output-tree mutation is allowed **after** a clean gate pass except read-only verification and report-writing side effects (`task-report.json`, `warnings.md`, `agentic-output.txt`, `plan-output-gate-report.json`). If a later step needs to mutate any deliverable, the gate must run again before the task can proceed to `office.delivered`.

### 4.2 Control flow

```
[Office Node: execute_office_work]
    │
    ├── Round 0: primary LLM materialization
    │     • write required plan artifact
    │     • materialize deliverables
    │     • deterministic post-processing for round 0
    │
    ├── emit office.validating_plan_output (lifecycle=running)
    │
    ├── [NEW] plan_output_gate.run(contract)
    │     • contract resolves plan_path, output_root, allowlist, expected shape
    │     • returns GateReport
    │
    ├── IF GateReport.is_clean:
    │     • emit office.validating_plan_output (lifecycle=done)
    │     • proceed to office.verifying -> office.delivered
    │
    └── ELSE (retry loop, max 3 rounds):
          • emit office.validating_plan_output (lifecycle=warning, summary=diff detected)
          • For round in 1..3:
              • emit office.reconciling_plan_output#{round} (lifecycle=running)
              • re-invoke LLM with retry prompt + same task state + authorized fix tools
              • deterministic post-processing for that round
              • re-run plan_output_gate.run(contract)
              • IF clean:
                    - emit office.reconciling_plan_output#{round} (lifecycle=done)
                    - emit office.validating_plan_output (lifecycle=done, summary=clean after retry)
                    - proceed to office.verifying -> office.delivered
              • ELSE:
                    - emit office.reconciling_plan_output#{round} (lifecycle=warning)
          • If still not clean after round 3:
                - emit office.validating_plan_output (lifecycle=warning, summary=exhausted)
                - emit office.gate_exhausted (lifecycle=warning)
                - write plan-output-gate-report.json
                - append warning to task result
                - proceed to office.verifying -> office.delivered
```

## 5. Components

### 5.1 `framework/office/plan_output_gate.py` (new)

Pure module: no network, no LLM calls, no capability-specific hardcoded sample data.

Public API:

```python
@dataclass(frozen=True)
class GateEntry:
    source_path: str
    expected_path: str
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OutputContract:
    capability: str
    plan_path: str
    output_root: str
    ancillary_allowlist: set[str]
    source_count: int
    expected_plan_kind: str


@dataclass(frozen=True)
class GateReport:
    capability: str
    plan_status: str               # ok | missing | unparseable | invalid
    planned_count: int
    actual_count: int
    missing: list[str]
    unexpected: list[str]
    mismatches: list[str]
    error_message: str = ""

    @property
    def is_clean(self) -> bool: ...


def resolve_output_contract(
    capability: str,
    validated_paths: list[str],
    output_mode: str,
    artifacts_dir: str,
) -> OutputContract: ...
def parse_plan(capability: str, plan_path: str) -> list[GateEntry]: ...
def walk_output(output_root: str, *, allowlist: set[str] | None = None) -> set[str]: ...
def diff(capability: str, plan: list[GateEntry], actual: set[str], contract: OutputContract) -> GateReport: ...
def run(contract: OutputContract) -> GateReport: ...
```

`resolve_output_contract(...)` is required so the gate does not guess paths ad hoc inside different node branches.

### 5.2 Capability-specific plan contract

| Capability | Mandatory plan artifact | Mandatory plan content | Output root |
|---|---|---|---|
| `organize` | `organization-plan.md` | `## Files Organized` table with one `(source, destination)` row per non-hidden source file | `organized-output/files/` under the resolved task output location |
| `summarize` | `summary-plan.md` | `## Source -> Summary Mapping` table with one `(source, summary_target)` row per discovered source file; `combined-summary.md` requirement when more than one file | resolved write directory for the task output mode |
| `analyze` | `analysis-plan.md` | `## Source -> Analysis Mapping` table with one `(source, analysis_target)` row per discovered source file, plus `## Committed Fields` facts used for validation | resolved write directory for the task output mode |

Additional requirements:

- For folder-backed `summarize` and `analyze`, the plan must enumerate the **expanded file list**, not only the original folder path.
- Empty plan is only valid when the source inventory is empty and the capability semantics allow zero deliverables.
- `organize` must still satisfy the existing rule that every non-hidden surveyed source file is copied exactly once.
- The plan for `summarize` and `analyze` is **mandatory** before the LLM can complete round 0; the gate runs at the end of round 0 with `plan_status="missing"` if the LLM skipped writing the plan, and the LLM is told to write the plan first in the next retry round (no `copy_file` / `write_workspace` calls are expected in that round).
- A plan whose capability tag does not match the running capability is reported as `plan_status="invalid"` with `error_message="plan for capability X found in capability Y slot"` and the LLM is told to overwrite it.
- A plan that is the artifact of a previous run (e.g. a stale `summary-plan.md` from a prior task sharing the same workspace) is treated as `unparseable` if its destination rows reference paths not in the current source inventory, and the LLM is told to rewrite it for the current task.

### 5.3 Ancillary-file allowlist

The gate must distinguish deliverables from system-side artifacts that are allowed to exist in the same root.

Allowed ancillary files:

- the capability's plan artifact (`organization-plan.md`, `summary-plan.md`, `analysis-plan.md`)
- `plan-output-gate-report.json`
- `task-report.json`
- `warnings.md`
- `agentic-output.txt`
- timestamped backup files created by `write_workspace` / `write_file` (for example `*.YYYYMMDD-HHMMSS.bak`)

Anything else under the resolved output root that is not part of the capability contract or the ancillary allowlist is `unexpected`.

Ancillary handling rules:

- The walker only counts regular files. Empty directories are not deliverables and not unexpected.
- Hidden files (dot-prefixed names) under the output root are ignored.
- The allowlist matches by basename, not by full path, so plan/report files placed in subdirectories are still ignored.
- Symlinks must be resolved with `realpath`; any symlink chain that escapes the output root is treated as an unexpected deliverable AND as a path-safety violation (see §5.10).

### 5.3.1 Plan-destination path safety

Every entry in the parsed plan must satisfy all of the following before it is considered a valid contract entry:

- `expected_path` is a relative path using `/` separators (the parser normalizes `\\` to `/`).
- `expected_path` does not start with `/`, `~`, or a drive letter.
- `expected_path` contains no `..` segment after normalization.
- `realpath(output_root + expected_path)` resolves to a path whose prefix matches `realpath(output_root)`.
- The corresponding `source_path` resolves to a path whose prefix matches one of the validated source roots for the task.
- No two plan entries share the same `(source_path, expected_path)` pair.

Plan entries that fail any of these checks are reported under a new `invalid_plan_entries` bucket on `GateReport` and the gate fails with `plan_status="invalid"`. The retry prompt must list the offending entries verbatim so the LLM can rewrite them.

### 5.4 Retry prompt builder

`_build_retry_prompt(capability, contract, gate_report, round_num) -> str` returns the single user message passed to the LLM on each reconciliation round.

Requirements for the retry prompt:

- cap each discrepancy bucket shown inline at 20 items, while the JSON report keeps the full lists
- state the round number and remaining budget
- instruct the LLM to fix the output to match the existing plan
- explicitly forbid plan regeneration unless the gate failure is `missing` / `unparseable` / `invalid`, in which case the LLM may repair the same plan artifact but must keep the same intended deliverable set
- remind the LLM that it must use only the authorized Office tools

Example shape:

```
[plan-output-gate] The declared plan and the materialized output disagree. (round R of 3)

Plan status: ok
Missing deliverables: M
Unexpected deliverables: U
Plan-specific mismatches: K

Missing from output (max 20 shown):
  - <planned path>

Unexpected in output (max 20 shown):
  - <actual path>

Fix the materialized output so it matches the existing plan contract exactly.
Do not invent new deliverables.
Do not leave stale outputs from previous rounds.
Use only the authorized Office tools for this task.
```

### 5.5 Controlled deletion requirement

The retry loop is not implementable unless the Office runtime exposes a safe way to remove stale outputs produced by earlier rounds.

Therefore this design requires a new LLM-facing output-cleanup capability:

- preferred shape: `delete_output_file(path)` limited to the resolved output root for the current task
- acceptable alternative: a capability-neutral delete action added to the Office tool surface with the same path restrictions

Hard requirements:

- must never delete original source inputs
- must never delete files outside the resolved task output root
- in `inplace` mode, only declared deliverable paths and ancillary files under the resolved target directory may be deleted
- deletion attempts must be logged without leaking secrets
- every call must use `realpath` and re-verify the prefix against the resolved output root (defends against symlink-escape during retry)
- the tool must reject calls if the path resolves to a file under any validated source root, not just a textual match
- the tool must be registered for all three capabilities (`analyze`, `summarize`, `organize`) before the gate's first run; if the registration check fails, the gate must surface a `tool_unavailable` error and the task must fail closed rather than silently retrying

This requirement applies to `analyze`, `summarize`, and `organize`, not just `organize`.

### 5.5.1 Plan integrity during retry

The plan is the source of truth. The LLM must not modify the plan artifact during reconciliation, except when the gate's failure mode is `missing`, `unparseable`, or `invalid` (in which case the LLM may repair the same plan artifact in place but must keep the same intended deliverable set).

Enforcement:

- The Office node snapshots the plan file's `realpath`, `mtime_ns`, and a sha256 of its bytes before each retry round.
- After the LLM returns from a retry round and before the gate re-runs, the Office node compares the current values to the snapshot.
- If the plan was modified AND the previous gate's `plan_status` was `ok`, the retry is treated as a forbidden plan regeneration. The Office node:
  - reverts the plan to the snapshot (or, if revert fails, refuses to proceed and surfaces a task failure)
  - emits `office.reconciling_plan_output#R` with lifecycle=`warning` and summary=`Plan was modified during retry; reverted to snapshot.`
  - counts the round as used
- If the plan was modified AND the previous gate's `plan_status` was `missing` / `unparseable` / `invalid`, the modification is allowed and the gate runs against the new plan.
- If the plan is missing entirely after a retry round (LLM deleted it), the gate re-runs with `plan_status="missing"` and the LLM is prompted to restore it from the snapshot (the snapshot is still in node-local state).

### 5.6 Office-node integration

In `agents/office/nodes.py`, `execute_office_work` must:

1. Resolve the output contract once from task state.
2. Run the primary LLM round.
3. Run deterministic post-processing for that round.
4. Run the gate.
5. If the gate is not clean, execute up to three reconciliation rounds using the retry prompt and the authorized fix tools.
6. Persist the final gate warning into task outputs if the gate exhausts.
7. Append every tool call (round 0 and every retry round) to `operations-plan.json` with a `round` field and a `trigger` field (`"primary"` for round 0, `"gate-retry"` for retry rounds, `"gate-exhausted"` for the final exhaustion write if any).
8. Snapshot the plan file (`realpath`, `mtime_ns`, sha256) before each retry round and verify it after (see §5.5.1).
9. Verify the `delete_output_file` tool is registered before the first retry round (see §5.5).

The Office node must not rescan the user's source folder during reconciliation for the purpose of redefining the contract. The gate compares against the source inventory already established by the task's validated input set and capability-specific discovery step.

#### 5.6.1 `inplace` mode semantics

For `output_mode="inplace"`, the resolved target directory is `<source_root>/organized-output/files/` (and the equivalent for the other two capabilities). Reconciliation in inplace mode:

- The `delete_output_file` tool may delete files only inside the resolved target directory, never inside the source input tree.
- Plan destinations must still pass the path-safety check from §5.3.1; a plan destination that points back into the source root is rejected as `invalid`.
- The retry prompt for inplace mode must remind the LLM that the source tree is read-only and that the only writable area is the resolved target directory.

#### 5.6.2 No-progress reconciliation detection

A retry round is considered "no progress" when the LLM's response contained no successful tool calls (only text). The Office node must:

- detect this by inspecting the run trace for any successful tool invocation whose `round` matches the current retry round
- if no progress: still increment the retry counter, but emit `office.reconciling_plan_output#R` with lifecycle=`warning` and summary=`Reconciliation round {R} made no tool-call progress.`
- this prevents the LLM from stalling the retry budget with text-only "I'll fix it" replies

### 5.7 Major-step rows

Three step keys are required in `agents/office/office_steps.py`:

```python
def emit_validating_plan_output(
    state, *,
    lifecycle_state: str,
    summary_template: str,
    summary_facts: dict | None = None,
) -> None: ...

def emit_reconciling_plan_output(
    state, *,
    lifecycle_state: str,
    round: int,
    summary_template: str,
    summary_facts: dict | None = None,
) -> None: ...

def emit_gate_exhausted(
    state,
    *,
    summary_facts: dict | None = None,
) -> None: ...
```

And `_office_major_step_skeleton` in `agents/compass/agent.py` adds:

```python
{
    "step_key": "office.validating_plan_output",
    "title": "Office validating output against plan",
    "agent": "office",
},
{
    "step_key": "office.reconciling_plan_output",
    "title": "Office reconciling output to match plan",
    "agent": "office",
    "conditional": True,
},
{
    "step_key": "office.gate_exhausted",
    "title": "Office plan-output gate exhausted",
    "agent": "office",
    "conditional": True,
},
```

This split is required so Compass UI shows both:

- the validation checkpoint itself
- the LLM repair process when the validation fails

### 5.8 Lifecycle semantics

Use `warning`, not `warn`, for lifecycle values.

`office.validating_plan_output`:

| Outcome | Lifecycle | Visual | Summary template |
|---|---|---|---|
| Initial validation in progress | `running` | `current` | `Office is validating the materialized output against the declared plan.` |
| Clean on first pass | `done` | `done` | `Plan and output match. Validated {planned_count} planned deliverable(s).` |
| Mismatch detected, retries remain | `warning` | `warn` | `Validation found {missing_count} missing, {unexpected_count} unexpected, and {mismatch_count} mismatched item(s). Starting reconciliation.` |
| Clean after retry | `done` | `done` | `Plan and output match after {round_count} reconciliation round(s). Validated {planned_count} planned deliverable(s).` |
| Exhausted after round 3 | `warning` | `warn` | `Plan-output gate exhausted after {round_count} reconciliation round(s): {missing_count} missing, {unexpected_count} unexpected, {mismatch_count} mismatched. See plan-output-gate-report.json.` |

`office.reconciling_plan_output`:

| Outcome | Lifecycle | Visual | Summary template |
|---|---|---|---|
| Round running | `running` | `current` | `Office is reconciling the output to match the plan (round {round} of 3).` |
| Round completed but further retry needed | `warning` | `warn` | `Reconciliation round {round} completed, but validation is still not clean.` |
| Round completed and next validation is clean | `done` | `done` | `Reconciliation round {round} completed and the output now matches the plan.` |

`office.gate_exhausted`:

| Outcome | Lifecycle | Visual | Summary template |
|---|---|---|---|
| Retry budget exhausted | `warning` | `warn` | `Office could not fully reconcile the output with the declared plan after {round_count} round(s).` |

## 6. Data flow

```
state["_plan_output_gate_warnings"]: list[str]      # Office-node-local
state["_plan_output_gate_retry_count"]: int         # Office-node-local
state["_plan_output_gate_last_report"]: dict        # Office-node-local
state["_plan_output_gate_plan_snapshot"]: dict      # Office-node-local; per-round
                                                       # {realpath, mtime_ns, sha256, bytes}
state["_plan_output_gate_last_diff_signature"]: str # Office-node-local; sha256 of
                                                       # sorted(missing)+sorted(unexpected)+sorted(mismatches)
                                                       # for no-progress detection

artifacts/<task>/plan-output-gate-report.json       # written only when gate exhausts
  {
    "capability": "summarize",
    "rounds": 3,
    "plan_status": "ok",
    "planned_count": 4,
    "actual_count": 5,
    "final": {
      "missing": ["..."],
      "unexpected": ["..."],
      "mismatches": []
    },
    "invalid_plan_entries": ["<verbatim plan rows that failed path/source safety>"],
    "no_progress_rounds": [1, 2],
    "plan_modification_detected": false,
    "tool_unavailable": false,
    "plan_path": "<resolved plan path>",
    "output_root": "<resolved output root>"
  }

artifacts/<task>/operations-plan.json               # appended on every tool call
  {
    "action": "copy_file",
    "src": "...",
    "dst": "...",
    "round": 0,                  # 0 for primary, 1..3 for retry rounds
    "trigger": "primary" | "gate-retry",
    "status": "succeeded",
    ...
  }
```

`summary_facts` written to Compass major steps should include at least:

- `planned_count`
- `actual_count`
- `missing_count`
- `unexpected_count`
- `mismatch_count`
- `invalid_plan_entries_count`
- `round`
- `round_count`
- `plan_status`
- `no_progress_count`

## 7. Error handling

| Failure | Behavior |
|---|---|
| Plan file missing | `plan_status="missing"`; gate fails and enters reconciliation. Do not skip the gate. |
| Plan file present but unparseable | `plan_status="unparseable"`; gate fails and enters reconciliation. Do not skip the gate. |
| Plan file structurally invalid for the capability | `plan_status="invalid"`; gate fails and enters reconciliation. |
| Plan for wrong capability in slot (e.g. `summary-plan.md` during `organize`) | `plan_status="invalid"` with explicit `error_message`; gate fails and enters reconciliation. |
| Stale plan from a previous task sharing the same workspace | `plan_status="unparseable"` if destination rows do not match current source inventory; gate fails and enters reconciliation. |
| Empty plan with non-empty source inventory | Treat as invalid plan unless the capability explicitly allows zero deliverables for that source inventory. |
| Non-empty plan with empty source inventory | `plan_status="invalid"`; gate fails and enters reconciliation. |
| Plan destination escapes output root (`..`, absolute, drive letter, symlink) | Reported in `invalid_plan_entries`; gate fails with `plan_status="invalid"`. |
| Plan references source path outside the validated source set | Reported in `invalid_plan_entries`; gate fails with `plan_status="invalid"`. |
| Duplicate `(source_path, expected_path)` in plan | Reported in `invalid_plan_entries`; gate fails with `plan_status="invalid"`. |
| Output root missing | Treat as all planned deliverables missing; gate fails and enters reconciliation. |
| Symlink chain under output root escapes it | File is treated as `unexpected` AND recorded as a path-safety violation; gate fails. |
| Hidden files or empty directories under output root | Ignored; not counted as deliverables. |
| Ancillary files present | Ignore files on the ancillary allowlist; do not count them as unexpected deliverables. |
| Output backup files exist | Ignore timestamped `.bak` files via allowlist pattern matching. |
| LLM retry fails (tool error, exception) | Catch, log, count as a reconciliation round, and continue to the next round. |
| Retry round has no successful tool calls ("no progress") | Count the round as used; emit a `warning` step; do not stall the budget. |
| Reconciliation deletes or overwrites source input | Forbidden; treat as task failure, not as a recoverable gate warning. |
| Plan file modified during retry when previous `plan_status` was `ok` | Revert to snapshot; emit `warning` step; count the round as used. |
| Plan file missing after retry round | Gate re-runs with `plan_status="missing"`; retry prompt tells the LLM to restore it from the snapshot. |
| `delete_output_file` tool not registered | Gate reports `tool_unavailable`; task fails closed (no retries with no delete capability). |
| `delete_output_file` called on source-input path | Tool rejects the call; round continues; recorded in operations log. |
| `delete_output_file` called on path outside output root | Tool rejects the call; round continues; recorded in operations log. |
| Disk space exhaustion / permission denied during retry | Log, count as a round, continue. If the same failure repeats for two consecutive rounds, treat as task failure. |
| Deterministic post-processing mutates outputs after a clean gate | Forbidden by contract; implementation must either move that mutation before the gate or re-run the gate. |
| Gate report write fails | Log an error and append a task warning, but do not crash the Office node. |
| `operations-plan.json` write fails | Log an error; reconciliation still runs. Operations log is a best-effort audit trail. |
| Plan file is enormous (> 1 MB) | Parser reads line-by-line; if memory or time threshold is exceeded, mark `plan_status="unparseable"` and let the LLM rewrite a smaller plan. |
| Source paths in plan use non-UTF-8 encoding | Parser rejects with `unparseable`; LLM must rewrite using UTF-8 only. |
| Two retry rounds in a row with the same `missing`+`unexpected` set | "No progress" signal; emit a stronger warning; the final exhaustion is more clearly attributable to the LLM. |

## 8. Boundary-agent and runtime parity

Per project instruction #11, the design must remain backend-agnostic.

This gate is intentionally limited to:

- filesystem inspection
- markdown parsing
- capability-local task state
- Compass major-step emission

It must not depend on a specific SCM backend, design backend, or external network service.

Per project instruction #10, the same gate behavior must work in both:

- local execution
- containerized execution

The contract must not assume shared memory between Compass and Office; major steps still propagate through the existing task-store or progress-sink path.

## 9. Testing

### 9.1 Unit tests — `tests/unit/agents/office/test_plan_output_gate.py` (new)

Positive:

- `parse_plan_organize_extracts_pairs`
- `parse_plan_summarize_extracts_expanded_file_rows`
- `parse_plan_analyze_extracts_output_rows_and_committed_fields`
- `parse_plan_missing_returns_missing_status`
- `parse_plan_unparseable_returns_unparseable_status`
- `walk_output_excludes_ancillary_files`
- `walk_output_excludes_timestamped_backups`
- `walk_output_ignores_hidden_files_and_empty_dirs`
- `walk_output_ignores_ancillary_files_in_subdirectories`
- `walk_output_symlink_escape_treated_as_unexpected`
- `diff_clean_tree_returns_clean_report`
- `diff_missing_file_populates_missing`
- `diff_unexpected_file_populates_unexpected`
- `diff_analyze_committed_fields_mismatch`
- `diff_empty_plan_with_non_empty_inventory_is_invalid`
- `diff_non_empty_plan_with_empty_inventory_is_invalid`
- `diff_huge_plan_does_not_oom_parser`
- `diff_non_utf8_source_paths_marked_unparseable`

Negative / path-safety:

- `parse_plan_destination_outside_output_root_is_invalid`
- `parse_plan_destination_with_parent_traversal_is_invalid`
- `parse_plan_destination_absolute_path_is_invalid`
- `parse_plan_destination_via_symlink_escape_is_invalid`
- `parse_plan_source_outside_validated_set_is_invalid`
- `parse_plan_duplicate_rows_is_invalid`
- `parse_plan_stale_plan_from_prior_task_is_unparseable`
- `parse_plan_wrong_capability_in_slot_is_invalid`
- `parse_plan_folder_source_not_expanded_is_invalid`
- `parse_plan_huge_file_bounded_by_size_cap`
- `parse_plan_non_utf8_bom_rejected`

### 9.2 Office flow tests — `tests/unit/agents/office/test_office_plan_output_gate_flow.py` (new)

- clean first-pass flow emits `office.validating_plan_output -> done`
- mismatch flow emits `office.validating_plan_output -> warning`, then `office.reconciling_plan_output#1`
- successful retry flow closes `office.reconciling_plan_output#1` as `done` and closes validation as `done`
- exhausted flow emits `office.gate_exhausted` and writes `plan-output-gate-report.json`
- folder-backed `summarize` task validates against discovered file count, not the original folder placeholder
- folder-backed `analyze` task validates per expanded file output target
- no-progress retry round emits a `warning` step but still counts the round
- retry round that modifies the plan with previous `plan_status=ok` reverts to snapshot
- retry round that produces no tool calls is detected and counted
- two consecutive retries with the same diff set produce the stronger exhaustion warning
- round 0 missing `summary-plan.md` causes gate to fail with `plan_status=missing` and the LLM is told to write the plan first
- `inplace` mode retry: delete tool rejects a path under the source root
- `delete_output_file` tool not registered: gate reports `tool_unavailable` and the task fails closed
- `operations-plan.json` contains a `round` field for every entry, including retries
- `plan-output-gate-report.json` is written only on exhausted gate

### 9.3 Tool-surface tests

- controlled delete tool refuses paths outside the resolved output root
- controlled delete tool refuses source-input paths
- controlled delete tool refuses paths whose realpath resolves outside the output root via symlink
- `inplace` mode delete restrictions are enforced

### 9.4 Compass timeline tests

- Compass renders `office.validating_plan_output`
- Compass renders conditional `office.reconciling_plan_output` rounds only when used
- Compass renders `office.gate_exhausted` as a warning row
- Compass skeleton entries for the new step keys exist in `_office_major_step_skeleton` for all three capabilities

### 9.5 Regression

All existing unit tests must continue to pass in both local and container-backed Office test matrices.

## 10. Files touched

| File | Change |
|---|---|
| `framework/office/__init__.py` | New empty package |
| `framework/office/plan_output_gate.py` | New pure gate module (parser, walker, diff, runner, contract resolver) |
| `framework/office/path_safety.py` | New shared path-safety helper used by both the gate and the `delete_output_file` tool (`realpath` resolution, prefix check, symlink-escape detection) |
| `agents/office/office_tools.py` | Add controlled `delete_output_file` capability with path guardrails that delegate to `framework.office.path_safety` |
| `agents/office/office_steps.py` | Add `emit_validating_plan_output`, `emit_reconciling_plan_output`, `emit_gate_exhausted` |
| `agents/office/nodes.py` | Add contract resolution, gate enforcement, retry prompt builder, plan integrity snapshot, no-progress detection, and plan requirements for summarize/analyze |
| `agents/compass/agent.py` | Add validation / reconciliation / exhaustion rows to the Office major-step skeleton for all three capabilities |
| `tests/unit/agents/office/test_plan_output_gate.py` | New gate unit tests (positive + path-safety + no-progress) |
| `tests/unit/agents/office/test_office_plan_output_gate_flow.py` | New Office flow tests |
| `tests/unit/framework/office/test_path_safety.py` | New path-safety helper unit tests |
| `tests/unit/agents/compass/test_ui_integration.py` | Assert new major-step rendering in Compass |

## 11. Out of scope

- Regenerating an entirely different plan during reconciliation
- Cross-task caching of GateReports
- Multi-plan tasks
- Semantic quality review of the written summary/analysis itself beyond the declared plan contract

## 12. Open questions

None after this revision. The critical missing requirements are now explicit:

- missing/unparseable plans do not bypass the gate
- retry rounds have a safe deletion mechanism
- validation and reconciliation are separate Compass major steps
- no post-gate output mutation is allowed without re-validation
- plan-destination path safety is enforced (`..` escape, absolute, drive letter, symlink)
- plan modifications during retry are detected and reverted
- the retry budget cannot be stalled by no-progress rounds
- `delete_output_file` is verified to be registered before any retry
- `inplace` mode semantics are explicit (resolved target directory, source tree is read-only)
- `operations-plan.json` carries a `round` field on every entry

## 13. Change history

- **2026-06-03 (initial)** — first draft.
- **2026-06-03 (review pass)** — added path-safety validation for plan destinations and walker symlink handling, plan-integrity snapshot for retry, no-progress detection, `delete_output_file` registration check, `inplace` mode semantics, capability-tag/stale-plan error cases, two-consecutive-no-progress escalation, expanded error-handling table, expanded test matrix, new `framework/office/path_safety.py` helper, `round` and `trigger` fields on `operations-plan.json` entries, `no_progress_count` on Compass summary facts.
