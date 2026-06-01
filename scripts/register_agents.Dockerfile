FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml /app/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pyyaml>=6.0 \
    && apt-get clean

COPY framework/ /app/framework/
COPY config/ /app/config/
COPY scripts/register_agents.py /app/scripts/register_agents.py
COPY agents/compass/config.yaml /app/agents/compass/config.yaml
COPY agents/team_lead/config.yaml /app/agents/team_lead/config.yaml
COPY agents/office/config.yaml /app/agents/office/config.yaml
COPY agents/jira/config.yaml /app/agents/jira/config.yaml
COPY agents/scm/config.yaml /app/agents/scm/config.yaml
COPY agents/ui_design/config.yaml /app/agents/ui_design/config.yaml
COPY agents/web_dev/config.yaml /app/agents/web_dev/config.yaml
COPY agents/code_review/config.yaml /app/agents/code_review/config.yaml
COPY agents/log_store/config.yaml /app/agents/log_store/config.yaml

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python3", "scripts/register_agents.py"]