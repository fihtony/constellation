"""Compass UI templates -- finalized three-column dark workspace.

Layout:
  - Left:   Task List   (pinned ``New Request`` + newest-first real tasks)
  - Middle: Task Chat   (scoped to the selected task; bottom-fixed composer)
  - Right:  Task Info   (overview, major steps, live logs with filters)

All runtime labels MUST be English. The UI hydrates initial state from
``/api/tasks`` and live-updates from ``/ui/events`` (Compass SSE) and
``/logs/stream/{task_id}`` (Log Store SSE).
"""
from __future__ import annotations

import html


def render_chat_message(role: str, text: str, style: str = "normal") -> str:
    """Render a chat message bubble (server-side preview for tests).

    The live UI renders bubbles from JSON via JavaScript, but unit tests and
    no-JS clients still need a meaningful HTML preview.
    """
    safe_role = html.escape(str(role or ""))
    safe_text = html.escape(str(text or "")).replace("\n", "<br>")
    role_lower = (role or "").lower()
    bubble_class = "user" if role_lower in {"user", "role_user"} else "agent"
    style_class = {
        "normal": "",
        "waiting": "waiting",
        "input-required": "waiting",
        "failed": "failed",
        "completed": "completed",
    }.get(style, "")
    classes = f"bubble {bubble_class} {style_class}".strip()
    return (
        f'<div class="{classes}" data-role="{safe_role}" data-style="{html.escape(style)}">'
        f'<div class="bubble-label">{safe_role}</div>'
        f'<div class="bubble-text">{safe_text}</div>'
        f'</div>'
    )


def render_task_tab(task_id: str, status: str, summary: str = "") -> str:
    """Render a task row in the Task List (server-side preview)."""
    safe_id = html.escape(str(task_id or ""))
    safe_summary = html.escape(str(summary or ""))
    primary_label = safe_summary or safe_id
    secondary_label = safe_id if safe_summary else ""
    status_text = {
        "active": "In Progress",
        "waiting": "Waiting for Input",
        "completed": "Completed",
        "failed": "Failed",
    }.get(status, "In Progress")
    safe_status = html.escape(status or "active")
    safe_status_text = html.escape(status_text)
    return (
        f'<div class="task-item" data-task-id="{safe_id}" data-status="{safe_status}">'
        f'<div class="task-title">{primary_label}</div>'
        f'<div class="task-tag-row">'
        f'<span class="status-pill {safe_status}">{safe_status_text}</span>'
        f'</div>'
        f'<div class="task-note">{secondary_label}</div>'
        f'</div>'
    )


