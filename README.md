# Restic Backups for Frappe

An independent Frappe app for scheduled native backups, encrypted off-site Restic
snapshots, backup history, retention and stopped-service recovery. ERPNext is not
required. Supports Frappe v15 and v16. Automated recovery currently targets Linux,
MariaDB and a containerised bench; the database connection comes from
`common_site_config.json` (or the site's own `db_host`/`db_port`).

## Install

Install it as `apps/frappe_restic`, install its Python package into the bench virtualenv,
and add `frappe_restic` to `sites/apps.txt`. For a normal Bench distribution, use
`bench get-app <your-app-repository>` and `bench --site <site> install-app frappe_restic`.
Build assets with `bench build --app frappe_restic`, then migrate using your deployment
pipeline. Do not install or migrate a live production site without its safety backup.

The image needs `restic`, `flock` (util-linux), Bash, and the normal Frappe
dependencies. It must contain this app before installing it in an existing database.
Include the JSON, JS, SCSS, text and shell resources when distributing the source.

## Runtime

Open **Restic Backups → Backup Control** (`/app/restic-backup-control` on v15,
`/desk/restic-backup-control` on v16).
Only System Manager and the built-in Administrator can access the page, settings,
history or backup APIs. Backup records are read-only to operators. Mutations use
authenticated POST endpoints; restore has no web endpoint. Repository credentials
stay in deployment environment variables and are never returned by the dashboard.

Enable a schedule in **Restic Backup Settings**. Each row selects weekdays and a
time in the site's timezone. Manual and scheduled runs include SQL, site configuration
and public/private files. Leave native Frappe backup encryption disabled; Restic
encrypts the off-site repository. Protect local archives and keep the repository
password recoverable outside the server.

The dashboard reads cached remote state. **Check off-site backups** explicitly
queues reconciliation. Daily maintenance applies retention, weekly maintenance also
prunes and checks the repository. Keep separate repository paths per environment.

## Environment

| Variable | Purpose |
| --- | --- |
| `RESTIC_OFFSITE_BACKUP_ENABLED` | `1` uploads native backups; default `0` keeps them local. |
| `RESTIC_REPOSITORY` | Repository URL; S3-compatible storage such as Backblaze B2. |
| `RESTIC_PASSWORD` | Repository encryption password. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Restricted S3 credentials. |
| `RESTIC_NAMESPACE` | Stable tag, host and recovery-lock prefix; default `frappe`. Never change it for an existing repository. |
| `RESTIC_BACKUP_UPTIME_KUMA_URL` | Optional scheduled/manual backup push monitor. |
| `RESTIC_DEPLOYMENT_BACKUP_UPTIME_KUMA_URL` | Optional separate deployment monitor. |
| `FRAPPE_BENCH_ROOT` | Default `/home/frappe/frappe-bench`. |
| `RESTIC_IMAGE_COMMIT_FILE` | Immutable image revision file; default `/home/frappe/source-commit`. |
| `RESTIC_RESTORE_SNAPSHOT` | Explicit snapshot ID or `latest`, filtered by site and namespace. |
| `RESTIC_CONFIGURE_EXECUTABLE` | Optional absolute executable to configure the restored bench/assets; receives `RESTIC_RECOVERY_FINALIZING=1`. |
| `SITE_NAME`, `DB_ROOT_PASSWORD` | Site selection and MariaDB root credential for recovery and deployment rollback. |

## Deployment integration

Copy `frappe_restic/bin/runtime-guard.sh` into the image as an executable and use it
as the entrypoint of every backend, worker, scheduler, websocket and frontend
process sharing the sites volume. The guard holds a shared lifetime lock and blocks
startup after an incomplete restore. Use the same namespace in every container.
Bake the application revision into `/home/frappe/source-commit` when building.

Stop all runtime services, then run `bash apps/frappe_restic/frappe_restic/bin/migrate-site.sh`
inside the bench with `SITE_NAME` and `DB_ROOT_PASSWORD` set. The executable takes an
exclusive lock, creates and validates a complete backup, uploads it, pauses the site,
installs this app if necessary, migrates, clears caches, records the release and
resumes the site. Only restart services after it succeeds.

- A failed local backup stops the deployment before anything changes.
- A failed upload does not stop it: the validated local backup protects the migration,
  is pinned, and the running site emails the failure.
- A failed migration returns the site (database, uploads and configuration) to the backup
  and fails the job, so the new release never starts. Redeploy the previous release to
  serve the site again. If the return itself fails, startup is blocked until a restore succeeds.

Deployment retention keeps the newest ten plus the last 30 days, excluding `.keep`
and `.failed` directories.

Projects with their own deployment orchestrator can call these executable boundaries:

```bash
python -m frappe_restic.deployment backup --site "$SITE_NAME"
python -m frappe_restic.deployment install --site "$SITE_NAME"
# Run the project's migration and cache-clearing pipeline here.
python -m frappe_restic.restic_backup.recovery record-release --site "$SITE_NAME"
python -m frappe_restic.retention server --root "$PWD/sites" --site "$SITE_NAME" --days 30
```

`backup` writes only the backup directory to stdout (logs go to stderr), exits
nonzero if the local backup or its validation fails, and warns without failing when
only the upload fails. It pins failures and local-only copies. The orchestrator must pin
that directory with `.failed` if its subsequent migration fails. These commands
require stopped services; the complete shell entrypoint enforces that with the lock.

For recovery, select the application revision/image, stop all runtime services,
set `SITE_OPERATION=restore`, `SITE_NAME` and `RESTIC_RESTORE_SNAPSHOT`, and run the
same shell entrypoint. Recovery verifies snapshot contents/site, takes an exclusive
lock and a safety backup, restores SQL and uploads, retains the encryption key,
installs this app if absent from the restored DB, purges stale jobs, migrates and
clears caches. A durable receipt prevents replay by the same job.

- Failures before the safety backup completes leave the site unchanged.
- Later failures return the site to its safety backup, clear the block and fail the job.
- Startup stays blocked only when there is no state to return to: the return failed,
  the volumes were fresh, the site was already blocked, or `--skip-safety-backup` was used.
  Inspect the receipt before starting a new recovery job.

Fresh volumes must be configured with database/Redis connections before recovery;
use `RESTIC_CONFIGURE_EXECUTABLE` when the platform also needs assets/config refreshed.

## Alerts

Set **Notification Recipients** in Restic Backup Settings. Scheduled and manual backup
failures are emailed when they happen. Deployment upload failures and restores that failed
or were rolled back are recorded on the sites volume while services are stopped; the site
emails them within 15 minutes of running again and logs restore failures to Error Log.
A site left in maintenance or blocked cannot send anything: watch the job's exit status
and the site itself from outside, and use the Uptime Kuma push URLs for backup freshness.

## Validation

Run the full suite in a disposable Docker bench (removed afterwards):

```bash
FRAPPE_IMAGE=frappe/erpnext:v15.121.3 tests/docker/run.sh
FRAPPE_IMAGE=frappe/erpnext:v16.36.0 DB_IMAGE=mariadb:11.8 tests/docker/run.sh
```

It covers permission, schedule, archive-validation, deployment and restore tests,
plus the site-less unit tests in `tests/docker/in-bench.sh`.
Set `RESTIC_NAMESPACE=frappe` for isolated tests. Real off-site restore drills should
use a disposable repository, site and database, separate from production.
