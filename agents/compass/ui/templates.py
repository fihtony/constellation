"""Compass UI templates."""
from __future__ import annotations


def render_chat_message(role: str, text: str, style: str = "normal") -> str:
    """Render a chat message bubble."""
    style_class = {
        "normal": "",
        "input-required": "input-required",
        "failed": "failed",
        "completed": "completed",
    }.get(style, "")

    label_class = {
        "normal": "agent",
        "input-required": "agent input-required",
        "failed": "agent failed",
        "completed": "agent completed",
    }.get(style, "agent")

    return f'''
    <div class="message {label_class}">
        <div class="message-label">{role}</div>
        <div>{text}</div>
    </div>
    '''


def render_task_tab(task_id: str, status: str, summary: str = "") -> str:
    """Render a task tab."""
    status_config = {
        "failed": {"class": "failed", "icon": "X", "label": "Failed"},
        "waiting": {"class": "waiting", "icon": "?", "label": "Needs input"},
        "active": {"class": "active", "icon": "-", "label": ""},
        "completed": {"class": "completed", "icon": "OK", "label": summary},
    }.get(status, {"class": "active", "icon": "-", "label": ""})

    label_html = ""
    if status_config["label"]:
        label_html = '<span style="font-size: 10px; margin-top: 2px;">' + status_config["label"] + '</span>'

    return '''
    <div class="task-tab {cls}" data-task-id="{task_id}">
        <span style="display: flex; align-items: center; gap: 6px;">
            <span>{task_id}</span>
            <span style="font-size: 10px;">{icon}</span>
        </span>
        {label_html}
    </div>
    '''.format(
        cls=status_config["class"],
        task_id=task_id,
        icon=status_config["icon"],
        label_html=label_html
    )