_INLINE_CSS = """
:root {
  --bg: #081018;
  --bg-accent: #0d1723;
  --panel: rgba(13, 23, 35, 0.88);
  --panel-strong: #101b29;
  --panel-soft: rgba(17, 30, 45, 0.78);
  --ink: #e6f0f7;
  --muted: #93a6b6;
  --line: rgba(154, 176, 196, 0.16);
  --accent: #7fc3d1;
  --accent-soft: rgba(127, 195, 209, 0.10);
  --accent-strong: #b2dbe3;
  --ok-bg: rgba(22, 163, 74, 0.16);
  --ok-ink: #63e6a5;
  --wait-bg: rgba(245, 158, 11, 0.14);
  --wait-ink: #ffd36f;
  --progress-bg: rgba(127, 195, 209, 0.12);
  --progress-ink: #a6d1dc;
  --error-bg: rgba(239, 68, 68, 0.16);
  --error-ink: #ff9191;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: "IBM Plex Sans", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  color: var(--ink);
  background:
    radial-gradient(circle at top left, rgba(127,195,209,0.10), transparent 28%),
    radial-gradient(circle at right 20%, rgba(55,88,116,0.18), transparent 24%),
    linear-gradient(180deg, var(--bg), var(--bg-accent));
  overflow: hidden;
}
.workspace {
  display: grid;
  grid-template-columns: clamp(250px, 22vw, 310px) minmax(340px, 1.02fr) minmax(360px, 1.16fr);
  gap: 16px;
  padding: 16px;
  height: 100vh;
  min-width: 0;
}
.panel {
  display: flex;
  flex-direction: column;
  min-height: 0;
  border-radius: 18px;
  border: 1px solid var(--line);
  background: var(--panel);
  overflow: hidden;
}
.panel-head {
  padding: 14px 18px;
  border-bottom: 1px solid var(--line);
  background: rgba(17, 30, 45, 0.92);
}
.panel-head .kicker {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--accent);
}
.panel-head h3 { margin-top: 4px; font-size: 16px; }
.panel-head p {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--muted);
}
.panel-body {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
#task-list-panel .panel-body { padding: 12px; overflow-y: auto; }
.tasks-overview-strip {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-bottom: 12px;
}
.overview-chip {
  padding: 10px 12px;
  border-radius: 12px;
  border: none;
  background: linear-gradient(180deg, rgba(15, 26, 38, 0.95), rgba(12, 21, 31, 0.9));
  box-shadow: 0 1px 3px rgba(0,0,0,0.2);
}
.overview-chip strong {
  display: block;
  font-size: 16px;
  line-height: 1;
}
.overview-chip span {
  display: block;
  margin-top: 6px;
  font-size: 11px;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.08em;
}
.overview-chip.attention strong { color: var(--wait-ink); }
.overview-chip.active strong { color: var(--accent-strong); }
.overview-chip.done strong { color: var(--ok-ink); }
.focus-note {
  margin-bottom: 12px;
  padding: 10px 12px;
  border-radius: 14px;
  border: 1px solid rgba(245, 158, 11, 0.16);
  background: linear-gradient(180deg, rgba(65, 44, 19, 0.58), rgba(36, 27, 16, 0.46));
}
.focus-note .focus-label {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--wait-ink);
}
.focus-note .focus-text {
  margin-top: 6px;
  font-size: 12px;
  line-height: 1.55;
}
.task-list { display: flex; flex-direction: column; gap: 8px; }
.task-item {
  position: relative;
  padding: 12px;
  border-radius: 12px;
  border: none;
  background: rgba(15, 25, 36, 0.7);
  cursor: pointer;
  box-shadow: 0 1px 3px rgba(0,0,0,0.15);
  transition: box-shadow 0.15s ease, transform 0.1s ease;
}
.task-item:hover { box-shadow: 0 2px 6px rgba(127,195,209,0.2); transform: translateY(-1px); }
.task-item.active {
  box-shadow: 0 0 0 1px rgba(127,195,209,0.4), 0 2px 8px rgba(127,195,209,0.15);
}
.task-item.new-request { background: rgba(127,195,209,0.05); border-color: rgba(127,195,209,0.18); }
.task-item-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}
.task-title { font-size: 12px; font-weight: 700; letter-spacing: 0.02em; }
.task-type {
  padding: 3px 8px;
  border-radius: 999px;
  background: rgba(127, 195, 209, 0.08);
  color: var(--accent-strong);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.task-note { margin-top: 6px; font-size: 11px; line-height: 1.55; color: var(--muted); }
.task-tag-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.task-step-preview {
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid rgba(154,176,196,0.1);
  font-size: 11px;
  line-height: 1.5;
  color: #c8d8e4;
}
.task-step-preview span {
  display: block;
  margin-bottom: 3px;
  color: var(--muted);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 9px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
}
.status-pill::before {
  content: "";
  width: 6px; height: 6px; border-radius: 50%; background: currentColor;
}
.status-pill.active   { background: var(--progress-bg); color: var(--progress-ink); }
.status-pill.waiting  { background: var(--wait-bg);     color: var(--wait-ink); }
.status-pill.completed{ background: var(--ok-bg);       color: var(--ok-ink); }
.status-pill.failed   { background: var(--error-bg);    color: var(--error-ink); }
.wait-badge {
  position: absolute; top: 10px; right: 10px;
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--wait-ink);
  box-shadow: 0 0 0 3px rgba(147,100,0,0.16);
}

/* Chat */
#task-chat-panel .panel-body { padding: 14px; }
.chat-context-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
  padding: 10px 12px;
  border-radius: 14px;
  border: 1px solid rgba(154, 176, 196, 0.08);
  background: rgba(15, 25, 36, 0.84);
  margin-bottom: 10px;
}
.chat-context-copy {
  font-size: 12px;
  line-height: 1.5;
  color: var(--muted);
}
.chat-context-pill {
  padding: 4px 8px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  background: rgba(127, 195, 209, 0.08);
  color: var(--accent-strong);
}
.chat-scroll {
  flex: 1; min-height: 0; overflow-y: auto;
  display: flex; flex-direction: column; gap: 10px;
  padding-right: 4px;
}
.bubble {
  max-width: 88%;
  padding: 10px 14px;
  border-radius: 16px;
  border: 1px solid var(--line);
  background: #121d2b;
  line-height: 1.6;
  font-size: 13px;
  word-wrap: break-word;
}
.bubble .bubble-label {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 4px;
}
.bubble.user { margin-left: auto; background: #74b8c6; border-color: transparent; color: #071218; }
.bubble.user .bubble-label { color: #163743; }
.bubble.waiting { border-left: 4px solid var(--wait-ink); background: var(--wait-bg); }
.bubble.failed  { border-left: 4px solid var(--error-ink); background: var(--error-bg); }
.bubble.completed { border-left: 4px solid var(--ok-ink); background: var(--ok-bg); }
.composer {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--line);
}
.composer-note {
  margin-bottom: 8px;
  padding: 8px 10px;
  border-radius: 10px;
  background: var(--wait-bg);
  color: var(--wait-ink);
  font-size: 12px;
}
.composer-box {
  display: flex; gap: 8px;
}
#composer-input {
  flex: 1;
  padding: 10px 12px;
  border-radius: 12px;
  border: 1px solid var(--line);
  background: #101b29;
  color: var(--ink);
  font-size: 13px;
  font-family: inherit;
  resize: none;
}
#composer-input:disabled { opacity: 0.5; cursor: not-allowed; }
#composer-send {
  padding: 0 18px;
  border-radius: 12px;
  border: none;
  background: var(--accent);
  color: #06121b;
  font-weight: 700;
  cursor: pointer;
}
#composer-send:disabled { opacity: 0.5; cursor: not-allowed; }

/* Detail */
#task-info-panel .panel-body { padding: 16px; overflow-y: auto; }
.detail-stack { display: flex; flex-direction: column; gap: 0; }
.detail-card {
  padding: 14px 0 0;
  border-radius: 0;
  border: none;
  background: transparent;
}
.detail-card + .detail-card {
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid rgba(154, 176, 196, 0.08);
}
.detail-card.spotlight {
  padding: 16px;
  border-radius: 16px;
  border: none;
  background:
    linear-gradient(135deg, rgba(127, 195, 209, 0.12) 0%, transparent 50%),
    linear-gradient(180deg, rgba(15, 26, 38, 0.95), rgba(12, 21, 32, 0.9));
}
.detail-card h4 { margin-top: 6px; font-size: 17px; letter-spacing: -0.01em; }
.detail-section {
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px solid rgba(154, 176, 196, 0.08);
}
.detail-section:first-of-type {
  margin-top: 10px;
}
.detail-inline-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.3fr) minmax(220px, 0.9fr);
  gap: 12px;
}
.detail-label {
  display: block;
  margin-bottom: 6px;
  color: var(--muted);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.detail-value {
  font-size: 13px;
  line-height: 1.6;
}
.meta-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  margin-top: 10px;
}
.meta {
  padding: 6px 0 0;
  border-radius: 0;
  background: transparent;
  border: none;
  min-width: 120px;
}
.meta-label {
  display: block;
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 4px;
  font-weight: 700;
}
.meta-value { font-size: 12px; line-height: 1.5; word-break: break-word; }
.kicker {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--accent);
}
.steps { display: flex; flex-direction: column; gap: 8px; margin-top: 10px; }
.step {
  display: grid;
  grid-template-columns: 24px 1fr;
  gap: 10px;
  padding: 8px 2px 12px;
  border-radius: 0;
  border: none;
  border-bottom: 1px solid rgba(154,176,196,0.08);
  background: transparent;
  font-size: 12px;
}
.step.current {
  background: linear-gradient(90deg, rgba(127,195,209,0.10), rgba(127,195,209,0.03));
  border-bottom-color: rgba(127,195,209,0.18);
}
.step-index {
  width: 24px; height: 24px;
  border-radius: 50%;
  background: #74b8c6;
  color: #071218;
  font-size: 11px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.step-agent {
  display: inline-flex;
  align-items: center;
  padding: 3px 8px;
  border-radius: 999px;
  background: rgba(127,195,209,0.10);
  color: var(--accent-strong);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.step-title {
  margin-top: 6px;
  line-height: 1.6;
}
.step-meta { color: var(--muted); font-size: 11px; margin-top: 4px; }
.step-digest {
  margin-top: 8px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.55;
}
.phase-banner {
  margin-top: 12px;
  padding: 12px 14px;
  border-radius: 14px;
  background: linear-gradient(180deg, rgba(15, 26, 38, 0.84), rgba(11, 20, 30, 0.72));
  border: 1px solid rgba(127,195,209,0.10);
}
.phase-banner-label {
  display: block;
  color: var(--muted);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.phase-banner strong {
  display: block;
  margin-top: 6px;
  font-size: 16px;
  color: var(--ink);
}
.phase-banner span:last-child {
  display: block;
  margin-top: 6px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.55;
}
.phase-rail {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
  margin-bottom: 12px;
}
.phase-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  border-radius: 999px;
  background: rgba(127,195,209,0.08);
  color: var(--muted);
  font-size: 11px;
  font-weight: 700;
}
.phase-pill.current {
  background: rgba(127,195,209,0.18);
  color: var(--ink);
}
.phase-pill.done {
  color: var(--accent-strong);
}
.phase-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.workflow-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.workflow-toggle {
  appearance: none;
  border: 1px solid rgba(154, 176, 196, 0.12);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--muted);
  font: inherit;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.02em;
  padding: 6px 10px;
  cursor: pointer;
}
.workflow-toggle:hover {
  color: var(--ink);
  border-color: rgba(127,195,209,0.22);
}
.phase-row {
  padding: 8px 0 12px;
  border-bottom: 1px solid rgba(154,176,196,0.08);
}
.phase-row.current {
  padding-left: 10px;
  border-left: 2px solid rgba(127,195,209,0.42);
  background: linear-gradient(90deg, rgba(127,195,209,0.08), transparent 70%);
}
.phase-row:last-child {
  border-bottom: none;
}
.phase-row-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.phase-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--ink);
}
.phase-count {
  display: inline-flex;
  align-items: center;
  padding: 3px 8px;
  border-radius: 999px;
  background: rgba(127,195,209,0.08);
  color: var(--muted);
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.04em;
}
.phase-detail {
  margin-top: 6px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.55;
}
.phase-time {
  font-size: 10px;
  color: var(--muted);
  letter-spacing: 0.04em;
}

/* Logs */
.log-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 12px 18px;
  align-items: center;
  margin-top: 10px;
  padding-bottom: 10px;
  border-bottom: 1px solid rgba(154, 176, 196, 0.08);
}
.log-toolbar-text {
  display: inline-flex;
  gap: 6px;
  align-items: baseline;
  font-size: 12px;
  color: var(--muted);
}
.log-toolbar-text strong {
  color: var(--ink);
  font-size: 14px;
}
.log-filters {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  align-items: center;
}
.log-filters label {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  color: var(--muted);
  font-size: 12px;
}
.log-filters select {
  padding: 5px 9px;
  border-radius: 999px;
  border: 1px solid rgba(154, 176, 196, 0.14);
  background: rgba(9, 16, 23, 0.42);
  color: var(--ink);
  font-size: 11px;
  font-family: inherit;
}
.log-box {
  margin-top: 10px;
  border-radius: 12px;
  border: 1px solid rgba(154, 176, 196, 0.08);
  background: rgba(9, 16, 23, 0.4);
  max-height: 380px;
  overflow-y: auto;
}
.log-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  align-items: center;
  padding: 10px 12px;
  border-bottom: 1px solid rgba(154, 176, 196, 0.08);
  background: rgba(15, 26, 38, 0.5);
}
.log-line {
  display: grid;
  grid-template-columns: 70px 42px 64px 1fr;
  gap: 8px;
  padding: 7px 0;
  border-bottom: 1px solid rgba(255,255,255,0.06);
  font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
  font-size: 10.5px;
  line-height: 1.4;
  color: #d7e5ec;
}
.log-ts {
  color: var(--muted);
  font-size: 9.5px;
  display: flex;
  flex-direction: column;
  gap: 1px;
  line-height: 1.2;
}
.log-date,
.log-time {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.log-level {
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.04em;
  padding: 2px 5px;
  border-radius: 4px;
  text-align: center;
}
.log-agent {
  font-size: 9.5px;
  font-weight: 600;
  color: var(--accent);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.log-msg { word-break: break-word; }
.log-line:last-child { border-bottom: none; }
.log-level { font-size: 9px; font-weight: 700; letter-spacing: 0.06em; padding: 2px 6px; border-radius: 4px; text-align: center; }
.log-level.ERROR { background: rgba(239, 68, 68, 0.25); color: #ff9191; }
.log-level.WARN  { background: rgba(245, 158, 11, 0.2); color: #ffd36f; }
.log-level.INFO  { background: rgba(127, 195, 209, 0.15); color: #b2dbe3; }
.log-level.DEBUG { background: rgba(147, 166, 182, 0.1); color: #93a6b6; }
.empty-state {
  padding: 22px;
  color: var(--muted);
  font-size: 13px;
  text-align: center;
}
@media (max-width: 1500px) {
  body { overflow: auto; }
  .workspace {
    height: auto;
    min-height: 100vh;
    grid-template-columns: minmax(230px, 290px) minmax(0, 1fr);
  }
  #task-list-panel {
    grid-column: 1;
    grid-row: 1 / span 2;
  }
  #task-chat-panel {
    grid-column: 2;
    grid-row: 1;
    min-height: 420px;
  }
  #task-info-panel {
    grid-column: 2;
    grid-row: 2;
    min-height: 460px;
  }
}
@media (max-width: 1100px) {
  .workspace {
    grid-template-columns: 1fr;
    gap: 12px;
    padding: 12px;
  }
  #task-list-panel,
  #task-chat-panel,
  #task-info-panel {
    grid-column: auto;
    grid-row: auto;
    min-height: 0;
  }
  .tasks-overview-strip,
  .detail-inline-grid,
  .meta-grid {
    grid-template-columns: 1fr;
  }
  .log-line {
    grid-template-columns: 1fr;
    gap: 4px;
  }
}
"""


