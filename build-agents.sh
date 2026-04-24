#!/usr/bin/env bash
# build-agents.sh — Build Docker images for all dynamic (on-demand) agents.
#
# Dynamic agents are NOT started by docker-compose; they are launched at
# runtime by the orchestrator via the Docker API when a matching task arrives.
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
    echo "==> Building android agent image: mvp-android-agent:latest"
    docker build \
        -t mvp-android-agent:latest \
        -f "${SCRIPT_DIR}/android/Dockerfile" \
        "${SCRIPT_DIR}"
    echo "    Done: mvp-android-agent:latest"
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
TARGET="${1:-all}"

case "$TARGET" in
    android)
        build_android
        ;;
    all)
        build_android
        ;;
    *)
        echo "Unknown target: $TARGET"
        echo "Usage: $0 [android|all]"
        exit 1
        ;;
esac

echo ""
echo "All dynamic agent images built successfully."
