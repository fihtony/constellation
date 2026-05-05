FROM python:3.12-slim

WORKDIR /app

# Install CA certs, tini, and curl
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini curl nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Import any corporate CA certificates placed in certs/ (supports .crt and .pem)
COPY certs/ /usr/local/share/ca-certificates/
RUN find /usr/local/share/ca-certificates/ -name "*.pem" \
        -exec sh -c 'cp "$1" "${1%.pem}.crt"' _ {} \; \
    && update-ca-certificates

RUN npm install -g @github/copilot

COPY common/       /app/common/
COPY compass/      /app/compass/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Run as non-root user. Docker socket access is granted at runtime via group_add in docker-compose.
RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "compass/app.py"]