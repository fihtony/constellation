FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY common/      /app/common/
COPY teams-gateway/ /app/teams_gateway/
# Create hyphenated alias so docker-compose command path still works
RUN ln -s /app/teams_gateway /app/teams-gateway

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

LABEL constellation.agent_id="teams-gateway"
LABEL constellation.agent_name="Teams Gateway"
LABEL constellation.agent_role="integration"

RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python3", "teams-gateway/app.py"]