def render_compass_ui(messages: list[dict], tasks: list[dict], selected_task_id: str = None) -> str:
    """Render the complete Compass UI HTML."""
    messages_html = "\n".join(
        render_chat_message(m["role"], m["text"], m.get("style", "normal"))
        for m in messages
    )

    tasks_html = "\n".join(
        render_task_tab(t["task_id"], t["status"], t.get("summary", ""))
        for t in tasks
    )

    return f'''
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <title>Compass UI</title>
    <style>
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #1a1a2e; color: #eee; padding: 20px; }}
        .main-layout {{ display: flex; gap: 20px; height: calc(100vh - 40px); }}
        .chat-section {{ flex: 1; background: #16213e; border-radius: 12px; padding: 20px; display: flex; flex-direction: column; overflow: hidden; }}
        .task-section {{ flex: 1; background: #16213e; border-radius: 12px; padding: 20px; overflow: hidden; display: flex; flex-direction: column; }}
        .chat-header {{ padding: 12px 16px; border-bottom: 1px solid rgba(255,255,255,0.1); display: flex; align-items: center; gap: 12px; }}
        .agent-name {{ font-size: 16px; font-weight: 600; color: #00d4ff; }}
        .agent-role {{ font-size: 12px; color: #888; }}
        .chat-messages {{ flex: 1; overflow-y: auto; padding: 16px; display: flex; flex-direction: column; gap: 12px; }}
        .input-area {{ padding: 12px; border-top: 1px solid rgba(255,255,255,0.1); display: flex; gap: 8px; }}
        #message-input {{ flex: 1; padding: 10px 14px; border-radius: 8px; border: 1px solid rgba(255,255,255,0.2); background: #0f3460; color: #eee; font-size: 14px; outline: none; }}
        #message-input:focus {{ border-color: #00d4ff; }}
        #send-btn {{ padding: 10px 20px; border-radius: 8px; border: none; background: #00d4ff; color: #1a1a2e; font-weight: 600; cursor: pointer; }}
        #send-btn:hover {{ background: #00b8e6; }}
        .section-title {{ font-size: 18px; font-weight: 600; padding: 12px 16px; border-bottom: 1px solid rgba(255,255,255,0.1); }}
        .task-tabs {{ display: flex; flex-direction: column; gap: 8px; padding: 12px; overflow-y: auto; }}
        .task-tab {{ padding: 12px 16px; border-radius: 8px; background: #0f3460; cursor: pointer; transition: all 0.2s; }}
        .task-tab:hover {{ background: #1a4a7a; }}
        .task-tab.active {{ border-left: 3px solid #00d4ff; }}
        .task-tab.completed {{ border-left: 3px solid #10b981; }}
        .task-tab.waiting {{ border-left: 3px solid #f59e0b; }}
        .task-tab.failed {{ border-left: 3px solid #ef4444; }}
        #task-detail {{ flex: 1; padding: 16px; overflow-y: auto; }}
        .message {{ padding: 12px 16px; border-radius: 12px; max-width: 85%; }}
        .message.agent {{ background: #0f3460; align-self: flex-start; }}
        .message.user {{ background: #1a4a7a; align-self: flex-end; }}
        .message-label {{ font-size: 11px; color: #00d4ff; margin-bottom: 4px; text-transform: uppercase; }}
        .message.completed {{ border-left: 3px solid #10b981; }}
        .message.waiting {{ border-left: 3px solid #f59e0b; }}
        .message.failed {{ border-left: 3px solid #ef4444; }}
        .task-status {{ padding: 16px; border-radius: 8px; background: #0f3460; margin-top: 12px; }}
        .status-badge {{ display: inline-block; padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; }}
        .status-badge.completed {{ background: #10b981; color: #fff; }}
        .status-badge.failed {{ background: #ef4444; color: #fff; }}
        .status-badge.waiting {{ background: #f59e0b; color: #1a1a2e; }}
        .status-badge.active {{ background: #00d4ff; color: #1a1a2e; }}
    </style>
</head>
<body>
    <div class="main-layout">
        <div class="chat-section">
            <div class="chat-header">
                <span class="agent-name">Compass Agent</span>
                <span class="agent-role">Control Plane</span>
            </div>
            <div class="chat-messages">
                {messages_html}
            </div>
            <div class="input-area">
                <input type="text" id="message-input" placeholder="Type your message...">
                <button id="send-btn">Send</button>
            </div>
        </div>
        <div class="task-section">
            <div class="section-title">Tasks</div>
            <div class="task-tabs">
                {tasks_html}
            </div>
            <div id="task-detail">
                <!-- Task detail rendered dynamically -->
            </div>
        </div>
    </div>
    <script>
        // Global state
        let currentTaskId = null;
        let messages = [];

        // Initialize
        document.addEventListener('DOMContentLoaded', function() {{
            // Auto-select first waiting task if any
            const firstWaiting = document.querySelector('.task-tab.waiting');
            if (firstWaiting) {{
                selectTask(firstWaiting.dataset.taskId);
            }}
        }});

        // Task tab click handler - use event delegation
        document.addEventListener('click', function(e) {{
            const taskTab = e.target.closest('.task-tab');
            if (taskTab) {{
                selectTask(taskTab.dataset.taskId);
            }}
        }});

        async function selectTask(taskId) {{
            currentTaskId = taskId;

            // Update tab visual selection
            document.querySelectorAll('.task-tab').forEach(tab => {{
                tab.classList.toggle('selected', tab.dataset.taskId === taskId);
            }});

            // Fetch task details
            try {{
                const resp = await fetch('/tasks/' + taskId);
                const data = await resp.json();

                // Build messages from task
                messages = [];
                if (data.task && data.task.status && data.task.status.message) {{
                    const msg = data.task.status.message;
                    const text = msg.parts && msg.parts[0] ? msg.parts[0].text : '';
                    const state = data.task.status.state;

                    // Add user request
                    if (data.task.metadata && data.task.metadata.user_request) {{
                        messages.push({{ role: 'USER', text: data.task.metadata.user_request, style: 'normal' }});
                    }}
                    // Add agent question based on state
                    if (state === 'TASK_STATE_INPUT_REQUIRED' && text) {{
                        messages.push({{ role: 'COMPASS', text: text, style: 'input-required' }});
                    }} else if (state === 'TASK_STATE_COMPLETED') {{
                        messages.push({{ role: 'COMPASS', text: 'Task completed: ' + text, style: 'completed' }});
                    }} else if (state === 'TASK_STATE_FAILED') {{
                        messages.push({{ role: 'COMPASS', text: text || 'Task failed', style: 'failed' }});
                    }} else if (text) {{
                        messages.push({{ role: 'COMPASS', text: text, style: 'normal' }});
                    }}
                }}

                renderMessages();
                renderTaskDetail(data.task);
            }} catch (e) {{
                console.error('Failed to load task:', e);
            }}
        }}

        function renderMessages() {{
            const container = document.querySelector('.chat-messages');
            container.innerHTML = messages.map(m => `
                <div class="message ${{m.role.toLowerCase()}} ${{m.style || ''}}">
                    <div class="message-label">${{m.role}}</div>
                    <div>${{escapeHtml(m.text)}}</div>
                </div>
            `).join('');
            container.scrollTop = container.scrollHeight;
        }}

        function renderTaskDetail(task) {{
            const detail = document.getElementById('task-detail');
            const state = task.status.state;
            const stateLabel = state.replace('TASK_STATE_', '').toLowerCase();
            const badgeClass = stateLabel === 'completed' ? 'completed' :
                               stateLabel === 'failed' ? 'failed' :
                               stateLabel === 'input_required' ? 'waiting' : 'active';

            detail.innerHTML = `
                <div class="task-status">
                    <div style="margin-bottom: 12px;">
                        <span class="status-badge ${{badgeClass}}">${{stateLabel.replace('_', ' ')}}</span>
                    </div>
                    <div style="font-size: 12px; color: #888; margin-bottom: 8px;">Task: ${{task.id}}</div>
                    ${{task.metadata && task.metadata.task_type ? '<div style="margin-bottom: 4px;">Type: ' + task.metadata.task_type + '</div>' : ''}}
                    ${{task.metadata && task.metadata.office_request ? '<div style="font-size: 11px; color: #666;">Capability: ' + task.metadata.office_request.capability + '</div>' : ''}}
                </div>
            `;
        }}

        function escapeHtml(text) {{
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }}

        // Basic interactivity
        document.getElementById("send-btn").addEventListener("click", sendMessage);
        document.getElementById("message-input").addEventListener("keypress", function(e) {{
            if (e.key === 'Enter') sendMessage();
        }});

        async function sendMessage() {{
            const input = document.getElementById("message-input");
            const text = input.value.trim();
            if (!text || !currentTaskId) return;

            // Add user message to UI
            messages.push({{ role: 'USER', text: text, style: 'normal' }});
            renderMessages();
            input.value = "";

            // Send resume request
            try {{
                const resp = await fetch('/tasks/' + currentTaskId + '/resume', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ input: text }})
                }});
                const data = await resp.json();

                // Add compass response
                if (data.task && data.task.status && data.task.status.message) {{
                    const msg = data.task.status.message;
                    const msgText = msg.parts && msg.parts[0] ? msg.parts[0].text : '';
                    const state = data.task.status.state;

                    if (state === 'TASK_STATE_COMPLETED') {{
                        messages.push({{ role: 'COMPASS', text: msgText || 'Task completed', style: 'completed' }});
                    }} else if (state === 'TASK_STATE_FAILED') {{
                        messages.push({{ role: 'COMPASS', text: msgText || 'Task failed', style: 'failed' }});
                    }} else if (msgText) {{
                        messages.push({{ role: 'COMPASS', text: msgText, style: 'normal' }});
                    }}
                }} else if (data.task) {{
                    messages.push({{ role: 'COMPASS', text: 'Task updated', style: 'normal' }});
                }}

                renderMessages();
                renderTaskDetail(data.task);

                // Refresh task list
                pollTasks();
            }} catch (e) {{
                console.error('Failed to send:', e);
                messages.push({{ role: 'COMPASS', text: 'Error: ' + e.message, style: 'failed' }});
                renderMessages();
            }}
        }}

        async function pollTasks() {{
            try {{
                const resp = await fetch('/tasks');
                const data = await resp.json();
                // Update task tabs could be added here
            }} catch (e) {{
                console.error('Poll failed:', e);
            }}
        }}

        // Poll for updates
        setInterval(pollTasks, 10000);
    </script>
</body>
</html>
    '''