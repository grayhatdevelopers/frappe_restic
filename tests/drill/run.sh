#!/usr/bin/env bash
# End-to-end deployment and recovery drill against the Restic repository in .env.test.
# Needs only docker and bash on the host.
#
#   FRAPPE_IMAGE=frappe/erpnext:v16.36.0 tests/drill/run.sh
#   FROM_IMAGE=frappe/erpnext:v15.121.3 FROM_DB_IMAGE=mariadb:10.6 \
#       FRAPPE_IMAGE=frappe/erpnext:v16.36.0 tests/drill/run.sh
#
# FROM_IMAGE and FROM_DB_IMAGE start the source on an older release and MariaDB, without
# this app, as a server being upgraded is. The first deployment then upgrades it in place,
# and the target restores that older site's snapshot onto the new release.
# Source project: deployments succeed, survive an upload failure, and roll back a failed
# migration. Target project (fresh volumes): a failed restore blocks startup, restores
# are not replayed, an explicit older snapshot restores, a failed restore of a working
# site rolls back, and the restored site serves and backs up.
# This run's snapshots are forgotten afterwards unless KEEP=1.
set -Eeuo pipefail
trap 'echo "FAIL: line ${LINENO}: ${BASH_COMMAND}" >&2' ERR

cd "$(dirname "$0")"
# Git Bash on Windows rewrites /container/paths in arguments; a no-op elsewhere.
export MSYS_NO_PATHCONV=1
[[ -f ../../.env.test ]] || { echo 'Create .env.test from .env.test.example first.' >&2; exit 2; }
grep -q '^RESTIC_REPOSITORY=.*/frappe-restic-ci-local$' ../../.env.test \
    || { echo 'Drills only run against a .../frappe-restic-ci-local repository.' >&2; exit 2; }

stamp="$(date +%s)"
export FRAPPE_IMAGE="${FRAPPE_IMAGE:?FRAPPE_IMAGE is required}"
export DRILL_IMAGE="frt-drill-app:${stamp}"
FROM_IMAGE="${FROM_IMAGE:-}"
FROM_DB_IMAGE="${FROM_DB_IMAGE:-}"
export SITE_NAME="drill-${stamp}.localhost"
source_project="frt-drill-src-${stamp}"
target_project="frt-drill-dst-${stamp}"
out="$(mktemp -d)"

cleanup() {
    if [[ "${KEEP:-0}" == 1 ]]; then
        echo "Kept ${source_project}, ${target_project}, ${DRILL_IMAGE} and the snapshots of ${SITE_NAME}."
        echo "Full operation output: ${out}/operations.log"
        return
    else
        docker run --rm --env-file ../../.env.test --entrypoint bash "$DRILL_IMAGE" -c \
            "restic snapshots --json --tag frappe-site:${SITE_NAME} | jq -r '.[].id' | xargs -r restic forget --prune --quiet" \
            || echo "WARNING: snapshots of ${SITE_NAME} were not forgotten" >&2
        for project in "$source_project" "$target_project"; do
            docker compose -p "$project" --profile app --profile ops down -v --remove-orphans >/dev/null 2>&1 || true
        done
        docker image rm "$DRILL_IMAGE" >/dev/null 2>&1 || true
    fi
    rm -rf "$out"
}
trap cleanup EXIT

compose() { docker compose -p "$1" "${@:2}"; }
tools() { compose "$1" exec -T tools "${@:2}"; }
fingerprint() { tools "$1" env/bin/python /drill/probe.py fingerprint; }
site_config() { tools "$1" jq -r ".$2 // 0" "sites/${SITE_NAME}/site_config.json"; }
snapshots() { tools "$1" bash -c "restic snapshots --json --tag frappe-site:${SITE_NAME} | jq -r '.[].id'"; }
step() { echo; echo "== $*"; }
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*" >&2; exit 1; }

# Run the one-shot site operation; extra arguments go to `compose run` (-e KEY=VALUE).
# Set OP_HOSTNAME to choose the job identity a restore receipt is keyed on.
operation() {
    local project="$1"; shift
    local result=0
    compose "$project" --profile ops run --rm -T "$@" site-operation > "$out/op" 2>&1 || result=$?
    tail -n 8 "$out/op" | sed 's/^/    | /'
    cat "$out/op" >> "$out/operations.log"
    return "$result"
}

expect_failure() { if operation "$@"; then fail "operation succeeded"; fi; }

running_site() {  # $1 project; ping through the frontend, from inside the network
    for _ in $(seq 1 90); do
        tools "$1" curl -fsS http://frontend:8080/api/method/ping 2>/dev/null | grep -q pong && return 0
        [[ -z "$(compose "$1" ps -q --status exited)" ]] || break
        sleep 2
    done
    compose "$1" --profile app logs --tail 30 backend frontend
    return 1
}

