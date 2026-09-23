#!/usr/bin/env bash
# Shared lifetime lock prevents restoration underneath live application processes.
set -euo pipefail
cd "${FRAPPE_BENCH_ROOT:-/home/frappe/frappe-bench}"
namespace="${RESTIC_NAMESPACE:-frappe}"
[[ "$namespace" =~ ^[a-z][a-z0-9_-]{0,31}$ ]] || { echo "Invalid RESTIC_NAMESPACE" >&2; exit 1; }
exec 9>>"sites/.${namespace}-runtime.lock"
flock -sn 9 || { echo 'Recovery is running; application startup refused.' >&2; exit 1; }
if [[ -f "sites/.${namespace}-recovery-blocked" ]]; then
    echo 'An incomplete recovery blocks startup. Inspect recovery receipts.' >&2
    exit 1
fi
exec "$@"
