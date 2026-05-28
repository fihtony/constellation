FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml /app/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git nodejs npm \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "pyyaml>=6.0" \
    && apt-get clean

COPY framework/ /app/framework/
COPY agents/__init__.py /app/agents/__init__.py
COPY agents/code_review/ /app/agents/code_review/
COPY skills/code-review/ /app/skills/code-review/
RUN mkdir -p /app/config/permissions /app/config/rules /app/config/workflows
COPY config/constellation.yaml /app/config/constellation.yaml
COPY config/permissions/code-review.yaml /app/config/permissions/code-review.yaml
COPY config/rules/ /app/config/rules/
COPY config/workflows/ /app/config/workflows/
COPY scripts/run_local.py /app/scripts/run_local.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    ARTIFACT_ROOT=/app/artifacts \
    AGENT_RUNTIME=claude-code

LABEL constellation.agent_id="code-review"
LABEL constellation.agent_name="Code Review Agent"
LABEL constellation.agent_role="per-task"

RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python3", "scripts/run_local.py", "code-review"]