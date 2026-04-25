#!/usr/bin/env bash
# build-agents.sh — Build Docker images for all dynamic (on-demand) agents.
#
# Dynamic agents are NOT started by docker-compose; they are launched at
# runtime by the Compass agent via the Docker API when a matching task arrives.
# Their images must be built manually before starting the compose stack.
#
# Usage:
#   ./build-agents.sh            # build all dynamic agent images
#   ./build-agents.sh android    # build only the android agent image
#
# After building, start the compose stack as usual:
#   docker compose up --build -d
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

build_android() {
    echo "==> Building android agent image: constellation-android-agent:latest"
    docker build \
        -t constellation-android-agent:latest \
        -f "${SCRIPT_DIR}/android/Dockerfile" \
        "${SCRIPT_DIR}"
    echo "    Done: constellation-android-agent:latest"
}

build_web() {
    echo "==> Building web agent image: constellation-web-agent:latest"
    docker build \
        -t constellation-web-agent:latest \
        -f "${SCRIPT_DIR}/web/Dockerfile" \
        "${SCRIPT_DIR}"
    echo "    Done: constellation-web-agent:latest"
}

build_team_lead() {
    echo "==> Building team-lead agent image: constellation-team-lead-agent:latest"
    docker build \
        -t constellation-team-lead-agent:latest \
        -f "${SCRIPT_DIR}/team-lead/Dockerfile" \
        "${SCRIPT_DIR}"
    echo "    Done: constellation-team-lead-agent:latest"
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
TARGET="${1:-all}"

case "$TARGET" in
    android)
        build_android
        ;;
    web)
        build_web
        ;;
    team-lead)
        build_team_lead
        ;;
    all)
        build_android
        build_web
        build_team_lead
        ;;
    *)
        echo "Unknown target: $TARGET"
        echo "Usage: $0 [android|web|team-lead|all]"
        exit 1
        ;;
esac

echo ""
echo "All dynamic agent images built successfully."
