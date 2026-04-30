FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini curl \
    && rm -rf /var/lib/apt/lists/*

COPY common/     /app/common/
COPY ui-design/  /app/ui-design/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

LABEL constellation.agent_id="ui-design-agent"
LABEL constellation.agent_name="UI Design Agent"
LABEL constellation.agent_role="integration"

# Run as non-root user.
RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "ui-design/app.py"]
