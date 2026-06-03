# Office Plan-Output Gate — Design

**Date:** 2026-06-03
**Status:** Draft
**Author:** Constellation engineering

## 1. Problem

The Office agent's `organize` capability (and by extension `analyze` and `summarize`) generates a plan first — `organization-plan.md` etc. — and then materializes the plan via `copy_file` / `move_file` / `write_workspace` calls. After the LLM finishes, `_repair_missing_organize_outputs` runs as a "canonicalizer" and re-materializes every file using `_canonical_organize_destination`, a metadata-only function (driven by `primary_entity` + `inferred_date_bucket` + raw filename) that **never consults the LLM's plan**.

This produces a class of bug where the materialized tree disagrees with the plan. A real example from the artifact log of `task-34adaec0e1cf`:

| Plan said | Materialized tree contains |
|---|---|
| `documents/School-Communications/Info-parents-de-janvier-2025.pdf` | `2025-03/wp-content_uploads_2025_03_..._pdf_cdc0feae.pdf` |
| `videos/Freak_the_Mighty__Perfect_Foil.mp4` | `other/2-Freak_the_Mighty__Perfect_Foil.mp4` |
| `archives/stitch.zip` | `other/stitch.zip` |
| `documents/Development/2026-03/implementation-plan.md` | `2026-03/1-implementation-plan.md` |

The user is left with a deliverable that contradicts the plan the agent itself wrote, with no signal that the disagreement happened.

## 2. Goal

After the LLM finishes materializing, a deterministic **script** compares the plan to the actual output. If they disagree, the LLM is given up to three retry rounds to reconcile the output with the plan. After three failed rounds, the gate reports the discrepancy as a structured warning; the task itself does not fail.

The gate applies to all three office capabilities (`analyze`, `summarize`, `organize`) so the same safety net protects every deliverable.

## 3. Non-goals

- Replacing the LLM. The retry loop is the LLM's call; the gate is pure.
- Tightening the canonicalizer itself. The canonicalizer stays as a safety net; the gate is the new contract.
- Cross-task validation. The gate validates one plan against one output tree for one task.
- Plan regeneration. The retry prompt tells the LLM **not** to regenerate the plan.

## 4. Architecture

```
[Office Node: execute_office_work]
    │
    ├── LLM agentic loop (existing)
    │     • Plan: writes organization-plan.md / summary-plan.md / analysis-plan.md
    │     • Materialize: copy_file / move_file / write_workspace
    │     • Declares done
    │
    ├── emit office.validating_plan_output (lifecycle=running)
    │
    ├── [NEW] plan_output_gate.run(capability, plan_path, output_root)
    │     • Returns GateReport + retry_count = 0
    │
    ├── IF GateReport.is_clean:
    │     • emit office.validating_plan_output (lifecycle=done, summary=clean)
    │     • proceed to emit office.verifying → office.delivered
    │
    └── ELSE (retry loop, max 3):
          • For round in 1..3:
              • Re-invoke LLM with diff prompt
              • Re-run gate
              • Re-emit office.validating_plan_output (lifecycle=running, summary=current round)
              • If clean: emit (lifecycle=done, summary=clean after R rounds); break
          • If still not clean after 3 rounds:
                • emit office.validating_plan_output (lifecycle=warn, summary=exhausted)
                • emit office.gate_exhausted (lifecycle=warn)
                • Write plan-output-gate-report.json
                • Add warning to task result
                • proceed to verify/deliver (do not block)
```

## 5. Components

### 5.1 `framework/office/plan_output_gate.py` (new)

Pure module: no LLM, no network, no framework coupling beyond the LLM-independent parsers/walkers it uses.

Public API:

```python
@dataclass(frozen=True)
class GateEntry:
    source_path: str
    expected_path: str
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GateReport:
    capability: str
    missing: list[str]            # expected_path values that are not present
    unexpected: list[str]         # actual files that are not in the plan
    mismatches: list[str]         # analyze-only: field commitments that don't match

    @property
    def is_clean(self) -> bool: ...


def parse_plan(capability: str, plan_path: str) -> list[GateEntry]: ...
def walk_output(output_root: str, *, allowlist: set[str] | None = None) -> set[str]: ...
def diff(capability: str, plan: list[GateEntry], actual: set[str]) -> GateReport: ...
def run(capability: str, plan_path: str, output_root: str) -> GateReport: ...
```

### 5.2 Capability-specific parsers