_INLINE_JS = r"""
(() => {
  'use strict';
  const NEW_REQUEST_ID = '__new_request__';

  const state = {
    selectedTaskId: NEW_REQUEST_ID,
    tasks: {},   // task_id -> task object
    order: [],   // task_ids newest-first
    logsByTask: {},
    logEventSources: {},
    phaseExpandedByTask: {},
    filters: { agent: 'all', level: 'all' },
  };

  function $(sel) { return document.querySelector(sel); }
  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, c =>
      ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
  }
  function statusKindOf(st) {
    return ({
      'TASK_STATE_COMPLETED': 'completed',
      'TASK_STATE_FAILED':    'failed',
      'TASK_STATE_INPUT_REQUIRED': 'waiting',
      'TASK_STATE_WORKING': 'active',
      'TASK_STATE_SUBMITTED': 'active',
    })[st] || 'active';
  }
  function statusLabel(kind) {
    return ({ active:'In Progress', waiting:'Waiting for Input',
              completed:'Completed', failed:'Failed' })[kind] || 'In Progress';
  }
  function nextActionForTask(task, kind) {
    if (kind === 'waiting') return `Reply in chat to resume ${task.task_id}.`;
    if (kind === 'failed') return 'Inspect the latest logs and retry after correcting the failing step.';
    if (kind === 'completed') return 'Review the output and decide whether any follow-up work is needed.';
    return 'Monitor the active major step and use the log stream for live execution detail.';
  }
  function developmentPhaseForText(text) {
    const value = String(text || '').toLowerCase();
    if (!value) return { key: 'other', label: 'Other' };
    if (value.includes('fix') || value.includes('fixing') || value.includes('revision') || value.includes('gap') || value.includes('retry') || value.includes('repair')) {
      return { key: 'fix', label: 'Fix' };
    }
    if (value.includes('analysis') || value.includes('plan') || value.includes('context') || value.includes('jira') || value.includes('design')) {
      return { key: 'plan', label: 'Plan' };
    }
    if (value.includes('implement') || value.includes('code') || value.includes('branch') || value.includes('change')) {
      return { key: 'implement', label: 'Implement' };
    }
    if (value.includes('build') || value.includes('compile') || value.includes('dist/')) {
      return { key: 'build', label: 'Build' };
    }
    if (value.includes('test') || value.includes('validation') || value.includes('vitest') || value.includes('jest')) {
      return { key: 'test', label: 'Test' };
    }
    if (value.includes('self assessment') || value.includes('self-assessment') || value.includes('self check') || value.includes('self-check') || value.includes('verify') || value.includes('screenshot')) {
      return { key: 'self-check', label: 'Self-check' };
    }
    if (value.includes('review') || value.includes('pr') || value.includes('deliver') || value.includes('report')) {
      return { key: 'deliver', label: 'Review & Deliver' };
    }
    return { key: 'other', label: 'Other' };
  }
  function deriveMajorPhases(task, currentStep) {
    const steps = Array.isArray(task.progressSteps) ? task.progressSteps : [];
    if ((task.taskType || task.task_type) !== 'development') return null;
    const phaseMap = new Map();
    function ensurePhaseBucket(phase, fallbackAgent) {
      if (!phaseMap.has(phase.key)) {
        phaseMap.set(phase.key, {
          key: phase.key,
          label: phase.label,
          agent: fallbackAgent || '',
          detail: '',
          preview: '',
          ts: '',
          items: [],
        });
      }
      return phaseMap.get(phase.key);
    }
    for (const step of steps) {
      const text = step.text || step.step || '';
      const phase = developmentPhaseForText(text);
      if (phase.key === 'other') continue;
      const bucket = ensurePhaseBucket(phase, step.agent || '');
      bucket.agent = bucket.agent || step.agent || '';
      bucket.detail = text;
      bucket.preview = text;
      bucket.ts = step.ts || bucket.ts;
      bucket.items.push({ text, agent: step.agent || '', ts: step.ts || '' });
    }
    const currentPhase = developmentPhaseForText(currentStep || '');
    if (currentPhase.key !== 'other') {
      const bucket = ensurePhaseBucket(currentPhase, '');
      if (currentStep) {
        bucket.preview = currentStep;
        bucket.detail = currentStep;
        if (!bucket.items.some(item => item.text === currentStep)) {
          bucket.items.push({ text: currentStep, agent: bucket.agent || '', ts: '' });
        }
      }
    }
    const ordered = ['plan', 'implement', 'build', 'test', 'self-check', 'fix', 'deliver']
      .filter(key => phaseMap.has(key))
      .map(key => {
        const phase = phaseMap.get(key);
        return {
          ...phase,
          updateCount: phase.items.length,
          detail: phase.preview || phase.detail || phase.label,
        };
      });
    return ordered.length ? { currentKey: currentPhase.key, phases: ordered } : null;
  }
  function looksGenericSummary(text, kind) {
    const value = String(text || '').trim().toLowerCase();
    if (!value) return true;
    if (value === 'resumed') return true;
    if (value === statusLabel(kind).toLowerCase()) return true;
    return value.startsWith('office task dispatched. status:') || value.startsWith('development task dispatched.');
  }
  function displayTitle(task, kind) {
    const summary = String(task.summary || '').trim();
    const request = String(task.userRequest || '').trim();
    if (summary && !looksGenericSummary(summary, kind)) return summary;
    if (request) return request;
    return task.task_id || task.id || 'Task';
  }
  function priorityRank(kind) {
    return ({ waiting: 0, failed: 1, active: 2, completed: 3 })[kind] ?? 4;
  }
  function orderedTaskIds() {
    return [...state.order].sort((a, b) => {
      const ta = state.tasks[a], tb = state.tasks[b];
      const ra = priorityRank(statusKindOf((ta && (ta.statusState || ta.status)) || ''));
      const rb = priorityRank(statusKindOf((tb && (tb.statusState || tb.status)) || ''));
      if (ra !== rb) return ra - rb;
      const ka = (ta && ta.createdAt) || a;
      const kb = (tb && tb.createdAt) || b;
      return ka < kb ? 1 : (ka > kb ? -1 : 0);
    });
  }
  function renderTaskOverview() {
    const strip = $('#tasks-overview-strip');
    if (!strip) return;
    const tasks = Object.values(state.tasks);
    const waiting = tasks.filter(t => statusKindOf(t.statusState || t.status) === 'waiting').length;
    const active = tasks.filter(t => statusKindOf(t.statusState || t.status) === 'active').length;
    const completed = tasks.filter(t => statusKindOf(t.statusState || t.status) === 'completed').length;
    strip.innerHTML = `
      <div class="overview-chip attention"><strong>${waiting}</strong><span>Needs Attention</span></div>
      <div class="overview-chip active"><strong>${active}</strong><span>In Progress</span></div>
      <div class="overview-chip done"><strong>${completed}</strong><span>Completed</span></div>`;
    const focus = $('#task-focus-note');
    if (!focus) return;
    const nextWaiting = orderedTaskIds().map(id => state.tasks[id]).find(t => statusKindOf(t.statusState || t.status) === 'waiting');
    if (!nextWaiting) {
      focus.style.display = 'none';
      focus.innerHTML = '';
      return;
    }
    focus.style.display = 'block';
    focus.innerHTML = `<div class="focus-label">Focus</div><div class="focus-text">${esc(displayTitle(nextWaiting, 'waiting'))}<br>${esc(nextActionForTask(nextWaiting, 'waiting'))}</div>`;
  }
  function fmtTime(iso) {
    if (!iso) return '';
    try { const d = new Date(iso); if (isNaN(d.getTime())) return iso; return d.toLocaleString(); }
    catch { return iso; }
  }
  function fmtLogTimestamp(iso) {
    if (!iso) return { date: '', time: '' };
    try {
      const d = new Date(iso);
      if (isNaN(d.getTime())) return { date: iso, time: '' };
      return {
        date: d.toLocaleString([], { month: '2-digit', day: '2-digit' }),
        time: d.toLocaleString([], { hour: '2-digit', minute: '2-digit' }),
      };
    } catch {
      return { date: iso, time: '' };
    }
  }
  function elapsedMs(createdIso, updatedIso) {
    if (!createdIso) return '';
    const start = new Date(createdIso).getTime();
    const end = updatedIso ? new Date(updatedIso).getTime() : Date.now();
    if (isNaN(start) || isNaN(end)) return '';
    const sec = Math.max(0, Math.round((end - start) / 1000));
    const m = Math.floor(sec / 60), s = sec % 60;
    return `${String(m).padStart(2,'0')}m ${String(s).padStart(2,'0')}s`;
  }

  function upsertTask(task) {
    if (!task || !task.task_id) return;
    state.tasks[task.task_id] = task;
    if (!state.order.includes(task.task_id)) state.order.unshift(task.task_id);
    state.order.sort((a, b) => {
      const ta = state.tasks[a], tb = state.tasks[b];
      const ka = (ta && ta.createdAt) || a, kb = (tb && tb.createdAt) || b;
      return ka < kb ? 1 : (ka > kb ? -1 : 0);
    });
  }

  async function loadTasks(autoSelect) {
    try {
      const resp = await fetch('/api/tasks');
      const data = await resp.json();
      state.tasks = {}; state.order = [];
      for (const t of (data.tasks || [])) upsertTask(t);
      if (autoSelect && !state.tasks[state.selectedTaskId] && state.selectedTaskId !== NEW_REQUEST_ID) {
        state.selectedTaskId = state.order[0] || NEW_REQUEST_ID;
      }
      if (state.selectedTaskId !== NEW_REQUEST_ID && state.tasks[state.selectedTaskId]) {
        await loadTaskDetail(state.selectedTaskId);
      }
      renderTaskList(); renderChat(); renderDetail();
      if (state.selectedTaskId !== NEW_REQUEST_ID) subscribeLogs(state.selectedTaskId);
    } catch (e) { console.error('loadTasks failed', e); }
  }

  async function loadTaskDetail(tid) {
    if (!tid || tid === NEW_REQUEST_ID) return;
    try {
      const resp = await fetch(`/api/tasks/${encodeURIComponent(tid)}`);
      const data = await resp.json();
      const task = data.task || data;
      if (task && task.task_id) upsertTask(task);
    } catch (e) {
      console.error('loadTaskDetail failed', e);
    }
  }

  function renderTaskList() {
    const root = $('#task-list');
    let html = '';
    renderTaskOverview();
    const active = state.selectedTaskId === NEW_REQUEST_ID ? ' active' : '';
    html += `<div class="task-item new-request${active}" data-task-id="${NEW_REQUEST_ID}">
      <div class="task-title">New Request</div>
      <div class="task-note">Start a new Compass task here.</div>
    </div>`;
    for (const tid of orderedTaskIds()) {
      const t = state.tasks[tid]; if (!t) continue;
      const kind = statusKindOf(t.statusState || t.status);
      const isActive = tid === state.selectedTaskId;
      const waitBadge = kind === 'waiting' ? '<span class="wait-badge"></span>' : '';
      const typeLabel = esc((t.taskType || t.task_type || 'general').replace(/_/g, ' '));
      const stepPreview = esc(t.currentMajorStep || '');
      const titleText = esc(displayTitle(t, kind));
      html += `<div class="task-item${isActive?' active':''}" data-task-id="${esc(tid)}" data-status="${esc(kind)}">
        ${waitBadge}
        <div class="task-item-head">
          <div class="task-title">${titleText}</div>
          <div class="task-type">${typeLabel}</div>
        </div>
        <div class="task-tag-row"><span class="status-pill ${esc(kind)}">${esc(statusLabel(kind))}</span></div>
        <div class="task-note">${esc(tid)}</div>
        ${stepPreview ? `<div class="task-step-preview"><span>Current Step</span>${stepPreview}</div>` : ''}
      </div>`;
    }
    root.innerHTML = html;
    root.querySelectorAll('.task-item').forEach(el => {
      el.addEventListener('click', () => selectTask(el.getAttribute('data-task-id')));
    });
  }

  function selectTask(tid) {
    if (!tid) return;
    state.selectedTaskId = tid;
    renderTaskList(); renderChat(); renderDetail();
    if (tid !== NEW_REQUEST_ID) subscribeLogs(tid);
    loadTaskDetail(tid).then(() => {
      renderTaskList(); renderChat(); renderDetail();
    });
  }

  function renderChat() {
    const tid = state.selectedTaskId;
    const head = $('#chat-head-sub');
    const context = $('#chat-context-bar');
    const scroll = $('#chat-scroll');
    const composerInput = $('#composer-input');
    const composerSend  = $('#composer-send');
    const composerNote  = $('#composer-note');

    if (tid === NEW_REQUEST_ID) {
      head.textContent = 'New Request';
      context.innerHTML = `<span class="chat-context-pill">Reply Route</span><span class="chat-context-copy">This composer will create a brand-new Compass task.</span>`;
      scroll.innerHTML = `<div class="empty-state">Send a request to create a new Compass task.</div>`;
      composerNote.style.display = 'none';
      composerInput.disabled = false;
      composerInput.placeholder = 'Describe a new task...';
      composerSend.disabled = false;
      composerSend.textContent = 'Create';
      composerInput.dataset.mode = 'create';
      composerInput.dataset.targetTaskId = '';
      return;
    }

    const t = state.tasks[tid];
    if (!t) {
      head.textContent = tid;
      scroll.innerHTML = `<div class="empty-state">Task not loaded.</div>`;
      return;
    }
    head.textContent = `Conversation for ${tid}`;

    const history = Array.isArray(t.chatHistory) ? t.chatHistory : [];
    if (!history.length) {
      scroll.innerHTML = `<div class="empty-state">No chat history yet.</div>`;
    } else {
      scroll.innerHTML = history.map(entry => {
        const role = (entry.role || 'agent').toLowerCase();
        const tone = entry.tone || (entry.style || 'normal');
        const cls = role === 'user' ? 'user' : 'agent';
        const styleCls = ({ waiting:'waiting','input-required':'waiting',failed:'failed',completed:'completed'})[tone] || '';
        return `<div class="bubble ${cls} ${styleCls}">
          <div class="bubble-label">${esc(entry.role || 'AGENT')}</div>
          <div class="bubble-text">${esc(entry.text || '').replace(/\n/g,'<br>')}</div>
        </div>`;
      }).join('');
    }
    scroll.scrollTop = scroll.scrollHeight;

    const kind = statusKindOf(t.statusState || t.status);
    const isWaiting = kind === 'waiting';
    context.innerHTML = `<span class="chat-context-pill">Reply Route</span><span class="chat-context-pill">${esc(statusLabel(kind))}</span><span class="chat-context-pill">${esc(t.taskType || t.task_type || 'general')}</span><span class="chat-context-copy">${esc(nextActionForTask(t, kind))}</span>`;
    head.textContent = displayTitle(t, kind);
    composerInput.disabled = false;
    composerSend.disabled = false;
    if (isWaiting) {
      composerNote.style.display = 'block';
      composerNote.textContent = `Sending here will resume task ${tid} (not create a new task).`;
      composerInput.placeholder = `Reply to ${tid}...`;
      composerSend.textContent = 'Resume';
      composerInput.dataset.mode = 'resume';
      composerInput.dataset.targetTaskId = tid;
    } else {
      composerNote.style.display = 'none';
      const terminal = (kind === 'completed' || kind === 'failed');
      composerInput.disabled = terminal;
      composerSend.disabled = terminal;
      composerInput.placeholder = terminal ? 'This task is closed. Select New Request to start a new one.' : `Reply to ${tid}...`;
      composerSend.textContent = terminal ? 'Closed' : 'Send';
      composerInput.dataset.mode = terminal ? 'disabled' : 'reply';
      composerInput.dataset.targetTaskId = tid;
    }
  }

  function renderDetail() {
    const tid = state.selectedTaskId;
    const root = $('#detail-stack');
    if (tid === NEW_REQUEST_ID) {
      root.innerHTML = `<div class="empty-state">Select a task to view details, or send a new request.</div>`;
      return;
    }
    const t = state.tasks[tid];
    if (!t) { root.innerHTML = `<div class="empty-state">Task not loaded.</div>`; return; }
    const kind = statusKindOf(t.statusState || t.status);
    const steps = Array.isArray(t.progressSteps) ? t.progressSteps : [];
    const currentStep = t.currentMajorStep || (steps.length ? steps[steps.length-1].text : '');
    const nextAction = nextActionForTask(t, kind);
    const statusMessage = t.statusMessage || t.summary || t.userRequest || '';
    const taskTitle = displayTitle(t, kind);
    const semanticPhases = deriveMajorPhases(t, currentStep);
    const phaseExpanded = !!state.phaseExpandedByTask[tid];

    let stepsHtml = '';
    let majorStepLead = `<p style="margin-top:8px; font-size:14px; line-height:1.6;">${esc(currentStep || 'Waiting for progress...')}</p>`;
    let workflowTitle = semanticPhases ? 'Workflow Timeline' : 'Current Major Step';
    if (semanticPhases) {
      const currentPhase = semanticPhases.phases.find(phase => phase.key === semanticPhases.currentKey) || semanticPhases.phases[semanticPhases.phases.length - 1];
      const visiblePhases = phaseExpanded ? semanticPhases.phases : (currentPhase ? [currentPhase] : []);
      const railHtml = visiblePhases.map((phase, index) => {
        const current = phase.key === semanticPhases.currentKey ? ' current' : '';
        const done = phaseExpanded && index < semanticPhases.phases.findIndex(p => p.key === semanticPhases.currentKey) ? ' done' : '';
        return `<span class="phase-pill${done}${current}">${esc(phase.label)}</span>`;
      }).join('');
      majorStepLead = currentPhase ? `<div class="phase-banner"><span class="phase-banner-label">Active Phase</span><strong>${esc(currentPhase.label)}</strong><span>${esc(currentPhase.detail || 'Waiting for progress...')}</span></div>` : majorStepLead;
      const phaseRows = visiblePhases.map(phase => {
        const current = phase.key === semanticPhases.currentKey ? ' current' : '';
        const updateLabel = `${phase.updateCount} update${phase.updateCount === 1 ? '' : 's'}`;
        const phaseTime = phase.ts ? `<span class="phase-time">${esc(fmtTime(phase.ts))}</span>` : '';
        return `<div class="phase-row${current}">
          <div class="phase-row-head">
            <span class="phase-title">${esc(phase.label)}</span>
            ${phase.agent ? `<span class="step-agent">${esc(phase.agent)}</span>` : ''}
            <span class="phase-count">${esc(updateLabel)}</span>
            ${phaseTime}
          </div>
          <div class="phase-detail">${esc(phase.detail || phase.label)}</div>
        </div>`;
      }).join('');
      const workflowToggle = `<button class="workflow-toggle" type="button" id="workflow-toggle">${phaseExpanded ? 'Current step only' : 'Show all steps'}</button>`;
      workflowTitle = `Workflow Timeline<div class="workflow-head">${workflowToggle}</div>`;
      stepsHtml = `<div class="phase-rail">${railHtml}</div><div class="phase-list">${phaseRows}</div>`;
    } else if (steps.length) {
      stepsHtml = steps.map((s, i) => {
        const cur = (i === steps.length - 1) ? 'current' : '';
        return `<div class="step ${cur}">
          <div class="step-index">${i+1}</div>
          <div>
            <span class="step-agent">${esc(s.agent || 'agent')}</span>
            <div class="step-title">${esc(s.text || s.step || '')}</div>
            <div class="step-meta">${esc(s.ts || '')}</div>
          </div>
        </div>`;
      }).join('');
    }

    root.innerHTML = `
      <div class="detail-card spotlight" id="task-spotlight">
        <div class="kicker">Task Spotlight</div>
        <span class="status-pill ${esc(kind)}">${esc(statusLabel(kind))}</span>
        <h4>${esc(taskTitle)}</h4>
        <div class="detail-section">
          <span class="detail-label">${kind === 'waiting' ? 'Action Required' : kind === 'failed' ? 'Blocked' : kind === 'completed' ? 'Ready for Review' : 'Currently Running'}</span>
          <div class="detail-value">${esc(statusMessage || 'No status update yet.')}</div>
        </div>
        <div class="detail-section detail-inline-grid">
          <div>
            <span class="detail-label">Next Action</span>
            <div class="detail-value">${esc(nextAction)}</div>
          </div>
          <div>
            <span class="detail-label">Current Focus</span>
            <div class="detail-value">${esc(currentStep || 'Waiting for progress...')}</div>
          </div>
        </div>
        <div class="detail-section">
          <span class="detail-label">Request Summary</span>
          <div class="detail-value" style="color:var(--muted);">
          ${esc(t.userRequest || '')}
          </div>
          <div class="meta-grid">
            <div class="meta"><span class="meta-label">Task ID</span><div class="meta-value">${esc(t.task_id)}</div></div>
            <div class="meta"><span class="meta-label">Type</span><div class="meta-value">${esc(t.taskType || 'general')}</div></div>
            <div class="meta"><span class="meta-label">Started At</span><div class="meta-value">${esc(fmtTime(t.createdAt))}</div></div>
            <div class="meta"><span class="meta-label">Elapsed</span><div class="meta-value">${esc(elapsedMs(t.createdAt, t.updatedAt))}</div></div>
          </div>
        </div>
      </div>
      <div class="detail-card">
        <div class="kicker">${semanticPhases ? 'Workflow Timeline' : 'Current Major Step'}</div>
        ${semanticPhases ? `<div class="workflow-head"><h4 style="margin-top:6px">${phaseExpanded ? 'All Phases' : 'Current Step Only'}</h4><button class="workflow-toggle" type="button" id="workflow-toggle">${phaseExpanded ? 'Current step only' : 'Show all steps'}</button></div>` : ''}
        ${majorStepLead}
        <div class="step-digest">Longer execution detail continues in merged logs.</div>
        <div class="steps">${stepsHtml}</div>
      </div>
      <div class="detail-card" id="logs-card">
        <div class="kicker">Merged Logs</div>
        <div class="log-toolbar" id="log-toolbar">
          <span class="log-toolbar-text"><strong>0</strong> Visible Logs:</span>
          <span class="log-toolbar-text"><strong>0</strong> Agents:</span>
          <span class="log-toolbar-text"><strong>All</strong> Current Filter:</span>
          <div class="log-filters">
          <label>Agent <select id="filter-agent"><option value="all">All</option></select></label>
          <label>Level <select id="filter-level">
            <option value="all">All</option>
            <option value="DEBUG">DEBUG+</option>
            <option value="INFO" selected>INFO+</option>
            <option value="WARN">WARN+</option>
            <option value="ERROR">ERROR</option>
          </select></label>
          <span class="status-pill active" id="log-live-indicator">Live via SSE</span>
          </div>
        </div>
        <div class="log-box" id="log-box"></div>
      </div>`;

    $('#filter-agent').value = state.filters.agent;
    $('#filter-level').value = state.filters.level;
    $('#filter-agent').addEventListener('change', e => { state.filters.agent = e.target.value; renderLogs(); });
    $('#filter-level').addEventListener('change', e => { state.filters.level = e.target.value; renderLogs(); });
    const workflowToggle = $('#workflow-toggle');
    if (workflowToggle) {
      workflowToggle.addEventListener('click', () => {
        state.phaseExpandedByTask[tid] = !state.phaseExpandedByTask[tid];
        renderDetail();
      });
    }
    renderLogs();
  }

  const LEVEL_ORDER = { DEBUG: 10, INFO: 20, WARN: 30, ERROR: 40 };
  function renderLogs() {
    const tid = state.selectedTaskId;
    const box = $('#log-box'); if (!box) return;
    const logs = state.logsByTask[tid] || [];
    const toolbar = $('#log-toolbar');
    const sel = $('#filter-agent');
    if (sel) {
      const existing = new Set(Array.from(sel.options).map(o => o.value));
      for (const l of logs) {
        if (l.agent && !existing.has(l.agent)) {
          const o = document.createElement('option');
          o.value = l.agent; o.textContent = l.agent;
          sel.appendChild(o); existing.add(l.agent);
        }
      }
    }
    const minLevel = LEVEL_ORDER[state.filters.level] || 0;
    const visible = logs.filter(l => {
      if (state.filters.agent !== 'all' && l.agent !== state.filters.agent) return false;
      const lv = LEVEL_ORDER[(l.level || '').toUpperCase()] || 0;
      if (state.filters.level !== 'all' && lv < minLevel) return false;
      return true;
    });
    if (toolbar) {
      const agents = new Set(visible.map(l => l.agent).filter(Boolean));
      const filterText = `${state.filters.agent === 'all' ? 'All agents' : state.filters.agent} / ${state.filters.level === 'all' ? 'all levels' : state.filters.level}`;
      toolbar.innerHTML = `
        <span class="log-toolbar-text"><strong>${visible.length}</strong> Visible Logs:</span>
        <span class="log-toolbar-text"><strong>${agents.size}</strong> Agents:</span>
        <span class="log-toolbar-text"><strong>${esc(filterText)}</strong> Current Filter:</span>
        <div class="log-filters">
          <label>Agent <select id="filter-agent"><option value="all">All</option></select></label>
          <label>Level <select id="filter-level">
            <option value="all">All</option>
            <option value="DEBUG">DEBUG+</option>
            <option value="INFO">INFO+</option>
            <option value="WARN">WARN+</option>
            <option value="ERROR">ERROR</option>
          </select></label>
          <span class="status-pill active" id="log-live-indicator">Live via SSE</span>
        </div>`;
      $('#filter-agent').value = state.filters.agent;
      $('#filter-level').value = state.filters.level;
      $('#filter-agent').addEventListener('change', e => { state.filters.agent = e.target.value; renderLogs(); });
      $('#filter-level').addEventListener('change', e => { state.filters.level = e.target.value; renderLogs(); });
    }
    if (!visible.length) {
      box.innerHTML = `<div class="empty-state">No logs yet for this filter.</div>`;
      return;
    }
    box.innerHTML = visible.map(l => {
      const ts = fmtLogTimestamp(l.timestamp || '');
      return `<div class="log-line">
      <span class="log-ts"><span class="log-date">${esc(ts.date)}</span><span class="log-time">${esc(ts.time)}</span></span>
      <span class="log-level ${esc((l.level||'').toUpperCase())}">${esc((l.level||'').toUpperCase())}</span>
      <span class="log-agent">${esc(l.agent || '')}</span>
      <span class="log-msg">${esc(l.message || '')}</span>
    </div>`;
    }).join('');
    box.scrollTop = box.scrollHeight;
  }

  function subscribeLogs(tid) {
    if (state.logEventSources[tid]) return;
    fetch(`/logs/${encodeURIComponent(tid)}`).then(r => r.json()).then(data => {
      state.logsByTask[tid] = Array.isArray(data.logs) ? data.logs : [];
      if (tid === state.selectedTaskId) renderLogs();
    }).catch(() => {});
    try {
      const es = new EventSource(`/logs/stream/${encodeURIComponent(tid)}`);
      es.addEventListener('log.appended', ev => {
        try {
          const entry = JSON.parse(ev.data);
          (state.logsByTask[tid] = state.logsByTask[tid] || []).push(entry);
          if (tid === state.selectedTaskId) renderLogs();
        } catch {}
      });
      es.onerror = () => { es.close(); delete state.logEventSources[tid]; };
      state.logEventSources[tid] = es;
    } catch (e) { /* SSE unsupported */ }
  }

  function subscribeTaskEvents() {
    try {
      const es = new EventSource('/ui/events');
      es.addEventListener('task.snapshot', ev => {
        try {
          const data = JSON.parse(ev.data);
          state.tasks = {}; state.order = [];
          for (const t of (data.tasks || [])) upsertTask(t);
          renderTaskList(); renderChat(); renderDetail();
        } catch {}
      });
      const refresh = () => loadTasks(false);
      es.addEventListener('task.created', refresh);
      es.addEventListener('task.updated', refresh);
      es.addEventListener('task.input_required', refresh);
      es.addEventListener('task.completed', refresh);
      es.addEventListener('task.failed', refresh);
      es.addEventListener('task.resumed', refresh);
      es.onerror = () => { es.close(); setTimeout(subscribeTaskEvents, 5000); };
    } catch (e) {
      setInterval(() => loadTasks(false), 5000);
    }
  }

  async function sendComposer() {
    const input = $('#composer-input');
    const text = (input.value || '').trim();
    if (!text) return;
    const mode = input.dataset.mode || 'create';
    const targetTaskId = input.dataset.targetTaskId || '';
    input.value = '';
    if (mode === 'create') {
      const r = await fetch('/message:send', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          message: { role: 'ROLE_USER', parts: [{ text }] },
          configuration: { returnImmediately: true },
        }),
      });
      const data = await r.json();
      const newId = (data.task && data.task.id) || (data.ui_update && data.ui_update.task_id);
      await loadTasks(false);
      if (newId) selectTask(newId);
    } else if (mode === 'resume' && targetTaskId) {
      await fetch(`/tasks/${encodeURIComponent(targetTaskId)}/resume`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ input: text }),
      });
      await loadTasks(false);
      selectTask(targetTaskId);
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    $('#composer-send').addEventListener('click', sendComposer);
    $('#composer-input').addEventListener('keydown', ev => {
      if (ev.key === 'Enter' && (ev.metaKey || ev.ctrlKey)) { ev.preventDefault(); sendComposer(); }
    });
    loadTasks(true);
    subscribeTaskEvents();
  });
})();
"""


