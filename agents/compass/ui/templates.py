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
.dashboard-card.waiting {
  cursor: pointer;
  transition: background 120ms ease, border-color 120ms ease, transform 120ms ease;
}
.dashboard-card.waiting:hover {
  background: rgba(38, 53, 66, 0.92);
  border-color: rgba(212, 152, 36, 0.42);
  transform: translateY(-1px);
}
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
  min-height: 56px;
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
/* Markdown-rendered completion summary */
.markdown-content {
  font-size: 13px;
  line-height: 1.6;
  color: var(--ink);
  word-break: break-word;
  white-space: normal;
}
.markdown-content > p {
  margin: 0 0 8px 0;
}
.markdown-content > p:last-child {
  margin-bottom: 0;
}
.markdown-content h1,
.markdown-content h2,
.markdown-content h3,
.markdown-content h4,
.markdown-content h5,
.markdown-content h6 {
  margin: 14px 0 6px 0;
  font-weight: 700;
  line-height: 1.3;
  letter-spacing: -0.01em;
  color: var(--ink);
}
.markdown-content h1:first-child,
.markdown-content h2:first-child,
.markdown-content h3:first-child,
.markdown-content h4:first-child,
.markdown-content h5:first-child,
.markdown-content h6:first-child { margin-top: 0; }
.markdown-content h1 { font-size: 18px; }
.markdown-content h2 { font-size: 16px; }
.markdown-content h3 { font-size: 15px; }
.markdown-content h4 { font-size: 14px; }
.markdown-content h5,
.markdown-content h6 { font-size: 13px; color: var(--muted); }
.markdown-content ul,
.markdown-content ol {
  margin: 0 0 8px 0;
  padding-left: 20px;
}
.markdown-content li {
  margin-bottom: 4px;
}
.markdown-content li:last-child { margin-bottom: 0; }
.markdown-content blockquote {
  margin: 0 0 8px 0;
  padding: 6px 12px;
  border-left: 3px solid var(--accent);
  background: rgba(147, 198, 208, 0.06);
  color: var(--muted);
  border-radius: 0 8px 8px 0;
}
.markdown-content code {
  padding: 1px 6px;
  border-radius: 5px;
  background: rgba(127, 195, 209, 0.12);
  color: var(--accent-strong);
  font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
  font-size: 0.92em;
}
.markdown-content pre {
  margin: 0 0 8px 0;
  padding: 10px 12px;
  border-radius: 10px;
  background: rgba(9, 16, 23, 0.6);
  border: 1px solid rgba(145, 171, 189, 0.1);
  overflow-x: auto;
}
.markdown-content pre code {
  padding: 0;
  background: transparent;
  color: var(--ink);
  font-size: 12px;
  line-height: 1.5;
  display: block;
}
.markdown-content a {
  color: var(--accent-strong);
  text-decoration: underline;
  text-decoration-color: rgba(147, 198, 208, 0.32);
  text-underline-offset: 2px;
  word-break: break-all;
}
.markdown-content a:hover {
  text-decoration-color: var(--accent-strong);
}
.markdown-content strong { font-weight: 700; color: var(--ink); }
.markdown-content em { font-style: italic; color: var(--ink); }
.markdown-content del { text-decoration: line-through; color: var(--muted); }
.markdown-content hr {
  margin: 12px 0;
  border: none;
  border-top: 1px solid rgba(145, 171, 189, 0.1);
}
.markdown-content table.markdown-table {
  width: 100%;
  border-collapse: collapse;
  margin: 0 0 8px 0;
  font-size: 12px;
  border: 1px solid rgba(145, 171, 189, 0.14);
  border-radius: 8px;
  overflow: hidden;
  background: rgba(8, 14, 22, 0.42);
  table-layout: auto;
}
.markdown-content table.markdown-table thead {
  background: rgba(147, 198, 208, 0.10);
}
.markdown-content table.markdown-table th {
  padding: 8px 12px;
  text-align: left;
  font-weight: 600;
  color: var(--ink);
  border-bottom: 1px solid rgba(145, 171, 189, 0.22);
  white-space: normal;
  word-break: break-word;
}
.markdown-content table.markdown-table td {
  padding: 6px 12px;
  border-bottom: 1px solid rgba(145, 171, 189, 0.08);
  color: var(--ink);
  white-space: normal;
  word-break: break-word;
  vertical-align: top;
}
.markdown-content table.markdown-table tr:last-child td {
  border-bottom: none;
}
.markdown-content table.markdown-table tbody tr:hover {
  background: rgba(147, 198, 208, 0.05);
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
    taskRefreshIntervalId: null,
    phaseExpandedByTask: {},
    filters: { agent: 'all', level: 'all' },
    openLogFilter: null,
    composerNote: '',
  };

  function $(sel) { return document.querySelector(sel); }
  function renderComposerNote(el, fallbackText = '') {
    if (!el) return;
    const text = String(state.composerNote || fallbackText || '').trim();
    el.textContent = text;
    el.style.display = text ? 'block' : 'none';
  }
  function clearComposerNote() {
    state.composerNote = '';
  }

  function ensureTaskRefreshLoop() {
    if (state.taskRefreshIntervalId) return;
    // Keep a low-rate full-list refresh running even when SSE appears healthy.
    // Long-lived browser tabs sometimes stop receiving task.completed/task.failed
    // events without surfacing an EventSource error, which leaves the list stuck
    // in "In Progress" until the user manually refreshes the page.
    state.taskRefreshIntervalId = setInterval(() => loadTasks(false), 5000);
  }

  function taskNeedsResume(task) {
    if (!task) return false;
    const kind = statusKindOf(task.statusState || task.status);
    if (kind === 'waiting') return true;
    const metadata = task.metadata || {};
    if (metadata && metadata._interrupt) return true;
    const history = Array.isArray(task.chatHistory) ? task.chatHistory : [];
    const last = history[history.length - 1];
    return !!(last && String(last.tone || '').toLowerCase() === 'input-required');
  }

  function resolveComposerMode(mode, targetTaskId) {
    const task = targetTaskId ? state.tasks[targetTaskId] : null;
    if ((mode === 'reply' || mode === 'resume') && taskNeedsResume(task)) return 'resume';
    return mode;
  }
  async function fetchJsonWithTimeout(url, options, timeoutMs = 15000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(url, { ...(options || {}), signal: controller.signal });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      return await response.json();
    } catch (error) {
      if (error && error.name === 'AbortError') {
        throw new Error(`Request timed out after ${timeoutMs}ms`);
      }
      throw error;
    } finally {
      clearTimeout(timer);
    }
  }
  function discardOptimisticTask(taskId) {
    if (!taskId) return;
    delete state.tasks[taskId];
    state.order = state.order.filter(id => id !== taskId);
    if (state.selectedTaskId === taskId) state.selectedTaskId = NEW_REQUEST_ID;
  }
  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/[&<>"']/g, c =>
      ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c]));
  }
  // Safe markdown → HTML renderer for the completion summary.
  // Order: extract code first → escape HTML → apply patterns → restore code.
  function renderMarkdown(text) {
    if (text === null || text === undefined) return '';
    const value = String(text);
    if (!value) return '';
    const codeBlocks = [];
    let html = value.replace(/```([a-zA-Z0-9_-]*)\n?([\s\S]*?)```/g, function (m, lang, code) {
      const idx = codeBlocks.length;
      codeBlocks.push({ lang: lang || '', code: code || '' });
      return 'ZQXCB' + idx + 'ZQX';
    });
    const inlineCodes = [];
    html = html.replace(/`([^`\n]+)`/g, function (m, code) {
      const idx = inlineCodes.length;
      inlineCodes.push(code);
      return 'ZQXIC' + idx + 'ZQX';
    });
    // Normalize literal escape sequences that arrive from upstream sources.
    // Some agents serialise their completion summary by JSON-encoding a
    // python string into another JSON envelope, so a real "\n" becomes the
    // two-character sequence backslash + n by the time it reaches the
    // browser ("Folder Organization Complete\n\n## Discovered Patterns…"
    // arrives as one unbroken line where the heading and list never start
    // at column 0).  Markdown grammar (headings, lists, paragraph splits,
    // blockquotes, tables) all anchor to real \n / line starts via the
    // ``m`` flag, so without this step those summaries render as a single
    // blob of plain text with "## Heading" and "- item" showing inline.
    // Code blocks and inline code were extracted into placeholders above,
    // so a legitimate literal "\n" inside ``print("hi\n")`` is preserved.
    html = html
      .replace(/\\r\\n/g, '\n')
      .replace(/\\n/g, '\n')
      .replace(/\\r/g, '\n')
      .replace(/\\t/g, '\t');
    html = esc(html);
    html = html.replace(/^[\s]*(?:[-]{3,}|[\*]{3,}|[_]{3,})[\s]*$/gm, '<hr>');
    html = html.replace(/^######\s+(.*)$/gm, '<h6>$1</h6>');
    html = html.replace(/^#####\s+(.*)$/gm, '<h5>$1</h5>');
    html = html.replace(/^####\s+(.*)$/gm, '<h4>$1</h4>');
    html = html.replace(/^###\s+(.*)$/gm, '<h3>$1</h3>');
    html = html.replace(/^##\s+(.*)$/gm, '<h2>$1</h2>');
    html = html.replace(/^#\s+(.*)$/gm, '<h1>$1</h1>');
    html = html.replace(/((?:^|\n)(?:&gt;\s*.*(?:\n|$))+)/g, function (m) {
      const inner = m.replace(/^&gt;\s*/gm, '').replace(/\n/g, '<br>').trim();
      return '\n<blockquote>' + inner + '</blockquote>\n';
    });
    html = html.replace(/(^|\n)((?:[-*+]\s+.+\n?)+)/g, function (m, prefix, list) {
      const items = list.trim().split('\n').map(function (line) {
        return '<li>' + line.replace(/^[-*+]\s+/, '') + '</li>';
      }).join('');
      return prefix + '<ul>' + items + '</ul>';
    });
    html = html.replace(/(^|\n)((?:\d+\.\s+.+\n?)+)/g, function (m, prefix, list) {
      const items = list.trim().split('\n').map(function (line) {
        return '<li>' + line.replace(/^\d+\.\s+/, '') + '</li>';
      }).join('');
      return prefix + '<ol>' + items + '</ol>';
    });
    html = html.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
    html = html.replace(/__([^_\n]+)__/g, '<strong>$1</strong>');
    html = html.replace(/(^|[^\*])\*([^*\n]+)\*/g, '$1<em>$2</em>');
    html = html.replace(/(^|[^_])_([^_\n]+)_/g, '$1<em>$2</em>');
    html = html.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');
    html = html.replace(/\[([^\]\n]+)\]\(([^)\s]+)(?:\s+"([^"]*)")?\)/g, function (m, text, url, title) {
      const safeUrl = sanitizeMarkdownUrl(url);
      const titleAttr = title ? ' title="' + esc(title) + '"' : '';
      return '<a href="' + safeUrl + '" target="_blank" rel="noopener noreferrer"' + titleAttr + '>' + text + '</a>';
    });
    html = html.replace(/(?<!["'=a-zA-Z>])(https?:\/\/[^\s<&]+)/g, function (m, url) {
      const safeUrl = sanitizeMarkdownUrl(url);
      return '<a href="' + safeUrl + '" target="_blank" rel="noopener noreferrer">' + url + '</a>';
    });
    // GitHub-flavored markdown tables: a header row, a separator row, and one
    // or more data rows.  Must be parsed BEFORE the paragraph splitter below
    // — otherwise each row would be wrapped in <p> and the `|---|` separator
    // would leak into the rendered text.  Cell contents are NOT re-escaped
    // here; the earlier ``html = esc(html);`` call already neutralized raw
    // HTML inside cells.  Inline-code / code-block placeholders
    // (``ZQXICnZQX`` / ``ZQXCBnZQX``) are still live and will be restored by
    // the placeholder replacers that follow.
    //
    // The regex is deliberately explicit about the four pieces (prefix, header
    // row, separator row, data rows) so the separator row is consumed by
    // group 3, not group 2, which would otherwise eat it via ``[^\n]*`` and
    // prevent the table from being detected.
    html = html.replace(
      /(^|\n)(\|[^\n]*\|[ \t]*\n)(\|[\s:|-]+\|[ \t]*\n)((?:\|[^\n]*\|[ \t]*\n?)+)/g,
      function (m, prefix, headerLine, sep, rows) {
        const headerCells = headerLine
          .replace(/\|[ \t]*\n?$/, '')
          .split('|')
          .slice(1)
          .map(function (c) { return c.trim(); });
        const dataLines = rows.split('\n').filter(function (l) { return l.trim(); });
        // parse alignment from the separator row
        const sepCells = sep
          .replace(/^\|/, '')
          .replace(/\|[ \t]*\n?$/, '')
          .split('|');
        const aligns = sepCells.map(function (c) {
          const t = c.trim();
          if (/^:-+:$/.test(t)) return 'center';
          if (/^-+:$/.test(t)) return 'right';
          if (/^:-+$/.test(t)) return 'left';
          return '';
        });
        let out = '<table class="markdown-table"><thead><tr>';
        headerCells.forEach(function (cell, i) {
          const align = aligns[i] ? ' style="text-align:' + aligns[i] + '"' : '';
          out += '<th' + align + '>' + cell + '</th>';
        });
        out += '</tr></thead><tbody>';
        dataLines.forEach(function (row) {
          const cells = row
            .replace(/\|[ \t]*$/, '')
            .split('|')
            .slice(1)
            .map(function (c) { return c.trim(); });
          out += '<tr>';
          cells.forEach(function (cell, i) {
            const align = aligns[i] ? ' style="text-align:' + aligns[i] + '"' : '';
            out += '<td' + align + '>' + cell + '</td>';
          });
          out += '</tr>';
        });
        out += '</tbody></table>';
        return prefix + out + '\n';
      }
    );
    html = html.replace(/ZQXCB(\d+)ZQX/g, function (m, idx) {
      const block = codeBlocks[Number(idx)];
      if (!block) return '';
      const langClass = block.lang ? ' class="language-' + esc(block.lang) + '"' : '';
      return '<pre><code' + langClass + '>' + esc(block.code) + '</code></pre>';
    });
    html = html.replace(/ZQXIC(\d+)ZQX/g, function (m, idx) {
      const code = inlineCodes[Number(idx)];
      if (code === undefined) return '';
      return '<code>' + esc(code) + '</code>';
    });
    const parts = html.split(/\n{2,}/);
    html = parts.map(function (part) {
      const trimmed = part.trim();
      if (!trimmed) return '';
      if (/^<(h[1-6]|ul|ol|blockquote|pre|hr|p|table|div|article|section)\b/.test(trimmed)) {
        return trimmed;
      }
      return '<p>' + trimmed.replace(/\n/g, '<br>') + '</p>';
    }).filter(Boolean).join('\n');
    return html;
  }
  function sanitizeMarkdownUrl(url) {
    const trimmed = String(url || '').trim();
    if (!trimmed) return '#';
    const first = trimmed.charAt(0);
    if (first === '#' || first === '/' || first === '?') {
      return esc(trimmed);
    }
    const protoMatch = trimmed.match(/^([a-zA-Z][a-zA-Z0-9+.-]*:)/);
    if (protoMatch) {
      const proto = protoMatch[1].toLowerCase();
      if (proto === 'http:' || proto === 'https:' || proto === 'mailto:') {
        return esc(trimmed);
      }
      return '#';
    }
    return esc(trimmed);
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
  function pickMarkForVisualState(visualState) {
    switch (String(visualState || '')) {
      case 'done': return '✓';
      case 'failed': return '✕';
      case 'warn': return '!';
      case 'current': return '●';
      case 'pending': return '○';
      case 'conditional_pending': return '◐';
      default: return '○';
    }
  }
  function renderTemplate(template, facts) {
    const text = String(template || '');
    const subs = (facts && typeof facts === 'object') ? facts : {};
    const safeValue = (v) => {
      if (v === null || v === undefined) return '--';
      const s = String(v);
      // Cap length to avoid unbounded blobs in the UI.
      return s.length > 200 ? s.slice(0, 197) + '...' : s;
    };
    // Escape any user-provided free text to prevent XSS through the template.
    const escapeHtml = (s) => String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
    return text.replace(/\{([a-zA-Z0-9_]+)\}/g, (_match, key) => escapeHtml(safeValue(subs[key])));
  }
  function pickPointerRow(task) {
    // Per design doc §0.6: active → failed → terminal → last.
    const rows = task.majorStepRows || {};
    const active = task.activeStepInstanceKey;
    const failed = task.failedStepInstanceKey;
    const terminal = task.terminalStepInstanceKey;
    const last = task.lastStepInstanceKey;
    if (active && rows[active]) return { key: active, row: rows[active] };
    if (failed && rows[failed]) return { key: failed, row: rows[failed] };
    if (terminal && rows[terminal]) return { key: terminal, row: rows[terminal] };
    if (last && rows[last]) return { key: last, row: rows[last] };
    return null;
  }
  function deriveMajorTimeline(task) {
    // Build a timeline from the structured v0.8 data. Returns ``null`` for
    // tasks that have no ``major_step_rows`` so the caller can fall back to
    // the generic progress-steps renderer.
    const rows = task.majorStepRows || {};
    if (!rows || Object.keys(rows).length === 0) {
      return null;  // signal to caller: use legacy bucketing
    }
    const skeleton = Array.isArray(task.majorStepsSkeleton) ? task.majorStepsSkeleton : [];
    const states = task.stepStates || {};
    const summaries = task.stepSummaries || {};

    // Build an ordered list: emitted rows (in their stored insertion order) +
    // any unfired conditional rows from the skeleton, dedup'd on
    // step_instance_key.
    const seen = new Set();
    const ordered = [];
    // Use the order from major_step_skeleton if present (preferred); otherwise
    // fall back to Object.keys order, which in modern JS preserves insertion.
    if (skeleton.length > 0) {
      for (const skel of skeleton) {
        const sik = skel.step_instance_key || skel.stepInstanceKey;
        if (!sik || seen.has(sik)) continue;
        seen.add(sik);
        const row = rows[sik];
        if (row) {
          ordered.push({
            key: sik,
            stepKey: row.step_key || skel.step_key,
            round: row.round || 0,
            title: row.title || skel.title || sik,
            agent: row.agent || skel.agent || '',
            visualState: row.visual_state || 'current',
            lifecycleState: row.lifecycle_state || '',
            conditional: !!(row.conditional || skel.conditional),
            startedAt: row.started_at || states[sik]?.started_at || '',
            endedAt: row.ended_at ?? states[sik]?.ended_at ?? null,
            summaryHtml: renderTemplate(row.summary_template || '', row.summary_facts || {}),
            ignored: !!row.ignored_after_terminal,
            fired: true,
          });
        } else {
          ordered.push({
            key: sik,
            stepKey: skel.step_key,
            round: skel.round || 0,
            title: skel.title || sik,
            agent: skel.agent || '',
            visualState: skel.conditional ? 'conditional_pending' : 'pending',
            lifecycleState: 'pending',
            conditional: !!skel.conditional,
            startedAt: '',
            endedAt: null,
            summaryHtml: '',
            ignored: false,
            fired: false,
          });
        }
      }
    } else {
      for (const [sik, row] of Object.entries(rows)) {
        if (seen.has(sik)) continue;
        seen.add(sik);
        ordered.push({
          key: sik,
          stepKey: row.step_key,
          round: row.round || 0,
          title: row.title || sik,
          agent: row.agent || '',
          visualState: row.visual_state || 'current',
          lifecycleState: row.lifecycle_state || '',
          conditional: !!row.conditional,
          startedAt: row.started_at || '',
          endedAt: row.ended_at ?? null,
          summaryHtml: renderTemplate(row.summary_template || '', row.summary_facts || {}),
          ignored: !!row.ignored_after_terminal,
          fired: true,
        });
      }
    }
    const normalizedOrdered = ordered.map((row) => {
      let visualState = row.visualState || 'pending';
      let lifecycleState = row.lifecycleState || '';
      const stepKey = String(row.stepKey || '');
      const sik = row.key;
      const isLegacyCompassReceived = stepKey === 'compass.received';
      const hasLaterFiredStep = ordered.some(candidate => candidate.key !== sik && candidate.fired && !candidate.ignored);
      // Pre-redesign tasks can leave ``compass.received`` stuck in ``running``
      // even though later rows already fired. Show it as completed so the
      // expanded timeline reflects the real historical sequence.
      if (
        isLegacyCompassReceived
        && row.fired
        && hasLaterFiredStep
        && (visualState === 'current' || lifecycleState === 'running')
      ) {
        visualState = 'done';
        lifecycleState = 'done';
      }
      return { ...row, visualState, lifecycleState };
    });
    return {
      currentKey: (pickPointerRow(task) || {}).key || '',
      ordered: normalizedOrdered.filter(row => !row.ignored),
    };
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
        // Naive timestamp (no Z / no offset). All Python emitters in the
        // system produce UTC (task_store, lifecycle, a2a client, compass
        // chat entries, devlog), and the container clock defaults to UTC.
        // Treating the value as UTC here keeps the displayed local time
        // correct for users in any timezone. Without this, devlog's
        // "YYYY-MM-DD HH:MM:SS" lines (e.g. "2026-06-01 12:34:56") would
        // be re-interpreted as the browser's local clock and leak UTC.
        return new Date(
          Date.UTC(
            Number(year),
            Number(month) - 1,
            Number(day),
            Number(hour),
            Number(minute),
            Number(second),
            milliseconds,
          )
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

  function replaceTaskCollection(tasks) {
    state.tasks = {};
    state.order = [];
    for (const t of (tasks || [])) upsertTask(t);
  }

  function createOptimisticTask(text) {
    const optimisticId = '__optimistic_' + Date.now().toString(36) + '_' + Math.random().toString(36).slice(2, 8);
    const ts = new Date().toISOString();
    return {
      task_id: optimisticId,
      id: optimisticId,
      userRequest: text,
      summary: 'Submitting…',
      status: 'active',
      statusState: 'TASK_STATE_SUBMITTED',
      createdAt: ts,
      taskType: 'general',
      optimistic: true,
      chatHistory: [{ role: 'USER', text, ts, tone: 'normal' }],
    };
  }

  function promoteOptimisticTask(optimisticId, newId) {
    if (!optimisticId || !newId) return;
    if (optimisticId === newId) return;
    const existed = Object.prototype.hasOwnProperty.call(state.tasks, optimisticId);
    if (existed) {
      delete state.tasks[optimisticId];
      state.order = state.order.filter(id => id !== optimisticId);
      if (state.selectedTaskId === optimisticId) state.selectedTaskId = newId;
    }
  }

  async function loadTasks(autoSelect) {
    try {
      const resp = await fetch('/api/tasks');
      const data = await resp.json();
      const previousSelectedId = state.selectedTaskId;
      replaceTaskCollection(data.tasks || []);
      // Auto-refresh MUST NOT change which task the user is looking at.
      // Earlier versions hijacked the selection to a waiting task whenever
      // the user was on the New Request composer, which silently stole
      // focus the moment a user clicked "New Request" and started typing.
      // Users now navigate to a waiting task explicitly by clicking the
      // "Waiting for Input" dashboard card (see selectLatestWaitingTask).
      // The only auto-adjustment we still make is rescuing the selection
      // when the previously-selected task has been removed from the list.
      if (autoSelect && !state.tasks[state.selectedTaskId] && !isOverviewSelection(state.selectedTaskId) && state.selectedTaskId !== NEW_REQUEST_ID) {
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
      clearComposerNote();
      state.selectedTaskId = OVERVIEW_ID;
      renderTaskList(); renderChat(); renderDetail();
      return;
    }
    clearComposerNote();
    state.selectedTaskId = tid;
    renderTaskList(); renderChat(); renderDetail();
    if (tid !== NEW_REQUEST_ID && !isOverviewSelection(tid)) subscribeLogs(tid);
    loadTaskDetail(tid).then(() => {
      renderTaskList(); renderChat(); renderDetail();
    });
  }

  // Explicit jump from the dashboard "Waiting for Input" card to the most
  // recent task that is still waiting for the user. Auto-refresh no longer
  // moves selection on its own (it would steal focus from someone typing
  // a new request); this handler is the user-initiated way to reach a
  // waiting task quickly. No-op if no waiting task exists.
  function selectLatestWaitingTask() {
    const waitingId = orderedTaskIds().find(id => {
      const t = state.tasks[id];
      return t && statusKindOf(t.statusState || t.status) === 'waiting';
    });
    if (!waitingId) return;
    if (waitingId === state.selectedTaskId) return;
    clearComposerNote();
    state.selectedTaskId = waitingId;
    renderTaskList(); renderChat(); renderDetail();
    subscribeLogs(waitingId);
    loadTaskDetail(waitingId).then(() => {
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
      renderComposerNote(composerNote);
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

    if (t.optimistic === true) {
      renderComposerNote(composerNote, 'Request submission in progress. Please wait for Compass to respond.');
      composerInput.disabled = true;
      composerSend.disabled = true;
      composerSend.textContent = 'Send';
      composerInput.placeholder = 'Submitting request...';
      composerInput.dataset.mode = 'disabled';
      composerInput.dataset.targetTaskId = '';
      return;
    }

    const kind = statusKindOf(t.statusState || t.status);
    const isWaiting = kind === 'waiting';
    composerInput.disabled = false;
    composerSend.textContent = 'Send';
    if (isWaiting) {
      renderComposerNote(composerNote);
      composerInput.placeholder = `Reply to ${tid}...`;
      composerSend.disabled = false;
      composerInput.dataset.mode = 'resume';
      composerInput.dataset.targetTaskId = tid;
    } else {
      renderComposerNote(composerNote);
      const terminal = (kind === 'completed' || kind === 'failed');
      composerInput.disabled = terminal;
      composerSend.disabled = terminal;
      composerInput.placeholder = terminal ? 'This task is closed. Select New Request to start a new one.' : `Reply to ${tid}...`;
      composerInput.dataset.mode = terminal ? 'disabled' : 'reply';
      composerInput.dataset.targetTaskId = tid;
    }
  }

  function timelineAgentDetail(agent, detail, fallbackDetail = '') {
    const owner = String(agent || '').trim();
    const text = String(detail || '').trim() || String(fallbackDetail || '').trim();
    if (owner && text) return `${owner}: ${text}`;
    return text || owner;
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
  // Compact duration formatter for the v0.8 timeline right column. Always
  // emits ``Xh Ym Zs`` even when X or Y is 0, so the column stays aligned
  // (e.g. ``0m 02s`` not ``2s``). Per design doc §2.1 / §8.1.
  function compactDuration(ms) {
    if (ms === null || ms === undefined || Number.isNaN(ms)) return '--';
    const totalSeconds = Math.max(0, Math.floor(ms / 1000));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    const hh = String(hours);
    const mm = String(minutes).padStart(2, '0');
    const ss = String(seconds).padStart(2, '0');
    return `${hh}h ${mm}m ${ss}s`;
  }
  // Format a UTC ISO timestamp as ``MM-DD HH:MM:SS`` in the viewer's local
  // timezone. Returns ``'--'`` for missing or unparseable input.
  function compactStartTime(iso) {
    if (!iso) return '--';
    const d = parseTimestamp(iso);
    if (!d || Number.isNaN(d.getTime())) return '--';
    const pad = (n) => String(n).padStart(2, '0');
    const month = pad(d.getMonth() + 1);
    const day = pad(d.getDate());
    const hours = pad(d.getHours());
    const minutes = pad(d.getMinutes());
    const seconds = pad(d.getSeconds());
    return `${month}-${day} ${hours}:${minutes}:${seconds}`;
  }
  function timelineHtmlForMajorSteps(task, timeline, currentStep, kind, expanded) {
    const ordered = (timeline && timeline.ordered) || [];
    if (!ordered.length) {
      return '<div class="empty-state">No workflow steps yet.</div>';
    }
    // In collapsed (current-only) mode, prefer the pointer row; fall back to
    // the first row in the ordered list when no pointer is set.
    const visibleRows = expanded
      ? ordered
      : (() => {
          const pointer = timeline.currentKey;
          const target = pointer ? ordered.find(r => r.key === pointer) : null;
          return target ? [target] : [ordered[ordered.length - 1]];
        })();
    const rows = visibleRows.map((row) => {
      const visualClass = String(row.visualState || 'pending');
      const mark = pickMarkForVisualState(visualClass);
      const isFocusedRow = !expanded || (row.fired && visualClass === 'current');
      // Right-hand column: ``MM-DD HH:MM:SS  Xh Ym Zs``.
      let startLabel;
      let durationLabel;
      if (!row.fired) {
        // Unfired conditional row: literal "Not started yet" per §8.1.
        startLabel = 'Not started yet';
        durationLabel = '--';
      } else {
        startLabel = compactStartTime(row.startedAt);
        const endDate = row.endedAt
          ? parseTimestamp(row.endedAt)
          : (visualClass === 'current' || visualClass === 'warn' ? null : parseTimestamp(task.updatedAt || ''));
        const startDate = row.startedAt ? parseTimestamp(row.startedAt) : null;
        const end = endDate ? endDate.getTime() : Date.now();
        const startMs = startDate ? startDate.getTime() : 0;
        durationLabel = compactDuration(startMs && end ? end - startMs : null);
      }
      const ownerAgent = row.agent || ownerOf(task);
      const summaryLine = row.summaryHtml
        ? `<div class="timeline-summary">${esc(ownerAgent)}: ${row.summaryHtml}</div>`
        : `<div class="timeline-summary">${esc(ownerAgent)}</div>`;
      return `<div class="timeline-row ${escapeAttr(visualClass)}${isFocusedRow ? ' current' : ''}">
        <div class="timeline-mark">${mark}</div>
        <div class="timeline-headline">
          <div class="timeline-title">${esc(row.title)}</div>
          <div class="timeline-facts">
            <span class="timeline-fact"><span class="timeline-fact-label">Started</span>${esc(startLabel)}</span>
            <span class="timeline-fact"><span class="timeline-fact-label">Time Spent</span>${esc(durationLabel)}</span>
          </div>
        </div>
        <div class="timeline-meta">${summaryLine}</div>
      </div>`;
    }).join('');
    return `<div class="timeline-list${expanded ? '' : ' collapsed'}">${rows}</div>`;
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
    // Optimistic placeholders are pre-snapshot stand-ins for a task that has
    // not been observed on the server yet.  Never surface their internal
    // ``__optimistic_*`` id — show a friendly pending label instead so the
    // detail panel does not look like the user is looking at a real task.
    const isOptimisticPlaceholder = t.optimistic === true;
    const kind = statusKindOf(t.statusState || t.status);
    const steps = mergedProgressSignals(t);
    const currentStep = t.currentMajorStep || currentStepFromLogs(t) || (steps.length ? steps[steps.length-1].text : '');
    const majorTimeline = deriveMajorTimeline(t);
    const phaseExpanded = !!state.phaseExpandedByTask[tid];
    const taskType = taskTypeOf(t);
    const typeLabel = taskTypeLabel(t);
    const hasTimelineToggle = majorTimeline
      ? majorTimeline.ordered.length > 1
      : (steps.length > 1);
    const timelineBody = majorTimeline
      ? timelineHtmlForMajorSteps(t, majorTimeline, currentStep, kind, phaseExpanded)
      : timelineHtmlForGeneric(t, steps, kind, phaseExpanded);
    const orchestratorTaskId = String(t.orchestratorTaskId || t.task_id || t.id || '').trim();
    const originalRequest = String(t.userRequest || t.user_request || '').trim();
    const detailTitle = originalRequest || tid;
    const detailTitleLabel = originalRequest ? 'Original Request' : 'Task';
    const outcome = summarizeTaskOutcome(t, kind, currentStep);
    if (taskInfoHeadTaskId) {
      // Suppress the raw ``__optimistic_*`` id so users never see internal
      // placeholder state — show "Submitting…" until the real task lands.
      taskInfoHeadTaskId.textContent = isOptimisticPlaceholder ? 'Submitting…' : orchestratorTaskId;
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
          <div class="detail-value multiline markdown-content">${renderMarkdown(outcome.text)}</div>
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
    ensureTaskRefreshLoop();
    try {
      const es = new EventSource('/ui/events');
      es.addEventListener('task.snapshot', ev => {
        try {
          const data = JSON.parse(ev.data);
          replaceTaskCollection(data.tasks || []);
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
    } catch (e) { /* fallback loop already armed */ }
  }

  // Short-poll a single task until it reaches a terminal state or the
  // deadline elapses.  Used immediately after a resume POST so the UI flips
  // from "In Progress" to "Completed" / "Failed" without waiting for the
  // 5-second SSE fallback.  Maximum total wait is 240s — beyond that the
  // SSE / fallback polling will pick up the change anyway.
  async function pollTaskUntilTerminal(tid, opts) {
    const maxMs = (opts && opts.maxMs) || 240000;
    const intervalMs = (opts && opts.intervalMs) || 1500;
    const deadline = Date.now() + maxMs;
    let ticks = 0;
    while (Date.now() < deadline) {
      // loadTaskDetail reuses the existing single-task fetch (no full list
      // re-render) and writes into the same state.tasks map.
      await loadTaskDetail(tid);
      ticks++;
      const t = state.tasks[tid];
      const kind = statusKindOf(t && (t.statusState || t.status));
      if (kind === 'completed' || kind === 'failed') {
        return { reachedTerminal: true, kind, ticks };
      }
      await new Promise(r => setTimeout(r, intervalMs));
    }
    return { reachedTerminal: false, ticks };
  }

  async function sendComposer() {
    const input = $('#composer-input');
    const text = (input.value || '').trim();
    if (!text) return;
    const mode = input.dataset.mode || 'create';
    const targetTaskId = input.dataset.targetTaskId || '';
    const effectiveMode = resolveComposerMode(mode, targetTaskId);
    if (effectiveMode === 'create') {
      input.value = '';
      clearComposerNote();
      const optimisticTask = createOptimisticTask(text);
      const optimisticId = optimisticTask.task_id;
      state.tasks[optimisticTask.task_id] = optimisticTask;
      if (!state.order.includes(optimisticTask.task_id)) state.order.unshift(optimisticTask.task_id);
      state.selectedTaskId = optimisticTask.task_id;
      renderTaskList(); renderChat(); renderDetail();
      let data;
      try {
        data = await fetchJsonWithTimeout('/message:send', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            message: { role: 'ROLE_USER', parts: [{ text }] },
            configuration: { returnImmediately: true },
          }),
        });
      } catch (e) {
        discardOptimisticTask(optimisticId);
        state.selectedTaskId = NEW_REQUEST_ID;
        state.composerNote = 'Compass did not acknowledge the request. Please retry.';
        renderTaskList(); renderChat(); renderDetail();
        return;
      }
      const newId = (data.task && data.task.id) || (data.ui_update && data.ui_update.task_id);
      if (newId) promoteOptimisticTask(optimisticTask.task_id, newId);
      // Pull the latest snapshot so the optimistic placeholder is replaced with
      // the real task before the user sees the final list state.
      try {
        const snapData = await fetchJsonWithTimeout('/api/tasks', {}, 5000);
        replaceTaskCollection(snapData.tasks || []);
      } catch (e) { /* fall through to loadTasks */ }
      // Always land on the real task id once we have one — even if the
      // snapshot is briefly behind.  Falling back to the placeholder leaves
      // the user staring at ``__optimistic_xxx`` in the detail panel.
      if (newId) {
        if (!state.tasks[newId]) await loadTasks(false);
        state.selectedTaskId = newId;
        if (state.tasks[newId]) subscribeLogs(newId);
      } else {
        await loadTasks(false);
      }
      // Safety net: if the snapshot wiped out the real task too (e.g. server
      // hasn't indexed it yet) but the placeholder is still hanging around,
      // remove it so the UI does not display a stale ``__optimistic_*`` id.
      if (
        state.selectedTaskId !== optimisticId
        && Object.keys(state.tasks).some(id => id.startsWith('__optimistic_'))
      ) {
        for (const id of Object.keys(state.tasks)) {
          if (id.startsWith('__optimistic_')) delete state.tasks[id];
        }
        state.order = state.order.filter(id => !id.startsWith('__optimistic_'));
      }
      renderTaskList(); renderChat(); renderDetail();
    } else if (effectiveMode === 'resume' && targetTaskId) {
      input.value = '';
      clearComposerNote();
      const targetTask = state.tasks[targetTaskId];
      if (targetTask) {
        // Optimistically flip the task-status badge to "In Progress" so the
        // user sees immediate feedback while the resume POST is in flight.
        //
        // We deliberately do NOT push a USER bubble optimistically.  The
        // server-side ``resume_task`` (compass/agent.py) records the resume
        // value as a USER entry in the task's ``chat_history`` metadata via
        // ``_append_chat_entry``, and the ``loadTasks(false)`` call below
        // pulls that server-authoritative history into the client state.
        // Pushing a bubble here would render the user's "workspace" (or
        // similar) reply twice for one render tick: once from the local
        // optimistic push, and once again when ``loadTasks`` is followed
        // by a re-push — the previous re-push de-dup used a strict
        // timestamp-equality check that always failed (client and server
        // timestamps are produced independently), so the duplicate was
        // guaranteed to survive until the next ``loadTaskDetail`` tick
        // cleaned it up.  The resume response is fire-and-forget (returns
        // in < 1s) so the user-perceived delay of waiting for the server
        // snapshot to land is negligible.
        targetTask.statusState = 'TASK_STATE_WORKING';
        targetTask.status = 'active';
        targetTask.summary = 'Office task dispatching in background';
        targetTask.currentMajorStep = 'Office task dispatching in background';
        renderTaskList(); renderChat(); renderDetail();
      }
      try {
        await fetchJsonWithTimeout(`/tasks/${encodeURIComponent(targetTaskId)}/resume`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ input: text }),
        });
      } catch (e) {
        if (targetTask) {
          targetTask.statusState = 'TASK_STATE_INPUT_REQUIRED';
          targetTask.status = 'waiting';
          targetTask.summary = 'Waiting for output mode selection';
          targetTask.currentMajorStep = 'Waiting for output mode selection';
        }
        input.value = text;
        state.composerNote = 'Failed to send reply to Compass. Please retry.';
        renderTaskList(); renderChat(); renderDetail();
        return;
      }
      // ``loadTasks(false)`` is awaited so the server-side ``chat_history``
      // (which now includes the resume value as a USER entry) is in place
      // before we re-render.  We do NOT re-push the optimistic message —
      // the server's record is the authoritative copy and would be a
      // duplicate if added again.
      try {
        await loadTasks(false);
      } catch (e) {
        try { await loadTaskDetail(targetTaskId); } catch (_ignored) {}
      }
      state.selectedTaskId = targetTaskId;
      renderTaskList(); renderChat(); renderDetail();
      // Fire-and-forget fast-poll: the resume POST returns almost immediately
      // (compass no longer blocks on the office roundtrip), but the actual
      // office work still takes seconds-to-minutes to finalize the task
      // state.  Poll this single task every 1.5s for up to 4 minutes and
      // re-render the chat / detail pane as soon as it lands.  Without this,
      // the user sees a stale "In Progress" until the 5s SSE fallback ticks.
      pollTaskUntilTerminal(targetTaskId).then(async (result) => {
        if (!result || !result.reachedTerminal) {
          // Fell off the deadline; SSE / 5s fallback will pick up the change.
          return;
        }
        // Pull the full task list so the task-list badge updates too, not
        // just the open chat pane.
        try { await loadTasks(false); } catch (e) { /* fall through */ }
        renderTaskList(); renderChat(); renderDetail();
      });
    } else if (effectiveMode === 'reply' && targetTaskId) {
      state.composerNote = 'Compass is not waiting for input on this task right now.';
      renderTaskList(); renderChat(); renderDetail();
    }
  }

  document.addEventListener('DOMContentLoaded', () => {
    document.addEventListener('click', event => {
      if (!event.target.closest('.log-filter') && state.openLogFilter) {
        state.openLogFilter = null;
        renderLogs();
      }
    });
    const dashboardEl = $('#dashboard');
    if (dashboardEl) {
      dashboardEl.addEventListener('click', event => {
        const waitingCard = event.target.closest('.dashboard-card.waiting');
        if (waitingCard) {
          selectLatestWaitingTask();
        }
      });
    }
    $('#composer-send').addEventListener('click', sendComposer);
    // Send on Enter; allow Shift+Enter for a newline inside the composer.
    // The previous implementation required Cmd/Ctrl+Enter which silently
    // dropped plain-Enter submissions — users who typed a one-word reply
    // like "workspace" and pressed Enter thought the message had been
    // sent but the request never left the browser, so the task stayed
    // stuck in TASK_STATE_INPUT_REQUIRED. Plain Enter (or the Send
    // button) is now the universal send gesture.
    $('#composer-input').addEventListener('keydown', ev => {
      if (ev.key === 'Enter' && !ev.shiftKey) {
        ev.preventDefault();
        sendComposer();
      }
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
