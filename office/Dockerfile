FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates tini git curl \
    && rm -rf /var/lib/apt/lists/*

# Import any corporate CA certificates placed in certs/ (supports .crt and .pem)
COPY certs/ /usr/local/share/ca-certificates/
RUN find /usr/local/share/ca-certificates/ -name "*.pem" \
        -exec sh -c 'cp "$1" "${1%.pem}.crt"' _ {} \; \
    && update-ca-certificates

RUN pip install --no-cache-dir \
    pypdf \
    pdfplumber \
    python-docx \
    python-pptx \
    openpyxl \
    xlrd \
    chardet

COPY common/ /app/common/
COPY office/ /app/office/

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

LABEL constellation.agent_id="office-agent"
LABEL constellation.agent_name="Office Agent"
LABEL constellation.agent_role="execution"

# Run as non-root user.
RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python3", "office/app.py"]