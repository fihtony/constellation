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
        f'<div class="task-item-head">'
        f'<div class="task-title">{primary_label}</div>'
        f'<div class="task-type">Task</div>'
        f'</div>'
        f'<div class="task-note">{secondary_label}</div>'
        f'<div class="task-foot">'
        f'<span class="status-pill {safe_status}">{safe_status_text}</span>'
        f'<span class="task-time">--</span>'
        f'</div>'
        f'</div>'
    )


_INLINE_CSS = """
:root {
  --bg: #071018;
  --bg-accent: #0b1620;
  --panel: rgba(12, 21, 31, 0.94);
  --panel-strong: #101b29;
  --panel-soft: rgba(14, 24, 34, 0.88);
  --ink: #eaf1f6;
  --muted: #8ea3b4;
  --line: rgba(145, 171, 189, 0.12);
  --accent: #93c6d0;
  --accent-soft: rgba(147, 198, 208, 0.12);
  --accent-strong: #b4d1db;
  --ok-bg: rgba(58, 162, 110, 0.16);
  --ok-ink: #79e0a6;
  --wait-bg: rgba(245, 158, 11, 0.14);
  --wait-ink: #ffd36f;
  --progress-bg: rgba(56, 162, 186, 0.16);
  --progress-ink: #93c6d0;
  --error-bg: rgba(239, 68, 68, 0.16);
  --error-ink: #ff8f8f;
  --user-bubble-bg-top: #355967;
  --user-bubble-bg-bottom: #284652;
  --user-bubble-border: rgba(150, 215, 230, 0.16);
  --user-bubble-text: #edf8fb;
  --user-bubble-glow: rgba(237, 248, 251, 0.04);
  --office-card-bg-top: rgba(102, 125, 98, 0.9);
  --office-card-bg-bottom: rgba(61, 82, 58, 0.94);
  --office-card-border: rgba(160, 190, 157, 0.3);
  --office-card-glow: rgba(210, 225, 208, 0.08);
  --office-card-rail: rgba(160, 190, 157, 0.26);
  --office-type-tag-bg: rgba(161, 112, 76, 0.82);
  --office-type-tag-border: rgba(226, 178, 138, 0.5);
  --office-type-tag-text: #fff0e2;
  --dev-card-bg-top: rgba(32, 78, 95, 0.90);
  --dev-card-bg-bottom: rgba(13, 40, 53, 0.98);
  --dev-card-border: rgba(143, 197, 212, 0.28);
  --dev-card-glow: rgba(183, 224, 233, 0.08);
  --dev-type-tag-bg: rgba(118, 204, 222, 0.28);
  --dev-type-tag-border: rgba(171, 230, 242, 0.36);
  --dev-type-tag-text: #e0f8fc;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; overflow: hidden; }
body {
  font-family: "IBM Plex Sans", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  color: var(--ink);
  background:
    radial-gradient(circle at top left, rgba(127,195,209,0.10), transparent 28%),
    radial-gradient(circle at right 20%, rgba(55,88,116,0.18), transparent 24%),
    linear-gradient(180deg, var(--bg), var(--bg-accent));
  overflow: hidden;
}
.page {
  width: min(1700px, calc(100vw - 24px));
  height: calc(100vh - 28px);
  margin: 14px auto;
}
.app-shell {
  height: 100%;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  border: 1px solid var(--line);
  border-radius: 26px;
  overflow: hidden;
  background: var(--panel);
  box-shadow: 0 26px 60px rgba(0, 0, 0, 0.34);
}
.dashboard {
  display: grid;
  grid-template-columns: repeat(5, minmax(0, 1fr));
  gap: 12px;
  padding: 16px;
  border-bottom: 1px solid rgba(145, 171, 189, 0.08);
  background:
    linear-gradient(180deg, rgba(15, 25, 36, 0.96), rgba(11, 18, 27, 0.94));
}
.dashboard-card {
  padding: 14px 16px;
  border-radius: 18px;
  border: 1px solid rgba(145, 171, 189, 0.08);
  background: rgba(18, 29, 40, 0.82);
}
.dashboard-card strong {
  display: block;
  font-size: 26px;
  line-height: 1;
  letter-spacing: -0.04em;
}
.dashboard-card span {
  display: block;
  margin-top: 8px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.dashboard-card.total strong,
.dashboard-card.total span { color: var(--accent-strong); }
.dashboard-card.waiting strong,
.dashboard-card.waiting span { color: rgba(212, 152, 36, 0.98); }
.dashboard-card.active strong,
.dashboard-card.active span { color: rgba(34, 147, 173, 0.98); }
.dashboard-card.done strong,
.dashboard-card.done span { color: rgba(43, 141, 93, 0.98); }
.dashboard-card.failed strong,
.dashboard-card.failed span { color: rgba(170, 49, 49, 0.98); }
.workspace {
  display: grid;
  grid-template-columns: clamp(255px, 22vw, 320px) minmax(340px, 1.02fr) minmax(370px, 1.12fr);
  height: 100%;
  min-width: 0;
  min-height: 0;
  overflow: hidden;
}
.panel {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-right: 1px solid rgba(145, 171, 189, 0.08);
}
.panel:last-child {
  border-right: none;
}
.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 15px 18px;
  border-bottom: 1px solid rgba(145, 171, 189, 0.08);
  background: rgba(14, 24, 34, 0.94);
}
.panel-head strong {
  font-size: 14px;
  color: #b4d1db;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.panel-head-title {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  flex-wrap: wrap;
}
.panel-note {
  color: var(--muted);
  font-size: 12px;
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
/* Clean scrollbars */
.panel-body::-webkit-scrollbar { width: 5px; }
.panel-body::-webkit-scrollbar-track { background: transparent; }
.panel-body::-webkit-scrollbar-thumb { background: rgba(127,195,209,0.15); border-radius: 999px; }
.panel-body::-webkit-scrollbar-thumb:hover { background: rgba(127,195,209,0.25); }
/* Task List scrollable region */
#task-list-scroll::-webkit-scrollbar { width: 5px; }
#task-list-scroll::-webkit-scrollbar-track { background: transparent; }
#task-list-scroll::-webkit-scrollbar-thumb { background: rgba(127,195,209,0.15); border-radius: 999px; }
#task-list-scroll::-webkit-scrollbar-thumb:hover { background: rgba(127,195,209,0.25); }
/* Chat scrollable region */
#chat-scroll::-webkit-scrollbar { width: 5px; }
#chat-scroll::-webkit-scrollbar-track { background: transparent; }
#chat-scroll::-webkit-scrollbar-thumb { background: rgba(127,195,209,0.15); border-radius: 999px; }
#chat-scroll::-webkit-scrollbar-thumb:hover { background: rgba(127,195,209,0.25); }
/* Log box scrollable region */
.log-box::-webkit-scrollbar { width: 5px; }
.log-box::-webkit-scrollbar-track { background: transparent; }
.log-box::-webkit-scrollbar-thumb { background: rgba(127,195,209,0.15); border-radius: 999px; }
.log-box::-webkit-scrollbar-thumb:hover { background: rgba(127,195,209,0.25); }
#task-list-panel .panel-body { padding: 0; overflow: hidden; }
#task-list-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 14px; }
.task-list { display: flex; flex-direction: column; gap: 10px; }
.task-item {
  position: relative;
  padding: 12px 13px;
  border-radius: 18px;
  border: 1px solid rgba(145, 171, 189, 0.1);
  background: rgba(17, 28, 39, 0.84);
  cursor: pointer;
  transition: transform 160ms ease, border-color 160ms ease, box-shadow 160ms ease, opacity 160ms ease;
}
.task-item:hover { transform: translateY(-1px); }
.task-item.active {
  border-color: rgba(234, 241, 246, 0.42);
  box-shadow: inset 0 0 0 1px rgba(234, 241, 246, 0.12), 0 0 0 1px rgba(234, 241, 246, 0.08), 0 20px 40px rgba(0, 0, 0, 0.24), 0 6px 16px rgba(8, 16, 24, 0.18);
  transform: translateY(-1px);
}
.task-list.has-selection .task-item:not(.active) {
  opacity: 0.54;
}
.task-list.is-overview .task-item {
  opacity: 0.7;
  filter: saturate(0.82) brightness(0.86);
}
.task-list.is-overview .task-item:hover {
  opacity: 0.8;
  filter: saturate(0.9) brightness(0.92);
}
.task-item.new-request {
  background: linear-gradient(180deg, rgba(74, 83, 93, 0.82), rgba(31, 36, 42, 0.96));
  border-color: rgba(231, 239, 245, 0.2);
  box-shadow: inset 0 0 0 1px rgba(231, 239, 245, 0.08);
}
.task-item.office {
  background: linear-gradient(180deg, var(--office-card-bg-top), var(--office-card-bg-bottom));
  border-color: var(--office-card-border);
  box-shadow: inset 0 0 0 1px var(--office-card-glow), inset 3px 0 0 var(--office-card-rail);
}
.task-item.development {
  background: linear-gradient(180deg, var(--dev-card-bg-top), var(--dev-card-bg-bottom));
  border-color: var(--dev-card-border);
  box-shadow: inset 0 0 0 1px var(--dev-card-glow);
}
.task-item-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
}
.task-title {
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.02em;
  line-height: 1.45;
  color: var(--ink);
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 3;
  overflow: hidden;
  overflow-wrap: anywhere;
  text-wrap: pretty;
  max-height: calc(1.45em * 3);
}
.task-type {
  padding: 3px 7px;
  border-radius: 999px;
  font-size: 7px;
  font-weight: 700;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  flex-shrink: 0;
  border: 1px solid transparent;
}
.task-type.office {
  background: var(--office-type-tag-bg);
  border-color: var(--office-type-tag-border);
  color: var(--office-type-tag-text);
}
.task-type.development {
  background: var(--dev-type-tag-bg);
  border-color: var(--dev-type-tag-border);
  color: var(--dev-type-tag-text);
}
.task-type.new {
  background: rgba(219, 229, 236, 0.22);
  border-color: rgba(231, 239, 245, 0.3);
  color: #f7fafc;
}
.task-note { margin-top: 7px; font-size: 12px; line-height: 1.55; color: #d8e2ea; }
.task-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 8px;
}
.task-time {
  display: inline-flex;
  align-items: center;
  justify-content: flex-end;
  min-height: 24px;
  color: var(--muted);
  font-size: 10px;
  line-height: 1.2;
  letter-spacing: 0.03em;
  white-space: nowrap;
  text-align: right;
}
.status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  border-radius: 999px;
  border: 1px solid transparent;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.08em;
  line-height: 1.2;
  text-transform: uppercase;
}
.status-pill::before {
  content: "";
  width: 6px; height: 6px; border-radius: 50%; background: currentColor;
}
.status-pill.active   { color: #effbfd; border-color: rgba(123, 220, 239, 0.58); background: rgba(34, 147, 173, 0.84); box-shadow: inset 0 0 0 1px rgba(224, 248, 252, 0.1); }
.status-pill.waiting  { color: #fff7e2; border-color: rgba(255, 221, 118, 0.56); background: rgba(212, 152, 36, 0.8); box-shadow: inset 0 0 0 1px rgba(255, 244, 210, 0.12); }
.status-pill.completed{ color: #e4ffef; border-color: rgba(125, 229, 171, 0.54); background: rgba(43, 141, 93, 0.82); }
.status-pill.failed   { color: #ffe6e6; border-color: rgba(255, 153, 153, 0.52); background: rgba(170, 49, 49, 0.82); }
.wait-badge {
  position: absolute; top: 10px; right: 10px;
  width: 10px; height: 10px; border-radius: 50%;
  background: var(--wait-ink);
  box-shadow: 0 0 0 3px rgba(147,100,0,0.16);
}

/* Chat */
#task-chat-panel .panel-body {
  padding: 0;
  display: grid;
  grid-template-rows: minmax(0, 1fr) auto;
  min-height: 0;
}
#chat-scroll {
  min-height: 0; overflow-y: auto;
  display: flex; flex-direction: column; gap: 10px;
  padding: 16px;
}
.chat-entry {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 5px;
}
.chat-entry.user {
  align-items: flex-end;
}
.bubble {
  max-width: 84%;
  padding: 10px 12px;
  border-radius: 15px;
  border: 1px solid rgba(145, 171, 189, 0.08);
  background: rgba(18, 29, 40, 0.92);
  line-height: 1.6;
  font-size: 13px;
  word-wrap: break-word;
}
.bubble.user {
  margin-left: auto;
  background: linear-gradient(180deg, var(--user-bubble-bg-top), var(--user-bubble-bg-bottom));
  border-color: var(--user-bubble-border);
  color: var(--user-bubble-text);
  box-shadow: inset 0 0 0 1px var(--user-bubble-glow);
}
.bubble.agent { border-left: 3px solid var(--accent); }
.bubble.waiting { border-left: 4px solid var(--wait-ink); background: var(--wait-bg); }
.bubble.failed  { border-left: 4px solid var(--error-ink); background: var(--error-bg); }
.bubble.completed { border-left: 4px solid var(--ok-ink); background: var(--ok-bg); }
.bubble-meta {
  font-size: 9px;
  line-height: 1;
  color: rgba(142, 163, 180, 0.7);
  letter-spacing: 0.02em;
  padding: 0 2px;
}
.composer {
  padding: 14px 16px 16px;
  border-top: 1px solid rgba(145, 171, 189, 0.08);
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
  display: grid;
  grid-template-columns: 1fr 92px;
  gap: 8px;
}
#composer-input {
  flex: 1;
  padding: 12px;
  border-radius: 14px;
  border: 1px solid rgba(145, 171, 189, 0.08);
  background: rgba(14, 24, 34, 0.92);
  color: var(--ink);
  font-size: 13px;
  font-family: inherit;
  resize: none;
}
#composer-input:disabled { opacity: 0.5; cursor: not-allowed; }
#composer-send {
  padding: 0 18px;
  border-radius: 14px;
  border: 1px solid rgba(145, 171, 189, 0.08);
  background: rgba(147, 198, 208, 0.16);
  color: var(--ink);
  font-weight: 700;
  font-size: 13px;
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
  border-radius: 18px;
  border: 1px solid rgba(145, 171, 189, 0.08);
  background: rgba(14, 23, 33, 0.8);
  box-shadow: none;
}
.detail-card.spotlight.completed {
  background:
    linear-gradient(180deg, rgba(22, 56, 39, 0.9), rgba(14, 29, 23, 0.94));
  border-color: rgba(121, 224, 166, 0.16);
}
.detail-card.spotlight.failed {
  background:
    linear-gradient(180deg, rgba(58, 20, 20, 0.92), rgba(30, 13, 13, 0.96));
  border-color: rgba(255, 143, 143, 0.18);
}
.detail-card.spotlight .kicker {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--accent);
  margin-bottom: 8px;
}
.detail-request-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
}
.detail-request-copy {
  display: grid;
  gap: 4px;
  min-width: 0;
}
.detail-request-kicker {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--muted);
}
.detail-request-title {
  font-size: 16px;
  font-weight: 700;
  line-height: 1.35;
  letter-spacing: -0.03em;
  color: var(--ink);
}
.detail-card.spotlight .status-pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  border-radius: 999px;
  font-size: 10px;
  font-weight: 700;
  margin-top: 0;
  margin-bottom: 0;
  letter-spacing: 0.06em;
  white-space: nowrap;
}
.detail-card.spotlight .status-pill::before {
  content: "";
  width: 6px; height: 6px; border-radius: 50%; background: currentColor;
}
.detail-card.spotlight .status-pill.active   { background: var(--progress-bg); color: var(--progress-ink); }
.detail-card.spotlight .status-pill.waiting  { background: var(--wait-bg);     color: var(--wait-ink); }
.detail-card.spotlight .status-pill.completed{ background: var(--ok-bg);       color: var(--ok-ink); }
.detail-card.spotlight .status-pill.failed   { background: var(--error-bg);    color: var(--error-ink); }
.detail-badge-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}
.detail-type-pill {
  display: inline-flex;
  align-items: center;
  padding: 6px 12px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.06em;
  text-transform: uppercase;
}
.detail-type-pill.office {
  background: var(--office-type-tag-bg);
  border: 1px solid var(--office-type-tag-border);
  color: var(--office-type-tag-text);
}
.detail-type-pill.development {
  background: var(--dev-type-tag-bg);
  border: 1px solid var(--dev-type-tag-border);
  color: var(--dev-type-tag-text);
}
.detail-card.spotlight h4 { margin-top: 4px; font-size: 18px; letter-spacing: -0.01em; font-weight: 700; color: var(--ink); }
.detail-card.spotlight .detail-section {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px solid rgba(154, 176, 196, 0.1);
}
.detail-card.spotlight .detail-section:first-of-type { margin-top: 10px; }
.detail-card.spotlight .detail-label {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.1em;
  text-transform: uppercase;
  color: var(--muted);
  margin-bottom: 4px;
}
.detail-card.spotlight .detail-value {
  font-size: 13px;
  line-height: 1.5;
}
.detail-card.spotlight .detail-value.multiline {
  white-space: pre-wrap;
  word-break: break-word;
}
.detail-card.spotlight .detail-value.action-hint {
  color: var(--accent-strong);
  font-weight: 600;
}
.detail-head-tag {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  justify-content: flex-end;
  flex-wrap: wrap;
}
.detail-head-task-id {
  display: inline-flex;
  align-items: center;
  min-width: 0;
  color: #dce7ef;
  font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
  font-size: 12px;
  line-height: 1.2;
  letter-spacing: 0.01em;
}
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
  border: 1px solid rgba(145, 171, 189, 0.12);
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.03);
  color: var(--muted);
  font: inherit;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0;
  padding: 7px 10px;
  cursor: pointer;
}
.workflow-toggle:hover {
  color: var(--ink);
  border-color: rgba(127,195,209,0.22);
}
.timeline-list {
  margin-top: 14px;
  display: grid;
  gap: 0;
}
.timeline-row {
  position: relative;
  padding: 12px 0 12px 28px;
  border-top: 1px solid rgba(145, 171, 189, 0.08);
}
.timeline-row:first-child {
  border-top: none;
}
.timeline-row::before {
  content: "";
  position: absolute;
  left: 10px;
  top: 0;
  bottom: 0;
  width: 2px;
  background: rgba(145, 171, 189, 0.12);
}
.timeline-mark {
  position: absolute;
  left: 2px;
  top: 14px;
  width: 16px;
  height: 16px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 9px;
  font-weight: 700;
  border: 2px solid rgba(145, 171, 189, 0.28);
  background: rgba(14, 23, 33, 0.98);
  color: var(--muted);
}
.timeline-row.done .timeline-mark {
  color: #04110a;
  background: var(--ok-ink);
  border-color: rgba(121, 224, 166, 0.9);
}
.timeline-row.failed .timeline-mark {
  color: #210707;
  background: var(--error-ink);
  border-color: rgba(255, 143, 143, 0.9);
}
.timeline-row.warn .timeline-mark {
  color: #281b01;
  background: var(--wait-ink);
  border-color: rgba(255, 211, 111, 0.9);
}
.timeline-row.pending .timeline-mark {
  background: transparent;
  color: rgba(145, 171, 189, 0.7);
}
.timeline-title {
  font-size: 13px;
  font-weight: 700;
  line-height: 18px;
  color: var(--ink);
}
.timeline-headline {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  flex-wrap: wrap;
  min-height: 18px;
}
.timeline-meta {
  margin-top: 4px;
  color: var(--muted);
  font-size: 12px;
  line-height: 1.55;
}
.timeline-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 10px;
}
.timeline-fact {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 8px;
  border-radius: 999px;
  border: 1px solid rgba(145, 171, 189, 0.1);
  background: rgba(255, 255, 255, 0.03);
  color: #c7d5e0;
  font-size: 10px;
  line-height: 1;
  letter-spacing: 0.02em;
  white-space: nowrap;
}
.timeline-fact-label {
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: 0.06em;
  font-size: 9px;
  font-weight: 700;
}
.timeline-list.collapsed .timeline-row:not(.current) {
  display: none;
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
.log-box {
  margin-top: 10px;
  border-radius: 12px;
  border: 1px solid rgba(145, 171, 189, 0.08);
  background: rgba(9, 16, 23, 0.32);
  max-height: 250px;
  overflow-y: auto;
}
.log-toolbar {
  display: inline-flex;
  justify-content: flex-end;
  gap: 10px;
  align-items: flex-start;
  padding: 0 0 10px;
  border-bottom: 1px solid rgba(145, 171, 189, 0.08);
}
.log-filter {
  position: relative;
}
.log-filter-trigger {
  appearance: none;
  min-width: 92px;
  padding: 6px 10px;
  border-radius: 12px;
  border: 1px solid rgba(145, 171, 189, 0.12);
  background: rgba(255, 255, 255, 0.03);
  color: #d8e2ea;
  font-size: 11px;
  font-family: inherit;
  display: inline-flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  cursor: pointer;
}
.log-filter.is-open .log-filter-trigger {
  border-color: rgba(147, 198, 208, 0.28);
  background: rgba(147, 198, 208, 0.08);
}
.log-filter-caret {
  color: rgba(216, 226, 234, 0.7);
  font-size: 10px;
  transition: transform 0.15s ease;
}
.log-filter.is-open .log-filter-caret { transform: rotate(180deg); }
.log-filter-menu {
  position: absolute;
  top: calc(100% + 8px);
  right: 0;
  min-width: 132px;
  padding: 8px;
  border-radius: 14px;
  border: 1px solid rgba(145, 171, 189, 0.14);
  background: rgba(12, 21, 31, 0.96);
  box-shadow: 0 18px 34px rgba(0, 0, 0, 0.28);
  display: none;
  gap: 4px;
  z-index: 4;
}
.log-filter.is-open .log-filter-menu { display: grid; }
.log-filter-option {
  appearance: none;
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 7px 9px;
  border-radius: 10px;
  border: none;
  background: rgba(255,255,255,0.02);
  color: #d8e2ea;
  font-size: 11px;
  line-height: 1.2;
  text-align: left;
  cursor: pointer;
}
.log-filter-option.is-active {
  background: rgba(147, 198, 208, 0.14);
  color: #eef7fb;
}
.log-filter-check {
  color: #93c6d0;
  font-size: 10px;
  line-height: 1;
}
.log-line {
  display: grid;
  grid-template-columns: 96px 44px minmax(58px, 72px) 1fr;
  gap: 10px;
  padding: 8px 0;
  border-bottom: 1px solid rgba(145, 171, 189, 0.08);
  font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
  font-size: 11px;
  line-height: 1.45;
  color: #d6e2ea;
  transition: background 0.1s ease;
}
.log-line:hover { background: rgba(147, 198, 208, 0.04); }
.log-ts {
  color: var(--muted);
  font-size: 10px;
  line-height: 1.2;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.log-level {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.06em;
  padding: 0;
  border-radius: 0;
  text-align: left;
  border: none;
  background: transparent;
  text-transform: uppercase;
}
.log-agent {
  font-size: 11px;
  font-weight: 600;
  color: var(--accent);
  min-width: 0;
  white-space: normal;
  overflow-wrap: anywhere;
  word-break: break-word;
}
.log-msg { word-break: break-word; color: #d6e2ea; }
.log-line:last-child { border-bottom: none; }
.log-level.ERROR { color: var(--error-ink); }
.log-level.WARN  { color: var(--wait-ink); }
.log-level.INFO  { color: #d6e2ea; }
.log-level.DEBUG { color: #93a6b6; }
.empty-state {
  padding: 22px;
  color: var(--muted);
  font-size: 13px;
  text-align: center;
}
.task-info-empty {
  display: grid;
  gap: 10px;
  padding: 22px 20px;
  border-radius: 18px;
  border: 1px dashed rgba(145, 171, 189, 0.18);
  background: rgba(255, 255, 255, 0.02);
}
.task-info-empty strong {
  font-size: 18px;
  letter-spacing: -0.03em;
  color: var(--ink);
}
.task-info-empty p {
  margin: 0;
  color: var(--muted);
  font-size: 13px;
  line-height: 1.7;
}
@media (max-width: 1500px) {
  .workspace {
    height: 100%;
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
  .page {
    width: min(100vw, calc(100vw - 12px));
    height: calc(100vh - 12px);
    margin: 6px auto;
  }
  .dashboard {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
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
  const OVERVIEW_ID = '__overview__';
  const isOverviewSelection = (tid) => !tid || tid === OVERVIEW_ID;

  const state = {
    selectedTaskId: NEW_REQUEST_ID,
    tasks: {},   // task_id -> task object
    order: [],   // task_ids newest-first
    logsByTask: {},
    logEventSources: {},
    phaseExpandedByTask: {},
    filters: { agent: 'all', level: 'all' },
    openLogFilter: null,
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
    const steps = mergedProgressSignals(task);
    if ((task.taskType || task.task_type) !== 'development') return null;
    const canonicalPhases = [
      { key: 'plan', label: 'Plan' },
      { key: 'implement', label: 'Implement' },
      { key: 'build', label: 'Build' },
      { key: 'test', label: 'Test' },
      { key: 'self-check', label: 'Self-check' },
      { key: 'fix', label: 'Fix' },
      { key: 'deliver', label: 'Review & Deliver' },
    ];
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
          startTs: '',
          endTs: '',
          items: [],
        });
      }
      return phaseMap.get(phase.key);
    }
    canonicalPhases.forEach(phase => ensurePhaseBucket(phase, ''));
    for (const step of steps) {
      const text = step.text || step.step || '';
      const phase = developmentPhaseForText(text);
      if (phase.key === 'other') continue;
      const bucket = ensurePhaseBucket(phase, step.agent || '');
      bucket.agent = bucket.agent || step.agent || '';
      bucket.detail = text;
      bucket.preview = text;
      bucket.ts = step.ts || bucket.ts;
      if (step.ts && (!bucket.startTs || step.ts < bucket.startTs)) bucket.startTs = step.ts;
      if (step.ts && (!bucket.endTs || step.ts > bucket.endTs)) bucket.endTs = step.ts;
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
    const fallbackCurrent = currentPhase.key !== 'other'
      ? currentPhase.key
      : [...canonicalPhases].reverse().find(phase => phaseMap.get(phase.key)?.items.length)?.key || 'plan';
    const ordered = canonicalPhases.map(phaseRef => {
      const phase = phaseMap.get(phaseRef.key);
      return {
        ...phase,
        updateCount: phase.items.length,
        detail: phase.preview || phase.detail || (phaseRef.key === fallbackCurrent ? currentStep : '') || phase.label,
      };
    });
    return ordered.length ? { currentKey: fallbackCurrent, phases: ordered } : null;
  }
  function looksGenericSummary(text, kind) {
    const value = String(text || '').trim().toLowerCase();
    if (!value) return true;
    if (value === 'resumed') return true;
    if (value === statusLabel(kind).toLowerCase()) return true;
    return value.startsWith('office task dispatched. status:') || value.startsWith('development task dispatched.');
  }
  function displayTitle(task, kind) {
    const request = String(task.userRequest || '').trim();
    const summary = String(task.summary || '').trim();
    const currentStep = String(task.currentMajorStep || task.current_major_step || '').trim();
    if (request) return request;
    if (summary && !looksGenericSummary(summary, kind)) return summary;
    if (currentStep && !looksGenericSummary(currentStep, kind)) return currentStep;
    return task.task_id || task.id || 'Task';
  }
  function taskSortStamp(task, fallback = '') {
    return String(
      (task && (
        task.createdAt
        || task.created_at
        || task.started_at
        || task.updatedAt
        || task.updated_at
      ))
      || fallback
      || ''
    );
  }
  function orderedTaskIds() {
    return [...state.order].sort((a, b) => {
      const ta = state.tasks[a], tb = state.tasks[b];
      const ka = taskSortStamp(ta, a);
      const kb = taskSortStamp(tb, b);
      if (ka !== kb) return ka < kb ? 1 : -1;
      return a < b ? 1 : (a > b ? -1 : 0);
    });
  }
  function renderDashboard() {
    const tasks = Object.values(state.tasks);
    const totals = {
      total: tasks.length,
      waiting: tasks.filter(t => statusKindOf(t.statusState || t.status) === 'waiting').length,
      active: tasks.filter(t => statusKindOf(t.statusState || t.status) === 'active').length,
      completed: tasks.filter(t => statusKindOf(t.statusState || t.status) === 'completed').length,
      failed: tasks.filter(t => statusKindOf(t.statusState || t.status) === 'failed').length,
    };
    const mappings = [
      ['#dashboard-total', totals.total],
      ['#dashboard-waiting', totals.waiting],
      ['#dashboard-active', totals.active],
      ['#dashboard-done', totals.completed],
      ['#dashboard-failed', totals.failed],
    ];
    for (const [selector, value] of mappings) {
      const node = $(selector);
      if (node) node.textContent = String(value);
    }
  }
  function fmtTime(iso) {
    return fmtLocalTimestamp(iso);
  }
  function parseTimestamp(iso) {
    const value = String(iso || '').trim();
    if (!value) return null;
    const match = value.match(
      /^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(?:([zZ])|([+\-])(\d{2}):(\d{2}))?$/,
    );
    if (match) {
      const [
        ,
        year,
        month,
        day,
        hour,
        minute,
        second,
        fraction = '0',
        zoneToken = '',
        offsetSign = '',
        offsetHour = '00',
        offsetMinute = '00',
      ] = match;
      const milliseconds = Number(fraction.padEnd(3, '0').slice(0, 3));
      if (!zoneToken && !offsetSign) {
        return new Date(
          Number(year),
          Number(month) - 1,
          Number(day),
          Number(hour),
          Number(minute),
          Number(second),
          milliseconds,
        );
      }
      const offsetMinutes = zoneToken
        ? 0
        : ((offsetSign === '-' ? -1 : 1) * ((Number(offsetHour) * 60) + Number(offsetMinute)));
      return new Date(
        Date.UTC(
          Number(year),
          Number(month) - 1,
          Number(day),
          Number(hour),
          Number(minute),
          Number(second),
          milliseconds,
        ) - (offsetMinutes * 60 * 1000),
      );
    }
    const parsed = new Date(value);
    return isNaN(parsed.getTime()) ? null : parsed;
  }
  function fmtLocalTimestamp(iso) {
    if (!iso) return '--';
    try {
      const d = parseTimestamp(iso);
      if (!d) return String(iso);
      const month = String(d.getMonth() + 1).padStart(2, '0');
      const day = String(d.getDate()).padStart(2, '0');
      const hour = String(d.getHours()).padStart(2, '0');
      const minute = String(d.getMinutes()).padStart(2, '0');
      const second = String(d.getSeconds()).padStart(2, '0');
      return `${month}-${day} ${hour}:${minute}:${second}`;
    } catch {
      return String(iso);
    }
  }
  function fmtLogTimestamp(iso) {
    if (!iso) return '--';
    try {
      const d = parseTimestamp(iso);
      if (!d) return String(iso);
      return fmtLocalTimestamp(iso);
    } catch {
      return String(iso);
    }
  }
  function elapsedMs(createdIso, updatedIso) {
    if (!createdIso) return '--';
    const startDate = parseTimestamp(createdIso);
    const endDate = updatedIso ? parseTimestamp(updatedIso) : null;
    const start = startDate ? startDate.getTime() : NaN;
    const end = endDate ? endDate.getTime() : Date.now();
    if (isNaN(start) || isNaN(end)) return '--';
    return formatDurationMs(Math.max(0, end - start));
  }
  function formatDurationMs(ms) {
    const totalSeconds = Math.max(0, Math.floor(ms / 1000));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    if (hours > 0) return `${hours}h ${String(minutes).padStart(2, '0')}m`;
    if (minutes > 0) return `${minutes}m ${String(seconds).padStart(2, '0')}s`;
    return `${seconds}s`;
  }
  function durationBetween(startIso, endIso) {
    if (!startIso) return '--';
    const startDate = parseTimestamp(startIso);
    const endDate = endIso ? parseTimestamp(endIso) : null;
    const start = startDate ? startDate.getTime() : NaN;
    const end = endDate ? endDate.getTime() : Date.now();
    if (isNaN(start) || isNaN(end)) return '--';
    return formatDurationMs(Math.max(0, end - start));
  }
  function taskTypeOf(task) {
    return String(task.taskType || task.task_type || 'general').toLowerCase();
  }
  function taskTypeLabel(task) {
    const type = taskTypeOf(task);
    if (type === 'development') return 'Development';
    if (type === 'office') return 'Office';
    return 'Task';
  }
  function latestMeaningfulLog(taskId) {
    const logs = state.logsByTask[taskId] || [];
    for (let index = logs.length - 1; index >= 0; index -= 1) {
      const row = logs[index] || {};
      const agent = String(row.agent || '').toLowerCase();
      const message = String(row.message || '').trim();
      if (!message) continue;
      if (agent === 'compass' && message.toLowerCase().includes('running in the background')) continue;
      return row;
    }
    return null;
  }
  function currentStepFromLogs(task) {
    const latest = latestMeaningfulLog(task.task_id || task.id || '');
    if (!latest || !latest.message) return '';
    return String(latest.message);
  }
  function mergedProgressSignals(task) {
    const steps = Array.isArray(task.progressSteps) ? [...task.progressSteps] : [];
    const logs = state.logsByTask[task.task_id || task.id || ''] || [];
    for (const row of logs) {
      const text = String(row.message || '').trim();
      if (!text) continue;
      steps.push({
        text,
        agent: row.agent || '',
        ts: row.timestamp || row.ts || '',
        fromLogs: true,
      });
    }
    steps.sort((a, b) => String(a.ts || '').localeCompare(String(b.ts || '')));
    return steps;
  }
  function summarizeTaskNote(task, kind) {
    const currentStep = String(task.currentMajorStep || task.current_major_step || '').trim() || currentStepFromLogs(task);
    const request = String(task.userRequest || '').trim();
    const summary = String(task.summary || '').trim();
    const title = displayTitle(task, kind);
    const candidates = [currentStep, request, summary];
    for (const candidate of candidates) {
      if (!candidate) continue;
      if (candidate === title) continue;
      return candidate;
    }
    return task.task_id || '';
  }
  function ownerOf(task) {
    if (!task) return 'Compass';
    if (task.currentOwner) return task.currentOwner;
    if (task.owner) return task.owner;
    if (task.assignedAgent) return task.assignedAgent;
    const logs = state.logsByTask[task.task_id || task.id || ''] || [];
    const lastLogAgent = [...logs].reverse().map(row => row.agent).find(agent => agent && String(agent).toLowerCase() !== 'compass');
    if (lastLogAgent) return lastLogAgent;
    const steps = mergedProgressSignals(task);
    const lastAgent = [...steps].reverse().map(step => step.agent).find(Boolean);
    return lastAgent || 'Compass';
  }
  function escapeAttr(value) {
    return esc(value).replace(/"/g, '&quot;');
  }

  function upsertTask(task) {
    if (!task || !task.task_id) return;
    state.tasks[task.task_id] = task;
    if (!state.order.includes(task.task_id)) state.order.unshift(task.task_id);
    state.order.sort((a, b) => {
      const ta = state.tasks[a], tb = state.tasks[b];
      const ka = taskSortStamp(ta, a);
      const kb = taskSortStamp(tb, b);
      if (ka !== kb) return ka < kb ? 1 : -1;
      return a < b ? 1 : (a > b ? -1 : 0);
    });
  }

  async function loadTasks(autoSelect) {
    try {
      const resp = await fetch('/api/tasks');
      const data = await resp.json();
      state.tasks = {}; state.order = [];
      for (const t of (data.tasks || [])) upsertTask(t);
      if (autoSelect && !state.tasks[state.selectedTaskId] && state.selectedTaskId !== NEW_REQUEST_ID && !isOverviewSelection(state.selectedTaskId)) {
        state.selectedTaskId = state.order[0] || NEW_REQUEST_ID;
      }
      if (state.selectedTaskId !== NEW_REQUEST_ID && !isOverviewSelection(state.selectedTaskId) && state.tasks[state.selectedTaskId]) {
        await loadTaskDetail(state.selectedTaskId);
      }
      renderTaskList(); renderChat(); renderDetail();
      if (state.selectedTaskId !== NEW_REQUEST_ID && !isOverviewSelection(state.selectedTaskId)) subscribeLogs(state.selectedTaskId);
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
    renderDashboard();
    const hasSelection = state.selectedTaskId !== NEW_REQUEST_ID && !isOverviewSelection(state.selectedTaskId);
    const isOverview = isOverviewSelection(state.selectedTaskId);
    const scroll = $('#task-list-scroll');
    if (scroll) scroll.parentElement.classList.toggle('has-selection', hasSelection);
    if (scroll) scroll.parentElement.classList.toggle('is-overview', isOverview);
    if (root) root.classList.toggle('has-selection', hasSelection);
    if (root) root.classList.toggle('is-overview', isOverview);
    const active = state.selectedTaskId === NEW_REQUEST_ID ? ' active' : '';
    html += `<div class="task-item new-request${active}" data-task-id="${NEW_REQUEST_ID}">
      <div class="task-item-head">
        <div class="task-title">New Request</div>
        <div class="task-type new">New</div>
      </div>
      <div class="task-note">Start a new Compass task here.</div>
    </div>`;
    for (const tid of orderedTaskIds()) {
      const t = state.tasks[tid]; if (!t) continue;
      const kind = statusKindOf(t.statusState || t.status);
      const isActive = tid === state.selectedTaskId;
      const taskType = taskTypeOf(t);
      const typeLabel = esc(taskTypeLabel(t));
      const titleText = esc(displayTitle(t, kind));
      const noteText = esc(summarizeTaskNote(t, kind));
      html += `<div class="task-item ${escapeAttr(taskType)}${isActive?' active':''}" data-task-id="${escapeAttr(tid)}" data-status="${escapeAttr(kind)}">
        <div class="task-item-head">
          <div class="task-title">${titleText}</div>
          <div class="task-type ${escapeAttr(taskType)}">${typeLabel}</div>
        </div>
        <div class="task-note">${noteText}</div>
        <div class="task-foot">
          <span class="status-pill ${escapeAttr(kind)}">${esc(statusLabel(kind))}</span>
          <span class="task-time">${esc(fmtLocalTimestamp(t.createdAt))}</span>
        </div>
      </div>`;
    }
    root.innerHTML = html;
    if (root.parentElement) root.parentElement.classList.toggle('has-selection', hasSelection);
    if (root.parentElement) root.parentElement.classList.toggle('is-overview', isOverview);
    root.classList.toggle('has-selection', hasSelection);
    root.classList.toggle('is-overview', isOverview);
    root.querySelectorAll('.task-item').forEach(el => {
      el.addEventListener('click', () => selectTask(el.getAttribute('data-task-id')));
    });
  }

  function selectTask(tid) {
    if (!tid) return;
    if (tid === state.selectedTaskId && tid !== NEW_REQUEST_ID) {
      state.selectedTaskId = OVERVIEW_ID;
      renderTaskList(); renderChat(); renderDetail();
      return;
    }
    state.selectedTaskId = tid;
    renderTaskList(); renderChat(); renderDetail();
    if (tid !== NEW_REQUEST_ID && !isOverviewSelection(tid)) subscribeLogs(tid);
    loadTaskDetail(tid).then(() => {
      renderTaskList(); renderChat(); renderDetail();
    });
  }

  function renderChat() {
    const tid = state.selectedTaskId;
    const scroll = $('#chat-scroll');
    const composerInput = $('#composer-input');
    const composerSend  = $('#composer-send');
    const composerNote  = $('#composer-note');

    if (tid === NEW_REQUEST_ID || isOverviewSelection(tid)) {
      scroll.innerHTML = `<div class="empty-state">${tid === NEW_REQUEST_ID ? 'Send a request to create a new Compass task.' : 'Select a task to inspect it, or send a new request.'}</div>`;
      composerNote.style.display = 'none';
      composerInput.disabled = false;
      composerInput.placeholder = 'Describe a new task...';
      composerSend.disabled = false;
      composerSend.textContent = 'Send';
      composerInput.dataset.mode = 'create';
      composerInput.dataset.targetTaskId = '';
      return;
    }

    const t = state.tasks[tid];
    if (!t) {
      scroll.innerHTML = `<div class="empty-state">Task not loaded.</div>`;
      return;
    }

    const history = Array.isArray(t.chatHistory) ? t.chatHistory : [];
    if (!history.length) {
      scroll.innerHTML = `<div class="empty-state">No chat history yet.</div>`;
    } else {
      scroll.innerHTML = history.map(entry => {
        const role = (entry.role || 'agent').toLowerCase();
        const tone = entry.tone || (entry.style || 'normal');
        const cls = role === 'user' ? 'user' : 'agent';
        const styleCls = ({ waiting:'waiting','input-required':'waiting',failed:'failed',completed:'completed'})[tone] || '';
        const alignClass = cls === 'user' ? 'user' : 'agent';
        return `<div class="chat-entry ${alignClass}">
          <div class="bubble ${cls} ${styleCls}">
            <div class="bubble-text">${esc(entry.text || '').replace(/\n/g,'<br>')}</div>
          </div>
          <div class="bubble-meta">${esc(fmtLocalTimestamp(entry.ts || entry.timestamp || entry.createdAt || ''))}</div>
        </div>`;
      }).join('');
    }
    scroll.scrollTop = scroll.scrollHeight;

    const kind = statusKindOf(t.statusState || t.status);
    const isWaiting = kind === 'waiting';
    composerInput.disabled = false;
    composerSend.textContent = 'Send';
    if (isWaiting) {
      composerNote.style.display = 'none';
      composerInput.placeholder = `Reply to ${tid}...`;
      composerSend.disabled = false;
      composerInput.dataset.mode = 'resume';
      composerInput.dataset.targetTaskId = tid;
    } else {
      composerNote.style.display = 'none';
      const terminal = (kind === 'completed' || kind === 'failed');
      composerInput.disabled = terminal;
      composerSend.disabled = terminal;
      composerInput.placeholder = terminal ? 'This task is closed. Select New Request to start a new one.' : `Reply to ${tid}...`;
      composerInput.dataset.mode = terminal ? 'disabled' : 'reply';
      composerInput.dataset.targetTaskId = tid;
    }
  }

  function phaseStateClass(phase, currentKey, taskKind) {
    if (phase.key === currentKey) {
      if (taskKind === 'failed') return 'failed';
      if (taskKind === 'waiting' || taskKind === 'active') return 'warn';
      return 'done';
    }
    if (phase.updateCount > 0) return 'done';
    return 'pending';
  }
  function phaseMarkForClass(statusClass) {
    if (statusClass === 'done') return '✓';
    if (statusClass === 'failed') return '✕';
    if (statusClass === 'warn') return '!';
    return '';
  }
  function timelineAgentDetail(agent, detail, fallbackDetail = '') {
    const owner = String(agent || '').trim();
    const text = String(detail || '').trim() || String(fallbackDetail || '').trim();
    if (owner && text) return `${owner}: ${text}`;
    return text || owner;
  }
  function timelineHtmlForDevelopment(task, semanticPhases, currentStep, kind, expanded) {
    const currentIndex = semanticPhases.phases.findIndex(phase => phase.key === semanticPhases.currentKey);
    const rows = semanticPhases.phases.map((phase, index) => {
      const statusClass = phaseStateClass(phase, semanticPhases.currentKey, kind);
      const reached = phase.updateCount > 0 || phase.key === semanticPhases.currentKey || (currentIndex >= 0 && index < currentIndex);
      const effectiveClass = reached ? statusClass : 'pending';
      const rawDetail = phase.key === semanticPhases.currentKey
        ? String(phase.detail || currentStep || '').trim()
        : String(phase.detail || '').trim();
      const detailText = rawDetail && rawDetail !== String(phase.label || '').trim() ? rawDetail : '';
      const metaText = reached
        ? timelineAgentDetail(
            phase.agent,
            detailText,
            phase.key === semanticPhases.currentKey ? 'Longer execution detail continues in merged logs.' : '',
          )
        : 'Not reached yet.';
      const facts = `
        <div class="timeline-facts">
          <span class="timeline-fact"><span class="timeline-fact-label">Started</span>${esc(phase.startTs ? fmtLocalTimestamp(phase.startTs) : '--')}</span>
          <span class="timeline-fact"><span class="timeline-fact-label">Time Spent</span>${esc(phase.startTs ? durationBetween(phase.startTs, phase.endTs || task.updatedAt) : '--')}</span>
        </div>`;
      return `<div class="timeline-row ${effectiveClass}${phase.key === semanticPhases.currentKey ? ' current' : ''}">
        <div class="timeline-mark">${phaseMarkForClass(effectiveClass)}</div>
        <div class="timeline-headline">
          <div class="timeline-title">${esc(phase.label)}</div>
          ${facts}
        </div>
        <div class="timeline-meta">${esc(metaText)}</div>
      </div>`;
    }).join('');
    return `<div class="phase-rail" style="display:none">${semanticPhases.phases.map(phase => `<span class="phase-pill${phase.key === semanticPhases.currentKey ? ' current' : ''}">${esc(phase.label)}</span>`).join('')}</div>
      <div class="timeline-list${expanded ? '' : ' collapsed'}">${rows}</div>`;
  }
  function timelineHtmlForGeneric(task, steps, kind, expanded) {
    if (!steps.length) return '<div class="empty-state">No workflow steps yet.</div>';
    return `<div class="timeline-list">${steps.map((step, index) => {
      const current = index === steps.length - 1;
      const currentClass = kind === 'failed' ? 'failed' : ((kind === 'waiting' || kind === 'active') ? 'warn' : 'done');
      const rowClass = current ? `${currentClass} current` : 'done';
      const currentMark = currentClass === 'failed' ? '✕' : (currentClass === 'warn' ? '!' : '✓');
      const owner = step.agent || ownerOf(task);
      const metaText = current
        ? timelineAgentDetail(owner, '', 'Longer execution detail continues in merged logs.')
        : timelineAgentDetail(owner, '');
      return `<div class="timeline-row ${rowClass}">
        <div class="timeline-mark">${current ? currentMark : '✓'}</div>
        <div class="timeline-headline">
          <div class="timeline-title">${esc(step.text || step.step || `Step ${index + 1}`)}</div>
          <div class="timeline-facts">
            <span class="timeline-fact"><span class="timeline-fact-label">Started</span>${esc(step.ts ? fmtLocalTimestamp(step.ts) : '--')}</span>
            <span class="timeline-fact"><span class="timeline-fact-label">Time Spent</span>${esc(step.ts ? durationBetween(step.ts, current ? task.updatedAt : step.ts) : '--')}</span>
          </div>
        </div>
        <div class="timeline-meta">${esc(metaText)}</div>
      </div>`;
    }).join('')}</div>`.replace('timeline-list">', `timeline-list${expanded ? '' : ' collapsed'}">`);
  }
  function mergedArtifactMetadata(task) {
    const metadata = { ...((task && task.metadata) || {}) };
    const artifacts = Array.isArray(task && task.artifacts) ? task.artifacts : [];
    for (const artifact of artifacts) {
      if (artifact && artifact.metadata && typeof artifact.metadata === 'object') {
        Object.assign(metadata, artifact.metadata);
      }
    }
    return metadata;
  }
  function artifactTextSummary(task) {
    const artifacts = Array.isArray(task && task.artifacts) ? task.artifacts : [];
    const chunks = [];
    for (const artifact of artifacts) {
      const parts = Array.isArray(artifact && artifact.parts) ? artifact.parts : [];
      for (const part of parts) {
        const text = String((part && part.text) || '').trim();
        if (text) chunks.push(text);
      }
    }
    return chunks.join('\n').trim();
  }
  function latestLogMessage(taskId, predicate) {
    const logs = state.logsByTask[taskId] || [];
    for (let idx = logs.length - 1; idx >= 0; idx -= 1) {
      const entry = logs[idx] || {};
      const message = String(entry.message || '').trim();
      if (!message) continue;
      if (!predicate || predicate(entry, message)) return message;
    }
    return '';
  }
  function latestLogField(taskId, fieldNames) {
    const logs = state.logsByTask[taskId] || [];
    const names = Array.isArray(fieldNames) ? fieldNames : [fieldNames];
    for (let idx = logs.length - 1; idx >= 0; idx -= 1) {
      const message = String((logs[idx] || {}).message || '');
      if (!message) continue;
      for (const name of names) {
        const pattern = new RegExp(`${name}='([^']+)'`);
        const match = message.match(pattern);
        if (match && match[1]) return match[1].trim();
      }
    }
    return '';
  }
  function latestOfficeResultSummary(taskId) {
    const message = latestLogMessage(taskId, (_, value) => value.includes("result_preview") && value.includes("summary': '"));
    if (!message) return '';
    const match = message.match(/summary': '([^']+)/);
    return match && match[1] ? match[1].trim() : '';
  }
  function looksGenericCompletionSummary(summary, taskType) {
    const value = String(summary || '').trim().toLowerCase();
    if (!value) return true;
    if (taskType === 'office') {
      return value === 'office task dispatched. status: completed'
        || value === 'office task completed'
        || value === 'office task returned a terminal result'
        || value === "office dispatch complete status='completed'"
        || value.startsWith("office execution completed capability=");
    }
    if (taskType === 'development') {
      return value === 'development task completed successfully.' || value.startsWith('development task completed successfully.\npr:');
    }
    return false;
  }
  function summarizeTaskOutcome(task, kind, currentStep) {
    const taskType = taskTypeOf(task);
    const meta = mergedArtifactMetadata(task);
    const taskId = task.task_id || task.id || '';
    const summary = String(task.summary || '').trim();
    const metadataSummary = String(meta.summary || '').trim();
    const statusMessage = String(task.statusMessage || '').trim();
    const artifactSummary = artifactTextSummary(task);
    const latestError = latestLogMessage(task.task_id || task.id, (entry) => String(entry.level || '').toUpperCase() === 'ERROR');
    const latestInfo = latestLogMessage(task.task_id || task.id);
    if (kind === 'failed') {
      const errorText = latestError || String(meta.message || '').trim() || statusMessage || artifactSummary || metadataSummary || summary || currentStep || 'Task failed without a detailed error message.';
      return { label: 'Error Summary', text: errorText };
    }
    if (kind !== 'completed') {
      return null;
    }
    const lines = [];
    const genericCandidates = [metadataSummary, artifactSummary, statusMessage, summary, currentStep];
    const primarySummary = genericCandidates.find((value) => {
      const text = String(value || '').trim();
      return text && !looksGenericCompletionSummary(text, taskType);
    });
    if (primarySummary) lines.push(primarySummary);
    if (taskType === 'development') {
      const repoUrl = String(meta.repoUrl || '').trim();
      const prUrl = String(meta.prUrl || '').trim();
      const branch = String(meta.branch || '').trim();
      const jiraRef = String(meta.jiraInReview || meta.jiraKey || '').trim();
      if (prUrl) lines.push(`PR: ${prUrl}`);
      if (branch) lines.push(`Branch: ${branch}`);
      if (repoUrl) lines.push(`Repo: ${repoUrl}`);
      if (jiraRef) lines.push(`Jira: ${jiraRef}`);
    } else if (taskType === 'office') {
      const workspacePath = String(meta.workspacePath || latestLogField(taskId, ['artifacts_dir', 'workspace_root'])).trim();
      const reportPath = String(meta.deliveryReportPath || latestLogField(taskId, ['task_report', 'task_report_path'])).trim();
      const rawOutputPath = String(meta.rawOutputPath || latestLogField(taskId, ['raw_output_path'])).trim();
      const officeResultSummary = latestOfficeResultSummary(taskId);
      if (officeResultSummary && !looksGenericCompletionSummary(officeResultSummary, taskType)) lines.unshift(officeResultSummary);
      if (!lines.length && (workspacePath || reportPath || rawOutputPath)) {
        lines.push('Office output delivered to the task workspace.');
      }
      if (workspacePath) lines.push(`Output: ${workspacePath}`);
      if (reportPath) lines.push(`Report: ${reportPath}`);
      if (rawOutputPath) lines.push(`Raw Output: ${rawOutputPath}`);
      const officeSignal = latestLogMessage(taskId, (_, message) => {
        const value = message.toLowerCase();
        return value.includes('written to the workspace')
          || value.includes('delivery verified')
          || value.includes('task_report=')
          || value.includes('task_report_path=')
          || value.includes('report written');
      });
      if (officeSignal && !lines.some(line => line.startsWith('Summary:'))) {
        lines.push('Summary: Office output was verified and written successfully.');
      }
    } else if (latestInfo) {
      lines.push(latestInfo);
    }
    const unique = [];
    for (const line of lines) {
      const text = String(line || '').trim();
      if (text && !unique.includes(text)) unique.push(text);
    }
    if (!unique.length) return null;
    return { label: 'Completion Summary', text: unique.join('\n') };
  }
  function renderDetail() {
    const tid = state.selectedTaskId;
    const root = $('#detail-stack');
    const taskInfoHeadTaskId = $('#task-info-head-task-id');
    const taskInfoHeadMeta = $('#task-info-head-meta');
    const renderTaskInfoEmpty = (title, body) => (
      `<div class="task-info-empty"><strong>${esc(title)}</strong><p>${esc(body)}</p></div>`
    );
    if (tid === NEW_REQUEST_ID || isOverviewSelection(tid)) {
      if (taskInfoHeadTaskId) taskInfoHeadTaskId.textContent = '';
      if (taskInfoHeadMeta) taskInfoHeadMeta.innerHTML = '';
      root.innerHTML = renderTaskInfoEmpty('No task selected yet', 'Select a task to view details, or send a new request.');
      return;
    }
    const t = state.tasks[tid];
    if (!t) {
      if (taskInfoHeadTaskId) taskInfoHeadTaskId.textContent = '';
      if (taskInfoHeadMeta) taskInfoHeadMeta.innerHTML = '';
      root.innerHTML = renderTaskInfoEmpty('Task not loaded', 'The selected task is unavailable right now. Try again after the next refresh.');
      return;
    }
    const kind = statusKindOf(t.statusState || t.status);
    const steps = mergedProgressSignals(t);
    const currentStep = t.currentMajorStep || currentStepFromLogs(t) || (steps.length ? steps[steps.length-1].text : '');
    const semanticPhases = deriveMajorPhases(t, currentStep);
    const phaseExpanded = !!state.phaseExpandedByTask[tid];
    const taskType = taskTypeOf(t);
    const typeLabel = taskTypeLabel(t);
    const hasTimelineToggle = semanticPhases ? semanticPhases.phases.length > 1 : steps.length > 1;
    const timelineBody = semanticPhases
      ? timelineHtmlForDevelopment(t, semanticPhases, currentStep, kind, phaseExpanded)
      : timelineHtmlForGeneric(t, steps, kind, phaseExpanded);
    const orchestratorTaskId = String(t.orchestratorTaskId || t.task_id || t.id || '').trim();
    const originalRequest = String(t.userRequest || t.user_request || '').trim();
    const detailTitle = originalRequest || tid;
    const detailTitleLabel = originalRequest ? 'Original Request' : 'Task';
    const outcome = summarizeTaskOutcome(t, kind, currentStep);
    if (taskInfoHeadTaskId) {
      taskInfoHeadTaskId.textContent = orchestratorTaskId;
    }
    if (taskInfoHeadMeta) {
      taskInfoHeadMeta.innerHTML = `<span class="detail-type-pill ${escapeAttr(taskType)}" style="margin-top:0">${esc(typeLabel)}</span>`;
    }

    root.innerHTML = `
      <div class="detail-card spotlight ${escapeAttr(kind)}" id="task-spotlight">
        <div class="detail-request-row">
          <div class="detail-request-copy">
            <span class="detail-request-kicker">${esc(detailTitleLabel)}</span>
            <div class="detail-request-title">${esc(detailTitle)}</div>
          </div>
          <span class="status-pill ${escapeAttr(kind)}">${esc(statusLabel(kind))}</span>
        </div>
        ${outcome ? `<div class="detail-section">
          <span class="detail-label">${esc(outcome.label)}</span>
          <div class="detail-value multiline">${esc(outcome.text)}</div>
        </div>` : ''}
      </div>
      <div class="detail-card">
        <div class="workflow-head">
          <div class="kicker">Workflow Timeline</div>
          ${hasTimelineToggle ? `<button class="workflow-toggle" type="button" id="workflow-toggle">${phaseExpanded ? 'Current step only' : 'Show all steps'}</button>` : ''}
        </div>
        ${timelineBody}
      </div>
      <div class="detail-card" id="logs-card">
        <div class="workflow-head">
          <div class="kicker">Task Logs</div>
          <div class="log-toolbar" id="log-toolbar">
            <div class="log-filter" id="filter-agent-shell"></div>
            <div class="log-filter" id="filter-level-shell"></div>
          </div>
        </div>
        <div class="log-box" id="log-box"></div>
      </div>`;
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
  function logLevelOptions() {
    return [
      { value: 'all', label: 'All' },
      { value: 'DEBUG', label: 'DEBUG' },
      { value: 'INFO', label: 'INFO' },
      { value: 'WARN', label: 'WARN' },
      { value: 'ERROR', label: 'ERROR' },
    ];
  }
  function renderLogFilterControl(kind, options, selected) {
    const triggerId = kind === 'agent' ? 'filter-agent-trigger' : 'filter-level-trigger';
    const menuId = kind === 'agent' ? 'filter-agent-menu' : 'filter-level-menu';
    const selectedOption = options.find(option => option.value === selected) || options[0];
    const open = state.openLogFilter === kind ? ' is-open' : '';
    return `<div class="log-filter${open}" data-filter-kind="${kind}">
      <button class="log-filter-trigger" type="button" id="${triggerId}">
        <span>${esc(selectedOption.label)}</span>
        <span class="log-filter-caret">▾</span>
      </button>
      <div class="log-filter-menu" id="${menuId}">
        ${options.map(option => `<button class="log-filter-option${option.value === selected ? ' is-active' : ''}" type="button" data-filter-kind="${kind}" data-filter-value="${escapeAttr(option.value)}">
          <span>${esc(option.label)}</span>
          <span class="log-filter-check">${option.value === selected ? '✓' : ''}</span>
        </button>`).join('')}
      </div>
    </div>`;
  }
  function bindLogFilterControls() {
    const agentTrigger = $('#filter-agent-trigger');
    const levelTrigger = $('#filter-level-trigger');
    if (agentTrigger) {
      agentTrigger.addEventListener('click', event => {
        event.stopPropagation();
        state.openLogFilter = state.openLogFilter === 'agent' ? null : 'agent';
        renderLogs();
      });
    }
    if (levelTrigger) {
      levelTrigger.addEventListener('click', event => {
        event.stopPropagation();
        state.openLogFilter = state.openLogFilter === 'level' ? null : 'level';
        renderLogs();
      });
    }
    document.querySelectorAll('.log-filter-option').forEach(button => {
      button.addEventListener('click', event => {
        event.stopPropagation();
        const filterKind = button.getAttribute('data-filter-kind');
        const filterValue = button.getAttribute('data-filter-value');
        if (!filterKind) return;
        state.filters[filterKind] = filterValue;
        state.openLogFilter = null;
        renderLogs();
      });
    });
  }
  function renderLogs() {
    const tid = state.selectedTaskId;
    const box = $('#log-box'); if (!box) return;
    const logs = [...(state.logsByTask[tid] || [])].sort((a, b) => String(a.timestamp || '').localeCompare(String(b.timestamp || '')));
    const toolbar = $('#log-toolbar');
    const agents = Array.from(new Set(logs.map(l => l.agent).filter(Boolean))).sort((a, b) => a.localeCompare(b));
    const minLevel = LEVEL_ORDER[state.filters.level] || 0;
    const visible = logs.filter(l => {
      if (state.filters.agent !== 'all' && l.agent !== state.filters.agent) return false;
      const lv = LEVEL_ORDER[(l.level || '').toUpperCase()] || 0;
      if (state.filters.level !== 'all' && lv < minLevel) return false;
      return true;
    });
    if (toolbar) {
      const agentOptions = [{ value: 'all', label: 'All' }, ...agents.map(agent => ({ value: agent, label: agent }))];
      toolbar.innerHTML = `
        ${renderLogFilterControl('agent', agentOptions, state.filters.agent)}
        ${renderLogFilterControl('level', logLevelOptions(), state.filters.level)}`;
      bindLogFilterControls();
    }
    if (!visible.length) {
      box.innerHTML = `<div class="empty-state">No logs yet for this filter.</div>`;
      return;
    }
    box.innerHTML = visible.map(l => {
      return `<div class="log-line">
      <span class="log-ts">${esc(fmtLogTimestamp(l.timestamp || ''))}</span>
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
      if (tid === state.selectedTaskId) renderDetail();
    }).catch(() => {});
    try {
      const es = new EventSource(`/logs/stream/${encodeURIComponent(tid)}`);
      es.addEventListener('log.appended', ev => {
        try {
          const entry = JSON.parse(ev.data);
          (state.logsByTask[tid] = state.logsByTask[tid] || []).push(entry);
          if (tid === state.selectedTaskId) renderDetail();
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
    document.addEventListener('click', event => {
      if (!event.target.closest('.log-filter') && state.openLogFilter) {
        state.openLogFilter = null;
        renderLogs();
      }
    });
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
  <div class="page">
    <div class="app-shell">
      <section class="dashboard" id="dashboard">
        <div class="dashboard-card total">
          <strong id="dashboard-total">0</strong>
          <span>Total</span>
        </div>
        <div class="dashboard-card waiting">
          <strong id="dashboard-waiting">0</strong>
          <span>Waiting for Input</span>
        </div>
        <div class="dashboard-card active">
          <strong id="dashboard-active">0</strong>
          <span>In Progress</span>
        </div>
        <div class="dashboard-card done">
          <strong id="dashboard-done">0</strong>
          <span>Completed</span>
        </div>
        <div class="dashboard-card failed">
          <strong id="dashboard-failed">0</strong>
          <span>Failed</span>
        </div>
      </section>

      <div class="workspace">
        <aside class="panel" id="task-list-panel">
          <div class="panel-head">
            <strong>Task List</strong>
          </div>
          <div class="panel-body">
            <div id="task-list-scroll">
              <div class="task-list" id="task-list">{server_task_list}</div>
            </div>
          </div>
        </aside>

        <section class="panel" id="task-chat-panel">
          <div class="panel-head">
            <strong>Compass Chat</strong>
          </div>
          <div class="panel-body">
            <div id="chat-scroll"></div>
            <div class="composer">
              <div class="composer-note" id="composer-note" style="display:none"></div>
              <div class="composer-box">
                <textarea id="composer-input" rows="2" placeholder="Describe a new task..."></textarea>
                <button id="composer-send">Send</button>
              </div>
            </div>
          </div>
        </section>

        <section class="panel" id="task-info-panel">
          <div class="panel-head">
            <div class="panel-head-title">
              <strong>Task Info</strong>
              <span class="detail-head-task-id" id="task-info-head-task-id"></span>
            </div>
            <span class="detail-head-tag" id="task-info-head-meta"></span>
          </div>
          <div class="panel-body">
            <div class="detail-stack" id="detail-stack">
              <div class="detail-card spotlight" id="task-spotlight">
                <div class="detail-request-row">
                  <div class="detail-request-copy">
                    <span class="detail-request-kicker">Original Request</span>
                    <div class="detail-request-title">Select a task</div>
                  </div>
                </div>
                <p style="margin-top:8px; font-size:13px; line-height:1.7; color:var(--muted);">
                  Select a task to view the most important status, next action, workflow timeline, and task logs.
                </p>
              </div>
              <div class="detail-card">
                <div class="workflow-head">
                  <div class="kicker">Workflow Timeline</div>
                  <button class="workflow-toggle" type="button">Show all steps</button>
                </div>
                <p style="margin-top:8px; font-size:14px; line-height:1.6; color:var(--muted);">
                  Current step only
                </p>
              </div>
              <div class="detail-card" id="logs-card">
                <div class="workflow-head">
                  <div class="kicker">Task Logs</div>
                  <div class="log-toolbar" id="log-toolbar">
                    <div class="log-filter">
                      <button class="log-filter-trigger" type="button" id="filter-agent-trigger">
                        <span>All</span>
                        <span class="log-filter-caret">▾</span>
                      </button>
                      <div class="log-filter-menu" id="filter-agent-menu"></div>
                    </div>
                    <div class="log-filter">
                      <button class="log-filter-trigger" type="button" id="filter-level-trigger">
                        <span>All</span>
                        <span class="log-filter-caret">▾</span>
                      </button>
                      <div class="log-filter-menu" id="filter-level-menu"></div>
                    </div>
                  </div>
                </div>
                <div class="log-box"><div class="empty-state">Select a task to load task logs.</div></div>
              </div>
            </div>
          </div>
        </section>
      </div>
    </div>
  </div>
  <script>{_INLINE_JS}</script>
</body>
</html>
"""
