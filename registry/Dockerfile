FROM python:3.12-slim

WORKDIR /app

COPY common/   /app/common/
COPY registry/ /app/registry/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

# Run as non-root user.
RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python3", "registry/app.py"]