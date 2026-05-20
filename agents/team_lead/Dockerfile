FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml /app/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir pyyaml>=6.0 \
    && apt-get clean

COPY framework/     /app/framework/
COPY agents/       /app/agents/
COPY skills/       /app/skills/
COPY config/       /app/config/
COPY scripts/      /app/scripts/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    ARTIFACT_ROOT=/app/artifacts \
    AGENT_RUNTIME=claude-code

LABEL constellation.agent_id="team-lead"
LABEL constellation.agent_name="Team Lead Agent"
LABEL constellation.agent_role="fundamental"

RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python3", "scripts/run_local.py", "team-lead", "--port", "8030"]