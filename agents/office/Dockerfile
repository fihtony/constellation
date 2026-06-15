# Thin per-agent layer.  All shared setup (system packages, Python deps,
# agentic CLI, framework code, non-root user) lives in docker/Dockerfile.base.
# This file only adds the agent's own code, its skill, and its permissions
# file, then sets the agent's labels and CMD.

ARG AGENT_RUNTIME=claude-code
FROM constellation-base:agentic-${AGENT_RUNTIME}

# Office-only document-reading deps.  These are pure-Python wheels
# (manylinux) so no C toolchain is needed.  They live here rather
# than in the shared base so the boundary agents (jira, scm,
# ui_design) do not carry the ~110 MB overhead.
RUN pip install --no-cache-dir \
        "beautifulsoup4>=4.12.3" \
        "lxml>=5.3.0" \
        "pdfplumber>=0.11.0" \
        "pypdf>=5.0.0" \
        "python-docx>=1.1.2" \
        "python-pptx>=1.0.2" \
        "pyxlsb>=1.0.10" \
        "striprtf>=0.0.29" \
        "openpyxl>=3.1.5" \
        "xlrd>=2.0.1"

COPY agents/office/   /app/agents/office/
COPY skills/office-generic-methodology/  /app/skills/office-generic-methodology/
COPY config/permissions/office.yaml  /app/config/permissions/office.yaml

LABEL constellation.agent_id="office"
LABEL constellation.agent_name="Office Agent"
LABEL constellation.agent_role="per-task"

USER appuser

CMD ["python3", "scripts/run_local.py", "office", "--port", "8060"]
