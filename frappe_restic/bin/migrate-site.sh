#!/usr/bin/env bash
# Run after stopping all runtime services. Failures before a change leave the site as it
# was; a failed migration returns the data to its backup and keeps the site paused.
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
# The deployment inherits descriptor 9 and holds the lock until it exits.
exec python -m frappe_restic.deployment deploy --site "$SITE_NAME"
