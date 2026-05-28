FROM python:3.12-slim

WORKDIR /app

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

COPY pyproject.toml /app/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates chromium curl git nodejs npm \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "pyyaml>=6.0" \
    && pip install --no-cache-dir "playwright>=1.45,<2" \
    && apt-get clean

COPY framework/ /app/framework/
COPY agents/__init__.py /app/agents/__init__.py
COPY agents/web_dev/ /app/agents/web_dev/
COPY skills/react-nextjs/ /app/skills/react-nextjs/
COPY skills/testing/ /app/skills/testing/
RUN mkdir -p /app/config/permissions /app/config/rules /app/config/workflows
COPY config/constellation.yaml /app/config/constellation.yaml
COPY config/permissions/web-dev.yaml /app/config/permissions/web-dev.yaml
COPY config/rules/ /app/config/rules/
COPY config/workflows/ /app/config/workflows/
COPY scripts/run_local.py /app/scripts/run_local.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    ARTIFACT_ROOT=/app/artifacts \
    AGENT_RUNTIME=claude-code

LABEL constellation.agent_id="web-dev"
LABEL constellation.agent_name="Web Dev Agent"
LABEL constellation.agent_role="per-task"

RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python3", "scripts/run_local.py", "web-dev"]