# Office/Compass Clarification Reply Contract (2026-06-11)

## Background

The Office clarification flow currently mixes two separate concerns:

1. **Presentation / timeline identity**: which major-step row the UI
   should keep open while waiting for a user reply.
2. **Reply semantics**: what kind of answer the agent is actually
   expecting from the user in this clarification round.

This coupling is fragile. A single display-oriented interrupt kind can
hide multiple semantic sub-modes across a multi-step clarification flow
(for example, "pick a dimension" vs "approve the drafted plan"). The
system then has to infer meaning from ad hoc combinations of
`interrupt.kind`, `needs_clarification.missing`, and local keyword
parsers. That makes the behavior hard to reason about, easy to regress,
and difficult to extend without adding more case-specific branches.

The methodology gap is not limited to one organize scenario. Any agent
that pauses for user input needs an explicit contract describing what
kind of reply is expected, how the reply is interpreted, and when the
system must fail closed instead of guessing.

## Goals

1. **Make reply understanding explicit.** Every clarification payload
   must declare the semantic contract for the expected user reply.
2. **Separate UI continuity from semantic validation.** A timeline row
   may stay stable across multiple clarification phases without forcing
   them to share the same reply parser.
3. **Use one shared resolution path.** Compass and Office must rely on
   the same contract-driven resolver instead of duplicating reply
   heuristics.
4. **Fail closed on ambiguity.** If a reply does not satisfy the active
   contract, the task stays in `INPUT_REQUIRED` and the user receives a
   precise re-ask; the system must not silently reinterpret the reply.
5. **Preserve methodology-level generality.** The design must not encode
   concrete folder schemas, domain nouns, or task-specific examples into
   the runtime behavior.

## Non-Goals

- Not redesigning the Compass chat UI or major-step visuals beyond the
  metadata needed to keep them correct.
- Not introducing LLM-based free-form intent classification for every
  clarification reply.
- Not changing non-clarification office execution behavior.
- Not broadening this iteration to Team Lead, Dev, or Code Review agent
  flows, though the contract model should be reusable there later.

## Root Cause

The current flow overloads `interrupt.kind` with two responsibilities:

- it selects the user-visible waiting row and resume route; and
- it implicitly selects the reply parser.

That works only while one waiting row maps to exactly one semantic reply
type. As soon as a single row can span multiple clarification phases,
the parser must look elsewhere (`needs_clarification.missing`, embedded
plan state, raw text) and the logic becomes layered and brittle.

The deeper issue is methodological: **the system does not treat "what
reply is valid right now?" as first-class state**.

## Recommended Approach

Introduce an explicit **reply contract** carried with every
`needs_clarification` payload and persisted in task interrupt metadata.
The reply contract becomes the single source of truth for validation and
normalization. `interrupt.kind` remains available for timeline/UI
continuity, but it is no longer responsible for semantic parsing.

## Architecture

### New data split

Every waiting-for-user state keeps two separate fields:

- `interrupt.kind`
  Purpose: presentation identity, major-step continuity, broad routing.
- `needs_clarification.reply_contract`
  Purpose: semantic validation and normalization of the next user reply.

Example shape:

```python
{
    "kind": "office_organize_dimension",
    "needs_clarification": {
        "missing": "organizeCustomPlan",
        "user_message": "...",
        "options": [...],
        "reply_contract": {
            "schema_version": 1,
            "kind": "approve_or_modify",
            "actions": [
                {"id": "approve", "label": "Approve plan"},
                {"id": "modify", "label": "Modify plan", "requires_note": False},
            ],
            "free_text_suffix": "optional",
            "reask_message": "Please reply with `approve` or `modify: <change>`.",
            "ambiguity_policy": "reask",
        },
    },
}
```

The key point is that the timeline can still show one stable
`office_organize_dimension` row while the semantic contract says the
current reply is an approval action, not a dimension choice.

### Shared reply resolver

Add a shared module, for example
`framework/clarification_reply.py`, exporting:

- `resolve_reply(contract: Mapping[str, Any], user_text: str) -> ReplyResolution`
- `render_reask(contract, reason) -> str`
- contract validators for supported contract kinds

`ReplyResolution` should be a structured result:

```python
{
    "ok": True,
    "normalized": {
        "kind": "approve_or_modify",
        "action": "modify",
        "note": "create top-level buckets first",
    },
    "diagnostic": "matched_action_prefix",
}
```

or

```python
{
    "ok": False,
    "reason": "unknown_action",
    "reask_message": "Please reply with `approve` or `modify: <change>`.",
}
```

Compass uses this resolver for preflight validation and user-facing
re-asks. Office uses the same resolver for authoritative normalization
when the waiting task resumes. There must not be two separate semantic
parsers for the same contract kind.

### Contract kinds for this iteration

This iteration only needs a small generic set:

