# Skill: Per-Task Agent Exit Rule

## Purpose

Per-task agent containers (Team Lead, Web Agent) must shut down after completing their task to avoid accumulating stale Docker containers. However they must **not** shut down before the parent agent has finished using them (e.g. requesting revisions).

The **Exit Rule** protocol solves this by having the parent declare the shutdown policy for the child, and requiring an explicit ACK before the child shuts down.

---

## Module

`common/per_task_exit.py` — `PerTaskExitHandler`

---

## Exit Rule Types

| Type | Behavior |
|------|----------|
| `wait_for_parent_ack` (default) | Child blocks shutdown until parent calls `POST /tasks/{id}/ack`, or timeout elapses |
| `immediate` | Child shuts down immediately after callback (legacy `AUTO_STOP_AFTER_TASK=1` behavior) |
| `persistent` | No auto-stop (for always-on agents) |

Default ACK timeout: `300` seconds (5 minutes). Configurable via `ack_timeout_seconds` in the rule dict.

---

## Message Metadata Contract

The parent embeds the exit rule when dispatching a task to a per-task child:

```python
# In parent (Team Lead), using the helper:
from common.per_task_exit import PerTaskExitHandler

metadata = {
    ...
    "exitRule": PerTaskExitHandler.build(
        rule_type="wait_for_parent_ack",
        ack_timeout_seconds=300,
    ),
}
```

The child reads it at the start of its workflow:

```python
exit_rule = PerTaskExitHandler.parse(metadata)
```

---

## Child Agent Implementation Pattern

```python
# Module-level singleton (web/app.py, team-lead/app.py, etc.)
exit_handler = PerTaskExitHandler()

def _run_workflow(task_id, message):
    metadata = message.get("metadata", {})
    exit_rule = PerTaskExitHandler.parse(metadata)
    ...
    try:
        # ... do work ...
        _notify_callback(callback_url, task_id, "TASK_STATE_COMPLETED", ...)
    except Exception:
        ...
    finally:
        # Apply exit rule in a background thread (non-blocking)
        _apply_task_exit_rule(task_id, exit_rule)

def _apply_task_exit_rule(task_id, exit_rule):
    def _run():
        exit_handler.apply(task_id, exit_rule, shutdown_fn=_schedule_shutdown, agent_id=AGENT_ID)
    threading.Thread(target=_run, daemon=True).start()
```

---

## ACK Endpoint (Required in all per-task agents)

```python
# POST /tasks/{id}/ack — parent confirms it is done with this task
m_ack = re.fullmatch(r"/tasks/([^/]+)/ack", path)
if m_ack:
    task_id = m_ack.group(1)
    exit_handler.acknowledge(task_id)
    self._send_json(200, {"ok": True, "task_id": task_id})
    return
```

---

## Parent Agent ACK Pattern

### Team Lead → Web Agent

After all review cycles are done (whether passed or timed out), Team Lead ACKs the Web Agent:

```python
# After review loop in team-lead/app.py
if ctx.dev_service_url and ctx.dev_task_id:
    _ack_agent(ctx.dev_service_url, ctx.dev_task_id)
```

If Team Lead fails mid-task (exception path), it must still ACK in the `except` block:

```python
except Exception as err:
    if ctx.dev_service_url and ctx.dev_task_id:
        _ack_agent(ctx.dev_service_url, ctx.dev_task_id)
```

### Compass → Team Lead

Compass ACKs Team Lead after the completeness gate passes or max revisions are reached:

```python
_send_agent_ack(service_url, downstream_task_id)
```

---

## Revision Handling (Critical)

For revision requests, Team Lead **reuses the same dev-agent container** — it does NOT launch a new one. This is the correct pattern:

```python
# CORRECT: reuse stored service URL
revision_service_url = ctx.dev_service_url
rev_task = _a2a_send(revision_service_url, revision_message)
ctx.dev_task_id = rev_task.get("id")
```

```python
# WRONG: launching a new container for revision
revision_agent_def, revision_instance, revision_service_url = _acquire_dev_agent(...)
```

The `wait_for_parent_ack` rule keeps the original container alive so it can accept a second `POST /message:send` for the revision.

---

## `_ack_agent` Helper

Both Team Lead and Compass have an `_ack_agent` / `_send_agent_ack` helper:

```python
def _ack_agent(service_url: str, task_id: str) -> None:
    """Best-effort ACK — does not raise on failure."""
    request = Request(
        f"{service_url.rstrip('/')}/tasks/{task_id}/ack",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=10):
            pass
    except Exception as err:
        print(f"[{AGENT_ID}] Could not ACK agent: {err}")
```

---

## Sequence Diagram

```
Compass → Team Lead:   POST /message:send  (exitRule: wait_for_parent_ack)
Team Lead → Web Agent: POST /message:send  (exitRule: wait_for_parent_ack)

Web Agent completes work → POST {team-lead}/tasks/{tl_task}/callbacks
Web Agent: exit_handler.wait(web_task_id, timeout=300) ← blocks

Team Lead reviews output (cycle 1/N)
  if review passes:
    _ack_agent(web_service_url, web_task_id)  ← Web Agent unblocks, schedules shutdown
  if review fails:
    POST {web_service_url}/message:send   (same container, new task)
    ... wait for callback ...
    _ack_agent(web_service_url, new_task_id)  ← after last cycle

Team Lead completes → POST {compass}/tasks/{compass_task}/callbacks
Team Lead: exit_handler.wait(tl_task_id, timeout=300) ← blocks

Compass completeness gate passes → POST {team-lead}/tasks/{tl_task}/ack
Team Lead unblocks → schedules shutdown
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `AUTO_STOP_AFTER_TASK` | `""` | Set to `1` to enable shutdown after task. Required for exit rules to trigger actual shutdown. |
| `DEV_AGENT_ACK_TIMEOUT_SECONDS` | `300` | Timeout Team Lead waits for Web Agent ACK (if Team Lead is the parent). |
| `COMPASS_ACK_TIMEOUT_SECONDS` | `300` | Timeout Team Lead waits for Compass ACK before shutting down. |
