#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "${SCRIPT_DIR}/.." && pwd)

docker build \
  -t mvp-android-agent:latest \
  -f "${PROJECT_ROOT}/android/Dockerfile" \
  "${PROJECT_ROOT}"