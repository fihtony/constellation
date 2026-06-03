#!/usr/bin/env bash
# stop.sh — Stop all Constellation v2 containers. Works for both
# docker and rancher runtimes.
#
# Usage: ./stop.sh
#
# Two-phase teardown:
#   1. `docker compose down` against BOTH v2 compose files. Both files
#      live in the same directory and therefore share the default
#      project name `constellation`, so whichever runtime is active,
#      the matching `down` actually stops the long-running services
#      (registry, compass, team-lead, jira, scm, ui-design). The other
#      `down` is a safe no-op.
#   2. Sweep any leftover per-task agent containers (office, web-dev,
#      code-review) by label. Normally each one auto-removes when its
#      task finishes, but if an orchestrator crashed mid-task the
#      container can outlive its parent; this catches that case.

set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$script_dir"

# Phase 1 — both compose files (one is a no-op).
for compose_file in docker-compose-v2.yml docker-compose-v2.rancher.yml; do
    if [ -f "$compose_file" ]; then
        echo "==> docker compose -f $compose_file down"
        docker compose -f "$compose_file" down --remove-orphans || true
    fi
done

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
