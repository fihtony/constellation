# Office Reply Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Introduce a shared clarification reply contract so Compass and Office validate and normalize user replies from explicit reply semantics instead of overloaded interrupt kinds.

**Architecture:** Add a shared clarification-reply resolver in `framework/`, teach Office to emit and consume `reply_contract` / normalized clarification resolutions, and switch Compass resume handling to the shared resolver while keeping existing interrupt kinds for UI continuity.

**Tech Stack:** Python 3.12, pytest, Constellation A2A task store and agent workflow code

---

### Task 1: Add the shared reply-contract resolver

**Files:**
- Create: `framework/clarification_reply.py`
- Test: `tests/unit/framework/test_clarification_reply.py`

- [ ] **Step 1: Write the failing tests**

```python
from framework.clarification_reply import resolve_reply


def test_select_option_resolves_alias_to_option_id():
    contract = {
        "kind": "select_option",
        "options": [
            {"id": "workspace", "aliases": ["ws"]},
            {"id": "inplace", "aliases": ["in place"]},
        ],
    }

    result = resolve_reply(contract, "ws")

    assert result["ok"] is True
    assert result["normalized"]["selection"] == "workspace"


def test_approve_or_modify_returns_modify_with_note():
    contract = {"kind": "approve_or_modify"}

    result = resolve_reply(contract, "modify: use top-level folders first")

    assert result["ok"] is True
    assert result["normalized"]["action"] == "modify"
    assert result["normalized"]["note"] == "use top-level folders first"


def test_unknown_action_reasks():
    contract = {
        "kind": "approve_or_modify",
        "reask_message": "Please reply with `approve` or `modify: <change>`.",
    }

    result = resolve_reply(contract, "sounds good")

    assert result["ok"] is False
    assert result["reason"] == "unknown_reply"
    assert "approve" in result["reask_message"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/unit/framework/test_clarification_reply.py -q`
Expected: FAIL because `framework.clarification_reply` does not exist yet.

- [ ] **Step 3: Write minimal implementation**

```python
def resolve_reply(contract: Mapping[str, Any], user_text: str) -> dict[str, Any]:
    kind = str(contract.get("kind") or "").strip()
    # normalize text, dispatch by contract kind, and return
    # {"ok": True, "normalized": {...}} or
    # {"ok": False, "reason": "...", "reask_message": "..."}.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/unit/framework/test_clarification_reply.py -q`
Expected: PASS

### Task 2: Teach Office to emit reply contracts and consume normalized resolutions

**Files:**
- Modify: `agents/office/agent.py`
- Test: `tests/unit/agents/test_office_clarification_roundtrip.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_office_custom_plan_interrupt_includes_reply_contract(...):
    out = ...
    needs = out["needs_clarification"]
    assert needs["reply_contract"]["kind"] == "approve_or_modify"


def test_office_resume_task_prefers_normalized_clarification_resolution(...):
    metadata = {
        "_interrupt": {
            "needs_clarification": {
                "missing": "organizeCustomPlan",
                "reply_contract": {"kind": "approve_or_modify"},
            }
        }
    }
    resolution = {"contract_kind": "approve_or_modify", "action": "approve", "note": ""}
    ...
    assert updated_metadata["organizeCustomAction"] == "approve"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/unit/agents/test_office_clarification_roundtrip.py -q`
Expected: FAIL because `reply_contract` and normalized resolution are not wired yet.

- [ ] **Step 3: Write minimal implementation**

```python
needs_clarification["reply_contract"] = {
    "schema_version": 1,
    "kind": "approve_or_modify",
    "reask_message": "Please reply with `approve` or `modify: <change>`.",
}

resolution = request_metadata.get("clarificationResolution") or {}
if resolution.get("contract_kind") == "approve_or_modify":
    updated_metadata["organizeCustomAction"] = resolution.get("action")
    updated_metadata["organizeCustomModifyNote"] = resolution.get("note") or ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/unit/agents/test_office_clarification_roundtrip.py -q`
Expected: PASS

### Task 3: Switch Compass resume validation to the shared resolver