start_app() { compose "$1" --profile app up -d backend websocket queue-long frontend; running_site "$1"; }
stop_app() { compose "$1" --profile app stop backend websocket queue-long frontend >/dev/null 2>&1; }

step "Build ${DRILL_IMAGE} from ${FRAPPE_IMAGE}"
docker build -q -f Containerfile --build-arg FRAPPE_IMAGE -t "$DRILL_IMAGE" ../.. >/dev/null
pass "image built with frappe_restic baked in"

step "Source: new site with records, uploads and an encrypted secret on ${FROM_IMAGE:-$FRAPPE_IMAGE}"
DRILL_IMAGE="${FROM_IMAGE:-$DRILL_IMAGE}" DB_IMAGE="${FROM_DB_IMAGE:-mariadb:11.8}" \
    compose "$source_project" up -d --wait
tools "$source_project" bash /drill/configure.sh
tools "$source_project" bench new-site --mariadb-user-host-login-scope='%' \
    --admin-password admin --db-root-password frappe-restic-drill "$SITE_NAME" >/dev/null
tools "$source_project" env/bin/python /drill/probe.py seed
if [[ -n "$FROM_IMAGE$FROM_DB_IMAGE" ]]; then
    # The new release and MariaDB take over the same volumes.
    compose "$source_project" up -d --wait
    tools "$source_project" bash /drill/configure.sh
fi
pass "seeded"

step "Source: a deployment is refused while the application runs"
start_app "$source_project"
expect_failure "$source_project"
grep -q 'Stop runtime services before migration' "$out/op"
[[ "$(site_config "$source_project" maintenance_mode)" == 0 ]]
stop_app "$source_project"
pass "refused without touching the site"

step "Source: deployment backs up, installs frappe_restic and migrates"
operation "$source_project"
old_snapshot="$(snapshots "$source_project")"
[[ "$(wc -l <<< "$old_snapshot")" == 1 ]] || fail "expected one snapshot"
tools "$source_project" bench --site "$SITE_NAME" list-apps | grep -q frappe_restic
old_print="$(fingerprint "$source_project")"
pass "snapshot ${old_snapshot:0:12} uploaded, app installed; ${old_print}"

step "Source: an upload failure does not stop the deployment"
tools "$source_project" env/bin/python /drill/probe.py add
new_print="$(fingerprint "$source_project")"
[[ "$new_print" != "$old_print" ]]
operation "$source_project" -e RESTIC_PASSWORD=wrong-password
grep -q 'off-site upload failed' "$out/op"
[[ "$(site_config "$source_project" maintenance_mode)" == 0 ]]
[[ "$(snapshots "$source_project" | wc -l)" == 1 ]]
tools "$source_project" bench --site "$SITE_NAME" execute \
    frappe_restic.restic_backup.backup_control.report_operation_outcomes >/dev/null
alerted="$(tools "$source_project" bench --site "$SITE_NAME" execute frappe.db.get_value \
    --args '["Restic Backup Run", {"source": "Deployment", "status": "Failed"}, "notification_status"]')"
[[ "$alerted" == *'Not requested'* ]] || fail "deployment upload failure not recorded for alerting: ${alerted}"
pass "deployed on the local backup; failure recorded and picked up for alerting"

step "Source: a migration that fails after changing data is rolled back"
expect_failure "$source_project" -e DRILL_FAIL_MIGRATE=1
grep -q 'injected failure after migration' "$out/op"
[[ "$(fingerprint "$source_project")" == "$new_print" ]] || fail "data not returned to the pre-deployment backup"
[[ "$(site_config "$source_project" maintenance_mode)" == 0 ]]
[[ "$(site_config "$source_project" pause_scheduler)" == 0 ]]
tools "$source_project" test ! -e sites/.frappe-recovery-blocked
pass "records, uploads and configuration returned to their pre-deployment state"

step "Source: the next deployment succeeds"
operation "$source_project"
[[ "$(snapshots "$source_project" | wc -l)" == 3 ]] || fail "expected three snapshots"
compose "$source_project" down -v >/dev/null 2>&1
pass "3 snapshots; source removed"

step "Target: fresh volumes; a failed restore blocks startup"
compose "$target_project" up -d --wait
tools "$target_project" bash /drill/configure.sh
OP_HOSTNAME=restore-a expect_failure "$target_project" \
    -e SITE_OPERATION=restore -e RESTIC_RESTORE_SNAPSHOT=latest -e DRILL_FAIL_MIGRATE=1
tools "$target_project" test -f sites/.frappe-recovery-blocked
if compose "$target_project" --profile app run --rm -T backend true > "$out/op" 2>&1; then
    fail "backend started over an incomplete recovery"
fi
grep -q 'incomplete recovery blocks startup' "$out/op"
pass "nothing to return to: startup blocked, runtime guard refused"

