#!/usr/bin/env bash
# Run after stopping all runtime services. A failed operation stays paused.
set -euo pipefail
cd "${FRAPPE_BENCH_ROOT:-/home/frappe/frappe-bench}"
export PATH="${PWD}/env/bin:${PATH}"
[[ "${SITE_NAME:-}" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]*$ && "$SITE_NAME" != all ]] || exit 2
namespace="${RESTIC_NAMESPACE:-frappe}"
[[ "$namespace" =~ ^[a-z][a-z0-9_-]{0,31}$ ]] || exit 2
if [[ "${SITE_OPERATION:-migrate}" == restore ]]; then
    exec python -m frappe_restic.restic_backup.recovery restore
fi
[[ "${SITE_OPERATION:-migrate}" == migrate ]] || { echo 'Use migrate or restore.' >&2; exit 2; }
[[ ! -f "sites/.${namespace}-recovery-blocked" ]] || { echo 'Incomplete recovery blocks migration.' >&2; exit 1; }
exec 9>>"sites/.${namespace}-runtime.lock"
flock -xn 9 || { echo 'Stop runtime services before migration.' >&2; exit 1; }
backup_path=''
finish() {
    result=$?
    trap - EXIT
    if (( result != 0 )); then
        [[ -z "$backup_path" ]] || touch "$backup_path/.failed"
        bench --site "$SITE_NAME" set-config -p maintenance_mode 1 || true
        bench --site "$SITE_NAME" set-config -p pause_scheduler 1 || true
        echo 'Deployment failed; keep runtime services stopped and inspect the backup.' >&2
    fi
    exit "$result"
}
trap finish EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
bench --site "$SITE_NAME" set-config -p maintenance_mode 1
bench --site "$SITE_NAME" set-config -p pause_scheduler 1
backup_path="$(python -m frappe_restic.deployment backup --site "$SITE_NAME")"
python -m frappe_restic.deployment install --site "$SITE_NAME"
bench --site "$SITE_NAME" migrate
bench --site "$SITE_NAME" clear-cache
bench --site "$SITE_NAME" clear-website-cache
python -m frappe_restic.restic_backup.recovery record-release --site "$SITE_NAME"
bench --site "$SITE_NAME" set-config -p pause_scheduler 0
bench --site "$SITE_NAME" set-config -p maintenance_mode 0
python -m frappe_restic.retention server --root "$PWD/sites" --site "$SITE_NAME" --keep 10 --days 30 \
    || echo 'WARNING: deployment succeeded but retention failed.' >&2