| Capability | Plan file | Parser extracts |
|---|---|---|
| `organize` | `organization-plan.md` | `## Files Organized` table rows → `(src, dst)` |
| `summarize` | `summary-plan.md` *(new artifact, written by the LLM)* | `## Source → Summary Mapping` table → `(src, dst_summary_path)` |
| `analyze` | `analysis-plan.md` *(new artifact, written by the LLM)* | `## Committed Fields` block → `{field_count, numeric_field_count, source_count}` |

The two new plan files (`summary-plan.md`, `analysis-plan.md`) are required by the LLM prompts. `_build_summarize_prompt` and `_build_analyze_prompt` in `agents/office/nodes.py` are updated to mandate these files before materialization begins.

Parser tolerance: tables may have leading/trailing whitespace, blank lines, comment-style `<!-- … -->` rows. Empty plan → empty entries (gate then becomes trivially clean).

### 5.3 Retry-prompt builder

`_build_retry_prompt(capability, plan_path, output_root, gate_report, round_num) -> str` returns the single user message passed to the LLM on each retry round:

```
[plan-output-gate] The plan and the materialized output disagree. (round R of 3)

Discrepancies (max 20 shown):

Missing from output (planned but not present):
  - documents/School-Communications/Info-parents-de-janvier-2025.pdf
  - documents/Educational/Grade3/Gr3_Wk2_Convert_Energy_to_Motion.pdf

Unexpected in output (present but not in plan):
  - 2025-03/wp-content_uploads_2025_03_..._pdf_cdc0feae.pdf
  - other/2-Freak_the_Mighty__Perfect_Foil.mp4

Fix the materialized output so it matches the plan exactly.
Use copy_file / move_file / delete_file to reconcile.
Do not regenerate the plan; the plan is the source of truth.
Do not invent new destinations; move/copy to the planned paths.
```

The LLM is invoked with the same runtime, same tools, same task state — only the user prompt changes.

### 5.4 Office-node integration

In `agents/office/nodes.py`, `execute_office_work` after the LLM's final response:

1. Call `plan_output_gate.run(capability, plan_path, output_root)`.
2. If `gate_report.is_clean`:
   - emit `office.validating_plan_output` (done, summary=clean)
   - continue to `emit_capability_completion_rows` → `emit_writing` → `emit_verifying` → `emit_delivered`.
3. Else, retry up to three rounds:
   - emit `office.validating_plan_output` (running, summary=current round)
   - re-invoke the LLM with the retry prompt
   - re-run the gate
   - on first clean, emit (done, summary=clean after R rounds) and continue
4. After three failed rounds:
   - emit `office.validating_plan_output` (warn, summary=exhausted counts)
   - emit `office.gate_exhausted` (warn, summary=see report)
   - write `artifacts/plan-output-gate-report.json`
   - append warning to `state["_plan_output_gate_warnings"]`
   - continue to verify/deliver (do not block)

### 5.5 Major-step rows

Two new step keys are added in `agents/office/office_steps.py`:

```python
def emit_validating_plan_output(
    state, *,
    lifecycle_state: str,
    summary_template: str,
    summary_facts: dict | None = None,
) -> None: ...

def emit_gate_exhausted(
    state,
    *,
    summary_facts: dict | None = None,
) -> None: ...
```

And `_office_major_step_skeleton` in `agents/compass/agent.py` adds one row for each capability (just before `office.verifying`):

```python
{
    "step_key": "office.validating_plan_output",
    "title": "Office validating output against plan",
    "agent": "office",
},
```

Plus one trailing `conditional: True` row at the end of each capability's skeleton:

```python
{
    "step_key": "office.gate_exhausted",
    "title": "Office plan-output gate exhausted",
    "agent": "office",
    "conditional": True,
},
```

### 5.6 Lifecycle semantics for `office.validating_plan_output`

| Outcome | Lifecycle | Visual | Summary template |
|---|---|---|---|
| Gate clean on first pass | `done` | `done` | `Plan and output match. Validated {N} planned files.` |
| Gate clean after retry | `done` | `done` | `Plan and output match after {R} reconciliation round(s). Validated {N} planned files.` |
| Gate failed, retries left | `running` | `warn` | `Reconciliation round {R} of 3: {M} missing, {U} unexpected.` |
| Gate exhausted (3 retries) | `warn` | `warn` | `Plan-output gate exhausted after 3 retries: {M} missing, {U} unexpected, {K} mismatches. See plan-output-gate-report.json.` |

## 6. Data flow

```
state["_plan_output_gate_warnings"]: list[str]  (Office-node-local)
state["_plan_output_gate_retry_count"]: int     (Office-node-local)

artifacts/<task>/plan-output-gate-report.json    (only on exhausted gate)
  {
    "capability": "organize",
    "rounds": 3,
    "final": {
      "missing": ["documents/School-Communications/Info-parents-de-janvier-2025.pdf", ...],
      "unexpected": ["2025-03/wp-content_uploads_..._pdf_cdc0feae.pdf", ...],
      "mismatches": []
    },
    "plan_path": "artifacts/<task>/office/artifacts/organization-plan.md",
    "output_root": "artifacts/<task>/office/artifacts/organized-output/files"
  }
```

