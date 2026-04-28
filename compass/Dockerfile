FROM python:3.12-slim

WORKDIR /app

# Install CA certificate tooling + docker CLI for dynamic agent launch
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy corporate certs early so they are in the build layer cache
RUN update-ca-certificates

COPY common/       /app/common/
COPY compass/      /app/compass/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Run as non-root user. Docker socket access is granted at runtime via group_add in docker-compose.
RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python3", "compass/app.py"]