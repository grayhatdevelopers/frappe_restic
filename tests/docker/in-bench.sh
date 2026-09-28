#!/usr/bin/env bash
# Runs inside the disposable bench container started by run.sh.
set -Eeuo pipefail

cd /home/frappe/frappe-bench
node_bin="$(find /home/frappe/.nvm/versions/node -maxdepth 2 -name bin -type d | head -n 1)"
export PATH="${PWD}/env/bin:${node_bin}:${PATH}"

echo "== Versions"
bench version

echo "== Configure bench"
bench set-config -g db_host db
bench set-config -gp db_port 3306
bench set-config -g redis_cache redis://redis-cache:6379
bench set-config -g redis_queue redis://redis-queue:6379
bench set-config -g redis_socketio redis://redis-queue:6379

echo "== Install app into the bench"
pip install --quiet --no-build-isolation -e apps/frappe_restic
if ! grep -qx frappe_restic sites/apps.txt; then
    [[ -z "$(tail -c 1 sites/apps.txt)" ]] || echo >> sites/apps.txt
    echo frappe_restic >> sites/apps.txt
fi
bench build --app frappe_restic

echo "== Create site"
bench new-site \
    --mariadb-user-host-login-scope='%' \
    --admin-password admin \
    --db-root-password "${DB_ROOT_PASSWORD}" \
    "${SITE_NAME}"
bench --site "${SITE_NAME}" install-app frappe_restic
bench --site "${SITE_NAME}" set-config allow_tests true

echo "== Unit tests (no site required)"
python -m unittest \
    frappe_restic.restic_backup.test_restic_transport \
    frappe_restic.restic_backup.test_recovery \
    frappe_restic.restic_backup.test_deployment \
    frappe_restic.test_install

echo "== Frappe tests"
bench --site "${SITE_NAME}" run-tests --app frappe_restic

echo "== All tests passed"