step "Target: redeploying the failed request retries it"
OP_HOSTNAME=restore-a operation "$target_project" -e SITE_OPERATION=restore -e RESTIC_RESTORE_SNAPSHOT=latest
grep -q 'Retrying a restore whose last attempt ended Started' "$out/op"
tools "$target_project" test ! -e sites/.frappe-recovery-blocked
pass "retried and unblocked"

step "Target: a new job restores the older snapshot it names"
OP_HOSTNAME=restore-b operation "$target_project" -e SITE_OPERATION=restore -e "RESTIC_RESTORE_SNAPSHOT=${old_snapshot}"
tools "$target_project" test ! -e sites/.frappe-recovery-blocked
[[ "$(fingerprint "$target_project")" == "$old_print" ]] || fail "restored data is not snapshot ${old_snapshot:0:12}"
[[ "$(site_config "$target_project" maintenance_mode)" == 0 ]]
pass "records, uploads, encryption key and secret match snapshot ${old_snapshot:0:12}"

step "Target: replaying the completed request is a no-op"
OP_HOSTNAME=restore-b operation "$target_project" -e SITE_OPERATION=restore -e "RESTIC_RESTORE_SNAPSHOT=${old_snapshot}"
grep -q 'already completed' "$out/op"
pass "no-op"

step "Target: a failed restore of a working site returns it to its safety backup"
OP_HOSTNAME=restore-c expect_failure "$target_project" \
    -e SITE_OPERATION=restore -e RESTIC_RESTORE_SNAPSHOT=latest -e DRILL_FAIL_MIGRATE=1
grep -q 'returned to its state before the restore' "$out/op"
tools "$target_project" test ! -e sites/.frappe-recovery-blocked
[[ "$(fingerprint "$target_project")" == "$old_print" ]] || fail "site not returned to its safety backup"
[[ "$(site_config "$target_project" maintenance_mode)" == 0 ]]
pass "unblocked, data and configuration as before the restore"

step "Target: the latest snapshot restores"
OP_HOSTNAME=restore-d operation "$target_project" -e SITE_OPERATION=restore -e RESTIC_RESTORE_SNAPSHOT=latest
[[ "$(fingerprint "$target_project")" == "$new_print" ]] || fail "latest restore differs from the last deployed data"
pass "matches the last deployed data"

step "Target: the runtime guard refuses startup while the recovery lock is held"
# Holds the lock by hand, the way a running recovery does.
compose "$target_project" exec -d tools flock -x sites/.frappe-runtime.lock sleep 15
sleep 2
if compose "$target_project" --profile app run --rm -T backend true > "$out/op" 2>&1; then
    fail "backend started while the lock was held"
fi
grep -q 'Recovery is running' "$out/op"
tools "$target_project" flock -x -w 30 sites/.frappe-runtime.lock true
pass "startup refused"

step "Target: the restored site serves, alerts, and backs up through its worker"
start_app "$target_project"
tools "$target_project" bench --site "$SITE_NAME" execute \
    frappe_restic.restic_backup.backup_control.report_operation_outcomes >/dev/null
tools "$target_project" bash -c "grep -l '\"Rolled Back\"' sites/.frappe-recovery/${SITE_NAME}/*.json \
    | xargs jq -e '.notification_status'" >/dev/null || fail "rolled-back restore not picked up for alerting"
keys="$(tools "$target_project" bench --site "$SITE_NAME" execute \
    frappe.core.doctype.user.user.generate_keys --args '["Administrator"]')"
auth="Authorization: token $(jq -r '.api_key + ":" + .api_secret' <<< "$keys")"
api="http://frontend:8080/api"
tools "$target_project" curl -fsS -H "$auth" \
    "${api}/method/frappe.desk.desk_page.getpage?name=restic-backup-control" >/dev/null
css="$(tools "$target_project" jq -r '."restic_backups.bundle.css"' sites/assets/assets.json)"
tools "$target_project" curl -fsS -o /dev/null "http://frontend:8080${css}"
run="$(tools "$target_project" curl -fsS -X POST -H "$auth" \
    "${api}/method/frappe_restic.restic_backup.backup_control.enqueue_manual_backup" | jq -r .message.name)"
for _ in $(seq 1 90); do
    state="$(tools "$target_project" curl -fsS -H "$auth" "${api}/resource/Restic%20Backup%20Run/${run}" \
        | jq -r '.data.status + "/" + .data.remote_status')"
    [[ "$state" == Queued/* || "$state" == Running/* ]] || break
    sleep 2
done
if [[ "$state" != Succeeded/Available ]]; then
    tools "$target_project" curl -fsS -H "$auth" "${api}/resource/Restic%20Backup%20Run/${run}" | jq -r .data.error_summary
    compose "$target_project" --profile app logs --tail 40 queue-long
    fail "manual backup ended as ${state}"
fi
pass "rolled-back restore alerted; page, assets and API served; manual backup ${run} ${state}"

step "All drills passed on ${FRAPPE_IMAGE}"
