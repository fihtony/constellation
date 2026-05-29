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
        f'<div class="task-title">{safe_id}</div>'
        f'<div class="task-tag-row">'
        f'<span class="status-pill {safe_status}">{safe_status_text}</span>'
        f'</div>'
        f'<div class="task-note">{safe_summary}</div>'
        f'</div>'
    )


_INLINE_CSS = """
:root {
  --bg: #081018;
  --bg-accent: #0d1723;
  --panel: rgba(13, 23, 35, 0.88);
  --panel-strong: #101b29;
  --ink: #e6f0f7;
  --muted: #93a6b6;
  --line: rgba(154, 176, 196, 0.16);
  --accent: #33d1ff;
  --accent-soft: rgba(51, 209, 255, 0.12);
  --ok-bg: rgba(22, 163, 74, 0.16);
  --ok-ink: #63e6a5;
  --wait-bg: rgba(245, 158, 11, 0.14);
  --wait-ink: #ffd36f;
  --progress-bg: rgba(51, 209, 255, 0.14);
  --progress-ink: #7adeff;
  --error-bg: rgba(239, 68, 68, 0.16);
  --error-ink: #ff9191;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  font-family: "IBM Plex Sans", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", sans-serif;
  color: var(--ink);
  background:
    radial-gradient(circle at top left, rgba(51,209,255,0.16), transparent 28%),
    radial-gradient(circle at right 20%, rgba(125,89,255,0.12), transparent 24%),
    linear-gradient(180deg, var(--bg), var(--bg-accent));
  overflow: hidden;
}
.workspace {
  display: grid;
  grid-template-columns: minmax(280px, 1.6fr) minmax(420px, 2.2fr) minmax(520px, 3.2fr);
  gap: 16px;
  padding: 16px;
  height: 100vh;
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
.panel-body {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
}
#task-list-panel .panel-body { padding: 12px; overflow-y: auto; }
.task-list { display: flex; flex-direction: column; gap: 10px; }
.task-item {
  position: relative;
  padding: 12px;
  border-radius: 14px;
  border: 1px solid var(--line);
  background: rgba(16, 27, 41, 0.96);
  cursor: pointer;
  transition: border-color 0.15s ease;
}
.task-item:hover { border-color: rgba(51,209,255,0.3); }
.task-item.active {
  border-color: rgba(51,209,255,0.55);
  box-shadow: inset 3px 0 0 var(--accent);
}
.task-item.new-request { background: rgba(51,209,255,0.06); border-color: rgba(51,209,255,0.25); }
.task-title { font-size: 12px; font-weight: 700; letter-spacing: 0.02em; }
.task-note { margin-top: 6px; font-size: 11px; line-height: 1.55; color: var(--muted); }
.task-tag-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
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
  background: #132131;
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
.bubble.user { margin-left: auto; background: var(--accent); border-color: transparent; color: #06121b; }
.bubble.user .bubble-label { color: #042330; }
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
#task-info-panel .panel-body { padding: 14px; overflow-y: auto; }
.detail-stack { display: flex; flex-direction: column; gap: 12px; }
.detail-card {
  padding: 14px;
  border-radius: 16px;
  border: 1px solid var(--line);
  background: rgba(16, 27, 41, 0.96);
}
.detail-card h4 { margin-top: 8px; font-size: 18px; letter-spacing: -0.01em; }
.meta-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 12px;
}
.meta {
  padding: 8px 10px;
  border-radius: 10px;
  background: rgba(51,209,255,0.06);
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
  padding: 10px 12px;
  border-radius: 12px;
  border: 1px solid rgba(154,176,196,0.1);
  background: rgba(17,30,45,0.96);
  font-size: 12px;
}
.step.current { background: var(--accent-soft); border-left: 4px solid var(--accent); }
.step-index {
  width: 24px; height: 24px;
  border-radius: 50%;
  background: var(--accent);
  color: #06121b;
  font-size: 11px; font-weight: 700;
  display: flex; align-items: center; justify-content: center;
}
.step-meta { color: var(--muted); font-size: 11px; margin-top: 4px; }

/* Logs */
.log-filters {
  display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; align-items: center;
}
.log-filters select, .log-filters label {
  padding: 6px 10px;
  border-radius: 10px;
  border: 1px solid var(--line);
  background: #101b29;
  color: var(--ink);
  font-size: 11px;
  font-family: inherit;
}
.log-box {
  margin-top: 10px;
  border-radius: 12px;
  border: 1px solid rgba(255,255,255,0.08);
  background: #112029;
  max-height: 320px;
  overflow-y: auto;
}
.log-line {
  display: grid;
  grid-template-columns: 140px 60px 100px 1fr;
  gap: 10px;
  padding: 7px 12px;
  border-bottom: 1px solid rgba(255,255,255,0.06);
  font-family: "IBM Plex Mono", "SFMono-Regular", monospace;
  font-size: 11px;
  line-height: 1.5;
  color: #d7e5ec;
}
.log-line:last-child { border-bottom: none; }
.log-line .log-level.ERROR { color: var(--error-ink); }
.log-line .log-level.WARN  { color: var(--wait-ink); }
.log-line .log-level.INFO  { color: var(--progress-ink); }
.log-line .log-level.DEBUG { color: var(--muted); }
.empty-state {
  padding: 22px;
  color: var(--muted);
  font-size: 13px;
  text-align: center;
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
  function fmtTime(iso) {
    if (!iso) return '';
    try { const d = new Date(iso); if (isNaN(d.getTime())) return iso; return d.toLocaleString(); }
    catch { return iso; }
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
      renderTaskList(); renderChat(); renderDetail();
      if (state.selectedTaskId !== NEW_REQUEST_ID) subscribeLogs(state.selectedTaskId);
    } catch (e) { console.error('loadTasks failed', e); }
  }

  function renderTaskList() {
    const root = $('#task-list');
    let html = '';
    const active = state.selectedTaskId === NEW_REQUEST_ID ? ' active' : '';
    html += `<div class="task-item new-request${active}" data-task-id="${NEW_REQUEST_ID}">
      <div class="task-title">New Request</div>
      <div class="task-note">Start a new Compass task here.</div>
    </div>`;
    for (const tid of state.order) {
      const t = state.tasks[tid]; if (!t) continue;
      const kind = statusKindOf(t.statusState || t.status);
      const isActive = tid === state.selectedTaskId;
      const waitBadge = kind === 'waiting' ? '<span class="wait-badge"></span>' : '';
      html += `<div class="task-item${isActive?' active':''}" data-task-id="${esc(tid)}" data-status="${esc(kind)}">
        ${waitBadge}
        <div class="task-title">${esc(tid)}</div>
        <div class="task-tag-row"><span class="status-pill ${esc(kind)}">${esc(statusLabel(kind))}</span></div>
        <div class="task-note">${esc(t.summary || t.userRequest || '')}</div>
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
  }

  function renderChat() {
    const tid = state.selectedTaskId;
    const head = $('#chat-head-sub');
    const scroll = $('#chat-scroll');
    const composerInput = $('#composer-input');
    const composerSend  = $('#composer-send');
    const composerNote  = $('#composer-note');

    if (tid === NEW_REQUEST_ID) {
      head.textContent = 'New Request';
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

    let stepsHtml = '';
    if (steps.length) {
      stepsHtml = steps.map((s, i) => {
        const cur = (i === steps.length - 1) ? 'current' : '';
        return `<div class="step ${cur}">
          <div class="step-index">${i+1}</div>
          <div>
            <div>${esc(s.text || s.step || '')}</div>
            <div class="step-meta">${esc(s.agent || '')} · ${esc(s.ts || '')}</div>
          </div>
        </div>`;
      }).join('');
    }

    root.innerHTML = `
      <div class="detail-card">
        <span class="status-pill ${esc(kind)}">${esc(statusLabel(kind))}</span>
        <h4>${esc(t.summary || t.userRequest || tid)}</h4>
        <p style="margin-top:8px; font-size:13px; line-height:1.7; color:var(--muted);">
          ${esc(t.userRequest || '')}
        </p>
        <div class="meta-grid">
          <div class="meta"><span class="meta-label">Task ID</span><div class="meta-value">${esc(t.task_id)}</div></div>
          <div class="meta"><span class="meta-label">Type</span><div class="meta-value">${esc(t.taskType || 'general')}</div></div>
          <div class="meta"><span class="meta-label">Started At</span><div class="meta-value">${esc(fmtTime(t.createdAt))}</div></div>
          <div class="meta"><span class="meta-label">Elapsed</span><div class="meta-value">${esc(elapsedMs(t.createdAt, t.updatedAt))}</div></div>
        </div>
      </div>
      <div class="detail-card">
        <div class="kicker">Current Major Step</div>
        <p style="margin-top:8px; font-size:14px; line-height:1.6;">${esc(currentStep || 'Waiting for progress...')}</p>
        <div class="steps">${stepsHtml}</div>
      </div>
      <div class="detail-card" id="logs-card">
        <div class="kicker">Merged Logs</div>
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
        <div class="log-box" id="log-box"></div>
      </div>`;

    $('#filter-agent').value = state.filters.agent;
    $('#filter-level').value = state.filters.level;
    $('#filter-agent').addEventListener('change', e => { state.filters.agent = e.target.value; renderLogs(); });
    $('#filter-level').addEventListener('change', e => { state.filters.level = e.target.value; renderLogs(); });
    renderLogs();
  }

  const LEVEL_ORDER = { DEBUG: 10, INFO: 20, WARN: 30, ERROR: 40 };
  function renderLogs() {
    const tid = state.selectedTaskId;
    const box = $('#log-box'); if (!box) return;
    const logs = state.logsByTask[tid] || [];
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
    if (!visible.length) {
      box.innerHTML = `<div class="empty-state">No logs yet for this filter.</div>`;
      return;
    }
    box.innerHTML = visible.map(l => `<div class="log-line">
      <span class="log-ts">${esc(l.timestamp || '')}</span>
      <span class="log-level ${esc((l.level||'').toUpperCase())}">${esc((l.level||'').toUpperCase())}</span>
      <span class="log-agent">${esc(l.agent || '')}</span>
      <span class="log-msg">${esc(l.message || '')}</span>
    </div>`).join('');
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
      </div>
      <div class="panel-body">
        <div class="task-list" id="task-list">{server_task_list}</div>
      </div>
    </aside>

    <section class="panel" id="task-chat-panel">
      <div class="panel-head">
        <div class="kicker">Task Chat</div>
        <h3 id="chat-head-sub">New Request</h3>
      </div>
      <div class="panel-body">
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
      </div>
      <div class="panel-body">
        <div class="detail-stack" id="detail-stack">
          <div class="empty-state">Select a task to view details, or send a new request.</div>
        </div>
      </div>
    </section>
  </div>
  <script>{_INLINE_JS}</script>
</body>
</html>
"""
