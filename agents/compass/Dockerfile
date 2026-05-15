FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy framework and agents
COPY framework/     /app/framework/
COPY agents/        /app/agents/
COPY skills/        /app/skills/
COPY config/        /app/config/
COPY scripts/       /app/scripts/
COPY pyproject.toml /app/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

LABEL constellation.agent_id="compass"
LABEL constellation.agent_name="Compass Agent"
LABEL constellation.agent_role="fundamental"

RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python3", "scripts/run_local.py", "compass", "--port", "8000"]