- `select_option`
  For replies that must resolve to one canonical option id.
- `approve_or_modify`
  For binary action replies with optional or required follow-up text.
- `free_text`
  For replies where the text is accepted verbatim.

The contract kind is methodology-level. Concrete tasks provide option
lists and messages, but not new parser logic.

### Normalized clarification payload

After a valid reply, Compass stores the normalized result under a
generic field, for example:

```python
office_request["clarification_resolution"] = {
    "contract_kind": "approve_or_modify",
    "action": "approve",
    "note": "",
}
```

Office then consumes that normalized payload when resuming the existing
task. For compatibility during rollout, Office may continue to populate
legacy fields such as `organize_custom_action` internally, but those
become a local translation step rather than the cross-agent contract.

## Flow Changes

### Before

1. Office pauses with `needs_clarification`.
2. Compass maps `missing` into one `interrupt.kind`.
3. On resume, Compass chooses a parser mostly from `interrupt.kind`,
   with extra special cases.
4. Office re-parses the same reply again using its own local logic.

### After

1. Office pauses with `needs_clarification.reply_contract`.
2. Compass stores the payload unchanged and keeps its chosen
   `interrupt.kind` only for UI continuity.
3. On resume, Compass resolves the reply against the active
   `reply_contract`.
4. Invalid or ambiguous replies re-ask using the same contract.
5. Valid replies are forwarded as normalized clarification results to
   the same waiting Office session.
6. Office uses the same shared resolver contract type and the normalized
   payload instead of guessing from raw text and local branches.

## Component Changes

### `framework/clarification_reply.py` (new)

- Defines contract schema helpers and the shared resolver.
- Owns generic alias tables for contract kinds like
  `approve_or_modify`, not task-specific nouns.
- Returns structured diagnostics so tests can assert *why* a reply was
  rejected.

### `agents/office/agent.py`

- When pausing for clarification, populate `reply_contract` in
  `needs_clarification`.
- On resume, prefer `clarification_resolution` when present.
- During rollout, translate the generic normalized result into existing
  office-local execution metadata as close to the execution boundary as
  possible.

### `agents/compass/agent.py`

- Stop deriving semantic parsing solely from `interrupt.kind`.
- Keep `_office_interrupt_kind()` only for UI/timeline continuity.
- Replace ad hoc `_resolve_office_resume_reply()` branching with shared
  contract resolution.
- Re-ask by preserving the same `reply_contract` and updating only the
  user-facing message.

### `agents/compass/tools.py`

- Preserve `reply_contract` when translating Office dispatch results
  into Compass task metadata so no semantic information is dropped
  between the first pause and later resumes.

## Error Handling

The system must explicitly distinguish these cases:

- `unknown_reply`
  The text does not match the allowed contract shape.
- `ambiguous_reply`
  The text maps to multiple possible outcomes and the contract says
  `ambiguity_policy = "reask"`.
- `missing_required_note`
  The action was recognized, but a required free-text suffix is absent.
- `stale_contract`
  The task metadata is missing or corrupt; fail with an internal error
  rather than guessing.

All user-facing retries keep the task in `INPUT_REQUIRED`, preserve the
same waiting Office session, and echo a concrete valid reply format.

## Testing Strategy

### Shared contract unit tests

- `select_option` resolves canonical ids and supported aliases.
- `approve_or_modify` resolves action-only replies and replies with
  trailing notes.
- invalid replies return `ok = False` with a stable diagnostic reason.
- ambiguous replies re-ask rather than choosing a branch.

### Compass/Office round-trip tests

- a clarification with one UI `interrupt.kind` and two different
  `reply_contract.kind` phases keeps one waiting row but changes parser
  semantics correctly.
- invalid replies keep the same Office session alive and re-ask with the
  preserved contract.
- valid replies forward normalized clarification results to the same
  Office task instead of launching a new Office agent.

### Regression tests

- existing output-mode and built-in dimension flows still work.
- custom-plan approval no longer depends on a dimension parser fallback.
- repeated user replies do not lose contract state after the second or
  later retry.

## Rollout Notes

To reduce side effects:

1. Keep the existing `interrupt.kind` values for UI continuity in this
   iteration.
2. Add `reply_contract` alongside current metadata first.
3. Switch Compass resume validation to the shared resolver.
4. Switch Office resume handling to normalized clarification results.
5. Remove obsolete ad hoc parser branches only after the tests cover the
   shared contract path.

## Acceptance Criteria

- Every Office clarification payload carries an explicit
  `reply_contract`.
- Compass resume validation no longer depends on overloaded
  `interrupt.kind` semantics.
- Office and Compass use one shared resolver path for clarification
  semantics.
- Invalid replies re-ask without spawning a new Office session.
- The design remains domain-neutral and introduces no task-specific
  business vocabulary into runtime logic.
