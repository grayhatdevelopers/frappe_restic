#!/usr/bin/env bash
# Run the app's tests in a disposable Docker bench.
#
#   FRAPPE_IMAGE=frappe/erpnext:v15.121.3 tests/docker/run.sh
#   FRAPPE_IMAGE=frappe/erpnext:v16.36.0 DB_IMAGE=mariadb:11.8 tests/docker/run.sh
#
# KEEP=1 leaves the project running for inspection; remove it with
#   docker compose -p <project> down -v
set -Eeuo pipefail

cd "$(dirname "$0")"
export MSYS_NO_PATHCONV=1
export COMPOSE_PROJECT_NAME="frt-test-$(date +%s)"
echo "Project ${COMPOSE_PROJECT_NAME} using ${FRAPPE_IMAGE:?FRAPPE_IMAGE is required}"

cleanup() {
    if [[ "${KEEP:-0}" == 1 ]]; then
        echo "Kept ${COMPOSE_PROJECT_NAME}; remove with: docker compose -p ${COMPOSE_PROJECT_NAME} down -v"
    else
        docker compose down -v --remove-orphans >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT

docker compose up -d --wait
docker compose exec -T bench bash /home/frappe/frappe_restic/tests/docker/in-bench.sh