**Files:**
- Modify: `agents/compass/agent.py`
- Modify: `agents/compass/tools.py`
- Test: `tests/unit/agents/test_office_clarification_roundtrip.py`
- Test: `tests/unit/agents/compass/test_dimension_first_gate.py`
- Test: `tests/unit/agents/compass/test_resume_robustness.py`
- Test: `tests/unit/agents/test_compass.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_compass_resume_task_reasks_from_reply_contract(...):
    interrupt = {
        "kind": "office_organize_dimension",
        "needs_clarification": {
            "missing": "organizeCustomPlan",
            "reply_contract": {
                "kind": "approve_or_modify",
                "reask_message": "Please reply with `approve` or `modify: <change>`.",
            },
        },
    }
    out = asyncio.run(agent.resume_task(task.id, "sounds good"))
    assert out["ui_update"]["task_status"] == "TASK_STATE_INPUT_REQUIRED"


def test_compass_resume_task_forwards_normalized_resolution_to_same_session(...):
    out = asyncio.run(agent.resume_task(task.id, "approve"))
    forwarded = captured["office_request"]["clarification_resolution"]
    assert forwarded["contract_kind"] == "approve_or_modify"
    assert forwarded["action"] == "approve"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/python -m pytest tests/unit/agents/test_office_clarification_roundtrip.py tests/unit/agents/compass/test_dimension_first_gate.py tests/unit/agents/compass/test_resume_robustness.py tests/unit/agents/test_compass.py -q`
Expected: FAIL because Compass still derives parsing from overloaded interrupt kinds.

- [ ] **Step 3: Write minimal implementation**

```python
reply_contract = dict(needs_clarification.get("reply_contract") or {})
resolution = resolve_reply(reply_contract, str(resume_value))
if not resolution["ok"]:
    # preserve reply_contract and re-ask
else:
    office_request["clarification_resolution"] = {
        "contract_kind": reply_contract.get("kind"),
        **resolution["normalized"],
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/python -m pytest tests/unit/agents/test_office_clarification_roundtrip.py tests/unit/agents/compass/test_dimension_first_gate.py tests/unit/agents/compass/test_resume_robustness.py tests/unit/agents/test_compass.py -q`
Expected: PASS

### Task 4: Regression verification

**Files:**
- Verify only: current working tree changes in `agents/compass/agent.py`, `agents/compass/tools.py`, `agents/office/agent.py`, `framework/clarification_reply.py`, and related tests

- [ ] **Step 1: Run focused regression suite**

Run: `./.venv/bin/python -m pytest tests/unit/framework/test_clarification_reply.py tests/unit/agents/test_office_clarification_roundtrip.py tests/unit/agents/compass/test_dimension_first_gate.py tests/unit/agents/compass/test_resume_robustness.py tests/unit/agents/test_compass.py tests/unit/agents/test_office_agent.py -q`
Expected: PASS

- [ ] **Step 2: Run syntax verification**

Run: `./.venv/bin/python -m py_compile framework/clarification_reply.py agents/compass/agent.py agents/compass/tools.py agents/office/agent.py`
Expected: PASS

- [ ] **Step 3: Summarize follow-up risk**

Record whether any non-Office clarification flow still duplicates reply parsing and should migrate to the shared contract later.

## Self-Review

- Spec coverage: Task 1 covers the shared resolver, Task 2 covers Office emission/consumption, Task 3 covers Compass-side contract-driven parsing with UI continuity, Task 4 covers regression and syntax verification.
- Placeholder scan: no TBD/TODO placeholders remain; every task has concrete files and commands.
- Type consistency: the plan uses `reply_contract` for the waiting-state contract and `clarification_resolution` / `clarificationResolution` for the normalized result that crosses the Compass → Office boundary.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-11-office-reply-contract-implementation.md`.

Two execution options:

1. Subagent-Driven (recommended) - I dispatch a fresh subagent per task, review between tasks, fast iteration
2. Inline Execution - Execute tasks in this session using executing-plans, batch execution with checkpoints

The current request already asks for implementation, so the default continuation is Inline Execution unless you redirect to Subagent-Driven.
