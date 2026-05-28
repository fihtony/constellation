FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml /app/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir "pyyaml>=6.0" \
    && apt-get clean

COPY framework/ /app/framework/
COPY agents/__init__.py /app/agents/__init__.py
COPY agents/jira/ /app/agents/jira/
RUN mkdir -p /app/config/permissions /app/config/rules /app/config/workflows
COPY config/constellation.yaml /app/config/constellation.yaml
COPY config/rules/ /app/config/rules/
COPY config/workflows/ /app/config/workflows/
COPY config/permissions/jira.yaml /app/config/permissions/jira.yaml
COPY scripts/run_local.py /app/scripts/run_local.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    ARTIFACT_ROOT=/app/artifacts

LABEL constellation.agent_id="jira"
LABEL constellation.agent_name="Jira Agent"
LABEL constellation.agent_role="boundary"

RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python3", "scripts/run_local.py", "jira", "--port", "8010"]