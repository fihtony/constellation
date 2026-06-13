# Thin per-agent layer for web_dev.  web_dev is the only
# agent that needs a headless browser (chromium for playwright),
# so its heavy / agent-specific build steps stay here rather than
# in the shared base.
ARG AGENT_RUNTIME=claude-code
FROM constellation-base:agentic-${AGENT_RUNTIME}

# Headless browser for playwright.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN apt-get update \
    && apt-get install -y --no-install-recommends chromium \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# playwright Python client.  The actual browser binary was installed
# via apt above (system chromium) rather than `playwright install`,
# which avoids a second multi-hundred-MB download.
RUN pip install --no-cache-dir "playwright>=1.45,<2"

# Agent-specific code + skills.
COPY agents/web_dev/                   /app/agents/web_dev/
COPY skills/react-nextjs/              /app/skills/react-nextjs/
COPY skills/testing/                   /app/skills/testing/
COPY config/permissions/web-dev.yaml   /app/config/permissions/web-dev.yaml

LABEL constellation.agent_id="web-dev"
LABEL constellation.agent_name="Web Dev Agent"
LABEL constellation.agent_role="per-task"

USER appuser

CMD ["python3", "scripts/run_local.py", "web-dev"]
