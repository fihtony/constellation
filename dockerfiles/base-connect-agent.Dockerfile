# Constellation Agent Base — connect-agent backend extension
# Uses pure Python transport (no external CLI needed).
#
# Usage in agent Dockerfile:
#   FROM constellation-base-connect-agent:latest
#   COPY <agent>/ /app/<agent>/
#   ...
#
# Build:
#   docker build -t constellation-base-connect-agent:latest \
#     -f dockerfiles/base-connect-agent.Dockerfile .

FROM constellation-base:latest

# connect-agent uses Python LLM transport — no npm CLI required.
ENV AGENT_RUNTIME=connect-agent
