FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml /app/

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git nodejs npm \
    && npm install -g @anthropic-ai/claude-code \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir \
        "pyyaml>=6.0" \
        "beautifulsoup4>=4.12.3" \
        "lxml>=5.3.0" \
        "pdfplumber>=0.11.0" \
        "pypdf>=5.0.0" \
        "python-docx>=1.1.2" \
        "python-pptx>=1.0.2" \
        "pyxlsb>=1.0.10" \
        "striprtf>=0.0.29" \
        "openpyxl>=3.1.5" \
        "xlrd>=2.0.1" \
    && apt-get clean

COPY framework/ /app/framework/
COPY agents/__init__.py /app/agents/__init__.py
COPY agents/office/ /app/agents/office/
COPY skills/office-generic-methodology/ /app/skills/office-generic-methodology/
RUN mkdir -p /app/config/permissions /app/config/rules /app/config/workflows
COPY config/constellation.yaml /app/config/constellation.yaml
COPY config/permissions/office.yaml /app/config/permissions/office.yaml
COPY config/rules/ /app/config/rules/
COPY config/workflows/ /app/config/workflows/
COPY scripts/run_local.py /app/scripts/run_local.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    ARTIFACT_ROOT=/app/artifacts

LABEL constellation.agent_id="office"
LABEL constellation.agent_name="Office Agent"
LABEL constellation.agent_role="per-task"

RUN adduser --disabled-password --gecos "" --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

CMD ["python3", "scripts/run_local.py", "office", "--port", "8060"]
