#!/usr/bin/env bash
# Stand-in for a deployment's configurator job: bench-wide connection settings.
set -euo pipefail
cd /home/frappe/frappe-bench
bench set-config -g db_host db
bench set-config -gp db_port 3306
bench set-config -g redis_cache redis://redis-cache:6379
bench set-config -g redis_queue redis://redis-queue:6379
bench set-config -g redis_socketio redis://redis-queue:6379