def render_compass_ui(
    messages: list[dict] | None = None,
    tasks: list[dict] | None = None,
    selected_task_id: str | None = None,
) -> str:
    """Render the finalized three-column workspace HTML.

    ``messages`` and ``tasks`` are accepted for backward compatibility with
    server-side tests; the live UI rehydrates everything via ``/api/tasks``
    and the SSE event streams.
    """
    tasks = tasks or []
    server_task_list = "\n".join(
        render_task_tab(t.get("task_id", t.get("id", "")), t.get("status", "active"), t.get("summary", ""))
        for t in tasks
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Compass Agent - Task Workspace</title>
  <style>{_INLINE_CSS}</style>
</head>
<body>
  <div class="workspace">
    <aside class="panel" id="task-list-panel">
      <div class="panel-head">
        <div class="kicker">Compass Agent</div>
        <h3>Task List</h3>
        <p>Prioritize tasks that need input first, then monitor active execution.</p>
      </div>
      <div class="panel-body">
        <div class="tasks-overview-strip" id="tasks-overview-strip">
          <div class="overview-chip attention"><strong>0</strong><span>Needs Attention</span></div>
          <div class="overview-chip active"><strong>0</strong><span>In Progress</span></div>
          <div class="overview-chip done"><strong>0</strong><span>Completed</span></div>
        </div>
        <div class="focus-note" id="task-focus-note" style="display:none"></div>
        <div class="task-list" id="task-list">{server_task_list}</div>
      </div>
    </aside>

    <section class="panel" id="task-chat-panel">
      <div class="panel-head">
        <div class="kicker">Task Chat</div>
        <h3 id="chat-head-sub">New Request</h3>
      </div>
      <div class="panel-body">
        <div class="chat-context-bar" id="chat-context-bar">
          <span class="chat-context-pill">Reply Route</span>
          <span class="chat-context-copy">This composer will create a brand-new Compass task.</span>
        </div>
        <div class="chat-scroll" id="chat-scroll"></div>
        <div class="composer">
          <div class="composer-note" id="composer-note" style="display:none"></div>
          <div class="composer-box">
            <textarea id="composer-input" rows="2" placeholder="Describe a new task..."></textarea>
            <button id="composer-send">Create</button>
          </div>
        </div>
      </div>
    </section>

    <section class="panel" id="task-info-panel">
      <div class="panel-head">
        <div class="kicker">Task Info</div>
        <h3>Overview, Steps, and Logs</h3>
        <p>Read the highlighted action first, then use steps and logs for execution context.</p>
      </div>
      <div class="panel-body">
        <div class="detail-stack" id="detail-stack">
          <div class="detail-card spotlight" id="task-spotlight">
            <div class="kicker">Task Spotlight</div>
            <h4>Next Action</h4>
            <p style="margin-top:8px; font-size:13px; line-height:1.7; color:var(--muted);">
              Select a task to view the most important status, next action, and workflow detail.
            </p>
          </div>
        </div>
      </div>
    </section>
  </div>
  <script>{_INLINE_JS}</script>
</body>
</html>
"""
