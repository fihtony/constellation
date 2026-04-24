FROM python:3.12-slim

WORKDIR /app

# Install CA certificate tooling + docker CLI for dynamic agent launch
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Copy corporate certs early so they are in the build layer cache
RUN update-ca-certificates

COPY common/       /app/common/
COPY certs/        /app/certs/
COPY compass/       /app/compass/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

CMD ["python3", "compass/app.py"]