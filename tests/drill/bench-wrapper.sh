#!/usr/bin/env bash
# Mounted ahead of the real bench. With DRILL_FAIL_MIGRATE=1, migrate runs, then damages
# the site's data and fails, so a rollback has something real to undo.
if [[ "${DRILL_FAIL_MIGRATE:-0}" == 1 && "${3:-}" == migrate ]]; then
    /usr/local/bin/bench "$@" || exit
    /home/frappe/frappe-bench/env/bin/python /drill/probe.py damage || exit
    echo "drill: injected failure after migration" >&2
    exit 1
fi
exec /usr/local/bin/bench "$@"
