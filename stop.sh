#!/usr/bin/env bash
# stop.sh — Stop all Constellation v2 containers. Works for both
# docker and rancher runtimes.
#
# Usage: ./stop.sh
#
# Two-phase teardown:
#   1. `docker compose down` against the v2 compose file(s).
#      ``docker-compose-v2.yml`` is the main file; the rancher
#      variant ``docker-compose.v2.rancher.yml`` is a small
#      override that only changes ``DOCKER_SOCKET_GID`` for
#      compass and team-lead.  We pass the main file plus the
#      override together so the merged services match exactly
#      what ``start.sh rancher`` would have started.  When the
#      active runtime was docker, the merged set is identical
#      to the main file alone, so the override contributes
#      nothing — the down is a no-op for the GID change.
#      Passing both is safer than guessing the runtime because
#      either compose down targets the same project name
#      (``constellation``) and the project-level resources
#      (network, named volumes) get cleaned up exactly once.
#   2. Sweep any leftover per-task agent containers (office,
#      web-dev, code-review) by label.  Normally each one
#      auto-removes when its task finishes, but if an
#      orchestrator crashed mid-task the container can outlive
#      its parent; this catches that case.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

# Phase 1 — merged compose down.  We pass the main file plus the
# rancher override; whichever runtime was actually used, the
# matching services are torn down, the other is a no-op.
main_compose="docker-compose-v2.yml"
rancher_compose="docker-compose.v2.rancher.yml"

compose_args=()
[ -f "$main_compose" ]    && compose_args+=(-f "$main_compose")
[ -f "$rancher_compose" ] && compose_args+=(-f "$rancher_compose")

if [ "${#compose_args[@]}" -gt 0 ]; then
    echo "==> docker compose ${compose_args[*]} down"
    docker compose "${compose_args[@]}" down --remove-orphans || true
fi

# Phase 2 — sweep on-demand (per-task) leftovers. The launcher tags every
# per-task container with `constellation.agent_role=on-demand`
# (see framework/launcher.py:ON_DEMAND_ROLE_LABEL), which is the
# single label that catches office, web-dev, and code-review without
# matching anything else.
echo "==> Sweeping per-task containers (label constellation.agent_role=on-demand)"
orphans="$(docker ps -a \
                --filter 'label=constellation.agent_role=on-demand' \
                --format '{{.Names}}' 2>/dev/null || true)"

if [ -z "${orphans:-}" ]; then
    echo "    (none)"
else
    while IFS= read -r name; do
        [ -z "$name" ] && continue
        echo "    - removing $name"
        docker rm -f "$name" >/dev/null 2>&1 || true
    done <<< "$orphans"
fi

echo "==> Constellation stopped."
