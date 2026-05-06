# Constellation Agent Base — claude-code backend extension
# Adds nodejs/npm + @anthropic-ai/claude-code CLI on top of constellation-base.
#
# Usage in agent Dockerfile:
#   FROM constellation-base-claude-code:latest
#   COPY <agent>/ /app/<agent>/
#   ...
#
# Build:
#   docker build -t constellation-base-claude-code:latest \
#     -f dockerfiles/base-claude-code.Dockerfile .

FROM constellation-base:latest

# nodejs/npm + Claude Code CLI for claude-code agentic runtime backend
RUN apt-get update \
    && apt-get install -y --no-install-recommends nodejs npm \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code

ENV AGENT_RUNTIME=claude-code
