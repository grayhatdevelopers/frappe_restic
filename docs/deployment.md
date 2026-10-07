# Deployment and restore for containers

For Docker/Compose (or similar) deployments where the bench runs in containers sharing
one `sites` volume. The app ships two scripts for this:

- `frappe_restic/bin/runtime-guard.sh` — entrypoint wrapper for every long-running process.
- `frappe_restic/bin/migrate-site.sh` — one-shot job that deploys or restores a site.

## Image

- Include this app and install `restic`, `flock` (util-linux) and Bash.
- Optional: write the application revision (e.g. the git commit) to `/home/frappe/source-commit`
  at build time, or set `RESTIC_IMAGE_COMMIT_FILE`. Releases and restore receipts record it,
  so you can tell which image a snapshot came from; without it they record `unknown`.

## Runtime services

Use the guard as the entrypoint of every backend, worker, scheduler, websocket and
frontend service, keeping each service's own command:

```yaml
entrypoint:
  - bash
  - /home/frappe/frappe-bench/apps/frappe_restic/frappe_restic/bin/runtime-guard.sh
  - /usr/local/bin/entrypoint.sh
```

The guard holds a shared lock for the life of the process. It refuses to start while a
deployment or restore holds the lock, or while an incomplete restore has blocked the
site. Use the same `RESTIC_NAMESPACE` in every container.

## Deploy

Stop all runtime services, then run the job with `SITE_NAME` and `DB_ROOT_PASSWORD` set:

```bash
bash apps/frappe_restic/frappe_restic/bin/migrate-site.sh
```

It takes the exclusive lock, then:

1. Creates a complete local backup, validates it and uploads it.
2. Puts the site in maintenance and pauses the scheduler.
3. Installs this app if the site does not have it yet, migrates and clears caches.
4. Records the release, resumes the site and applies local retention.

Start the runtime services only after it succeeds. With Compose, make them depend on the
job with `condition: service_completed_successfully`.

If something fails:

- **Local backup fails:** the job stops before anything changes.
- **Upload fails:** the deployment continues on the validated local backup, which is kept
  (pinned with `.failed`). The site emails the failure once it runs again.
- **Migration fails:** the site is returned to the backup (database, uploads and
  configuration, maintenance off) and the job fails, so the new release never starts.
  Redeploy the previous release to serve the site again. If returning to the backup
  itself fails, startup stays blocked until a restore succeeds.

Deployment backups keep the newest ten plus the last 30 days, excluding pinned ones.
Set `RESTIC_DEPLOYMENT_BACKUP_UPTIME_KUMA_URL` for a separate deployment push monitor.

## Restore

Pick the application revision (image) that matches the snapshot, stop all runtime
services and run the same job with:

| Variable | Value |
| --- | --- |
| `SITE_OPERATION` | `restore` |
| `SITE_NAME` | The site to restore. |
| `RESTIC_RESTORE_SNAPSHOT` | A snapshot ID, or `latest` for the newest snapshot of this site. |
| `DB_ROOT_PASSWORD` | MariaDB root password. |

The restore verifies the snapshot belongs to the site, takes a safety backup of the
current site, restores the database and uploads, keeps the site's encryption key,
clears stale jobs, migrates, installs this app if the snapshot lacks it and clears
caches.

Each request is recorded in a receipt under `sites/.<namespace>-recovery/<site>/`, keyed
by the job's hostname. Re-running a completed request does nothing; re-running one that
failed or was interrupted tries it again.

If something fails:

- **`DB_ROOT_PASSWORD` does not open the database server:** refused before anything
  changes; fix the password and redeploy the same request.
- **Before the safety backup completes:** the site is unchanged.
- **Later:** the site is returned to its safety backup and the job fails.
- **No state to return to** (fresh volumes, the return failed, the site was already
  blocked, or the emergency `--skip-safety-backup` console option was used): startup stays blocked by
  `sites/.<namespace>-recovery-blocked`. Inspect the receipt, then run the restore again.

Fresh volumes need database and Redis connections in `common_site_config.json` before
restoring (run your configurator job first). If the bench needs more setup after the
data is restored, such as refreshing assets, set `RESTIC_CONFIGURE_EXECUTABLE` to an
absolute path; it runs before the migration with `RESTIC_RECOVERY_FINALIZING=1`.

## Alerts while services are stopped

Deployment upload failures and failed or rolled-back restores happen while the site is
down, so they are recorded on the sites volume. The running site emails them and shows
them in every System Manager's notification bell within 15 minutes, and logs restore
failures to Error Log. A site that stays in maintenance or
blocked cannot send anything: watch the job's exit status and the site from outside.

## Custom orchestration

Projects with their own deployment pipeline can call the steps directly, with services
stopped and the lock held:

```bash
python -m frappe_restic.deployment backup --site "$SITE_NAME"
# Run the project's migration here; install after it, then clear caches.
python -m frappe_restic.deployment install --site "$SITE_NAME"
python -m frappe_restic.restic_backup.recovery record-release --site "$SITE_NAME"
python -m frappe_restic.retention server --root "$PWD/sites" --site "$SITE_NAME" --days 30
```

`backup` prints only the backup directory to stdout (logs go to stderr). It fails if the
local backup or its validation fails, and only warns when the upload fails. If the
migration then fails, pin that directory with a `.failed` file and restore it.

## Environment

In addition to the [repository settings](../README.md#setup):

| Variable | Default | Purpose |
| --- | --- | --- |
| `SITE_NAME` | | Site to deploy or restore. |
| `DB_ROOT_PASSWORD` | | Needed for restores and for returning a failed deployment to its backup. |
| `SITE_OPERATION` | `migrate` | `restore` to restore instead. |
| `RESTIC_RESTORE_SNAPSHOT` | | Snapshot ID or `latest`. |
| `RESTIC_CONFIGURE_EXECUTABLE` | | Optional bench configuration script for restores. |
| `RESTIC_DEPLOYMENT_BACKUP_UPTIME_KUMA_URL` | | Optional deployment push monitor. |
| `FRAPPE_BENCH_ROOT` | `/home/frappe/frappe-bench` | Bench location. |
| `RESTIC_IMAGE_COMMIT_FILE` | `/home/frappe/source-commit` | Optional application revision file. |
