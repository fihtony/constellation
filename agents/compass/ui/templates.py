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
    <div class="task-tab {cls}">
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
        .chat-section {{ flex: 1; background: #16213e; border-radius: 12px; padding: 20px; display: flex; flex-direction: column; }}
        .task-section {{ flex: 1; background: #16213e; border-radius: 12px; padding: 20px; }}
        /* CSS classes as defined in design spec */
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

        // Basic interactivity
        document.getElementById("send-btn").addEventListener("click", sendMessage);
        async function sendMessage() {{
            const input = document.getElementById("message-input");
            const text = input.value;
            if (!text) return;
            // Send to Compass via fetch
            input.value = "";
        }}

        // Log polling
        async function pollLogs() {{
            if (!currentTaskId) return;
            try {{
                const resp = await fetch("/logs/" + currentTaskId);
                const data = await resp.json();
                updateLogsPanel(data.logs);
            }} catch (e) {{
                console.error("Log poll failed:", e);
            }}
            setTimeout(pollLogs, 5000);
        }}

        function updateLogsPanel(logs) {{
            // Update the logs panel with new log entries
            console.log("Updating logs:", logs);
        }}
    </script>
</body>
</html>
    '''