## 7. Error handling

| Failure | Behavior |
|---|---|
| Plan file missing | Log warning, skip gate (existing behavior continues). The LLM was supposed to write the plan; if it didn't, the gate cannot enforce it. |
| Plan file present but unparseable | Log warning, skip gate. Same reasoning. |
| Output root missing | Treat as `unexpected=[]`, `missing=[<all planned entries>]`; gate fails → retries. |
| LLM retry fails (tool error, exception) | Catch, log, count as a round, continue to next round. After 3 failed rounds, gate exhausts as if diff were unresolved. |
| Gate report file write fails | Log error, but do not fail the task. |

## 8. Boundary-agent parity

Per project instruction #11, the gate must not depend on a single backend. The gate is **pure filesystem + markdown parsing** — no GitHub/Bitbucket/MCP calls, no Stitch/Figma calls. So it is backend-agnostic by construction. No additional parity work is required.

The change is local to the Office agent's filesystem-touching code path. `analyze` and `summarize` continue to use the same backend-agnostic tools.

## 9. Testing

### 9.1 Unit tests — `tests/unit/agents/office/test_plan_output_gate.py` (new)

- `parse_plan_organize` — extracts `(src, dst)` pairs from a sample `organization-plan.md` table.
- `parse_plan_organize_tolerates_whitespace` — leading/trailing whitespace, blank lines.
- `parse_plan_organize_empty_plan` — empty file → empty entries.
- `parse_plan_summarize` — extracts `(src, dst_summary)` from a `summary-plan.md` table.
- `parse_plan_analyze` — extracts committed fields from an `analysis-plan.md` block.
- `walk_output_returns_all_files_under_root` — fixture tree, walk collects every file path.
- `walk_output_allowlist_excludes_ancillary_files` — `organization-plan.md` etc. excluded.
- `diff_clean_tree_returns_empty_report` — plan matches output → all three sets empty.
- `diff_missing_file_populates_missing` — file in plan but not in output.
- `diff_unexpected_file_populates_unexpected` — file in output but not in plan.
- `diff_analyze_field_count_mismatch` — committed `field_count` doesn't match report.
- `diff_truncates_to_20_per_bucket` — long diffs are truncated for the retry prompt; full report written to JSON.

### 9.2 Integration test — `tests/unit/agents/office/test_office_plan_output_gate_flow.py` (new)

Simulate a `task-34adaec0e1cf`-style scenario using mocked LLM:

- LLM pass 1 produces a correct plan but the materialization is overridden (simulated by the test pre-seeding the output with metadata-derived paths).
- Gate detects 8 missing + 5 unexpected.
- LLM retry pass 2 receives the diff prompt and re-materializes correctly.
- Gate is now clean.
- Verify `office.validating_plan_output` emit sequence: running (round 0) → running (round 1) → done (clean after 1 round).
- Verify retry prompt contains the expected diff strings.

### 9.3 Regression

All 900 existing unit tests must continue to pass.

## 10. Files touched

| File | Change |
|---|---|
| `framework/office/__init__.py` | New (empty) package |
| `framework/office/plan_output_gate.py` | New: parser, walker, diff, runner |
| `agents/office/office_steps.py` | Add `emit_validating_plan_output`, `emit_gate_exhausted` |
| `agents/office/nodes.py` | Add `_enforce_plan_output_gate`, `_build_retry_prompt`; call from `execute_office_work`; update summarize/analyze prompts to require `summary-plan.md` / `analysis-plan.md` |
| `agents/compass/agent.py` | Insert `office.validating_plan_output` row + conditional `office.gate_exhausted` row in `_office_major_step_skeleton` for all three capabilities |
| `tests/unit/agents/office/test_plan_output_gate.py` | New unit tests |
| `tests/unit/agents/office/test_office_plan_output_gate_flow.py` | New integration test |
| `docs/2026-06-02-workflow-timeline-redesign-zh.md` | Append note on the new major step |

## 11. Out of scope

- Regenerating the plan during retry. Explicitly forbidden by the retry prompt.
- Caching GateReports across tasks. Each task is independent.
- Multi-plan outputs (a single task producing multiple plans). The current capability model has one plan per task; we keep that invariant.
- Schema-aware validation for `analyze` beyond the committed-fields check.

## 12. Open questions

None — design is approved.
