#!/usr/bin/env bash
# Stand-in for a deployment's configurator job: bench-wide connection settings.
set -euo pipefail
cd /home/frappe/frappe-bench
# The sites volume keeps the apps.txt of the image that first created it.
ls -1 apps > sites/apps.txt
bench set-config -g db_host db
bench set-config -gp db_port 3306
bench set-config -g redis_cache redis://redis-cache:6379
bench set-config -g redis_queue redis://redis-queue:6379
bench set-config -g redis_socketio redis://redis-queue:6379
