# Constellation Agent Base Image
# Shared foundation for ALL constellation agents.
# Provides: Python 3.12, tini, git, curl, ca-certs, requests, non-root user.
#
# Build:
#   docker build -t constellation-base:latest -f dockerfiles/base.Dockerfile .
#
# Agents extend this image with their own layers (agent code, backend CLI, etc.)

FROM python:3.12-slim

WORKDIR /app

# Core system dependencies shared by all agents:
#   tini        — proper PID 1 / signal handling in containers
#   git         — SCM operations, clone, push
#   curl        — health checks, HTTP utilities
#   ca-certificates — TLS trust
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        tini \
        git \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Import any corporate CA certificates placed in certs/ (supports .crt and .pem)
COPY certs/ /usr/local/share/ca-certificates/
RUN find /usr/local/share/ca-certificates/ -name "*.pem" \
        -exec sh -c 'cp "$1" "${1%.pem}.crt"' _ {} \; \
    && update-ca-certificates

# Python dependencies shared by all agents
RUN pip install --no-cache-dir requests

# Copy shared common library
COPY common/ /app/common/

# Python environment defaults
ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Non-root user for security (OWASP A05)
RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
