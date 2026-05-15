FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*

COPY framework/     /app/framework/
COPY agents/        /app/agents/
COPY skills/        /app/skills/
COPY config/        /app/config/
COPY scripts/       /app/scripts/
COPY pyproject.toml /app/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

LABEL constellation.agent_id="web-dev"
LABEL constellation.agent_name="Web Dev Agent"
LABEL constellation.agent_role="execution"

RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python3", "scripts/run_local.py", "web-dev", "--port", "8050"]
