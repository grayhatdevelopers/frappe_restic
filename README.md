# Restic Backups for Frappe

Encrypted, off-site backups for any Frappe site, using [Restic](https://restic.net) and
S3-compatible storage such as Backblaze B2 or AWS S3. Schedule backups from the desk,
see every run and snapshot in one place, get an email when something fails, and deploy
or restore containerised sites without risking a working site.

ERPNext is not required.

## Key features

- **Complete backups:** database, site configuration and public/private files, validated
  before upload.
- **Encrypted off-site snapshots:** Restic encrypts and deduplicates; credentials stay in
  environment variables and are never shown in the desk.
- **Schedules:** pick weekdays and times in the site's timezone, or run a backup by hand.
- **Backup Control page:** backup history, off-site snapshot state and actions in
  one place, restricted to System Managers.
- **Retention:** local backup sets and off-site snapshots are pruned automatically; the
  repository is checked weekly.
- **Alerts:** every System Manager is told about failures in the notification bell and by
  email; optional success emails and Uptime Kuma push monitors.
- **Safe deployments and restores for containers:** back up before every migration,
  return to that backup if the migration fails, and restore a site from a snapshot into
  new or existing volumes. See [Deployment](docs/deployment.md).

## Compatibility

| Frappe | Database | Status |
| --- | --- | --- |
| v16 | MariaDB 10.6+ | Tested (unit, Frappe and end-to-end drill) |
| v15 | MariaDB 10.6+ | Tested (unit and Frappe) |

PostgreSQL is not supported. Automated deployment and restore target Linux containers.

## Installation

The server needs `restic` and `flock` (util-linux) installed.

```bash
bench get-app https://github.com/grayhatdevelopers/frappe_restic
bench --site <site> install-app frappe_restic
```

For Docker images, add the app to the `apps.json` you build with
[frappe_docker](https://github.com/frappe/frappe_docker). The official Frappe images
already include `restic` and `flock`.

## Setup

Set the repository in the environment of every process that runs backups (the web
server and the background workers):

| Variable | Purpose |
| --- | --- |
| `RESTIC_OFFSITE_BACKUP_ENABLED` | `1` uploads backups off-site; the default `0` keeps them local. |
| `RESTIC_REPOSITORY` | Repository URL, e.g. `s3:https://s3.<region>.backblazeb2.com/<bucket>/restic/<site>`. |
| `RESTIC_PASSWORD` | Repository encryption password. Keep a copy outside the server; without it the backups cannot be read. |
| `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` | Storage credentials, restricted to the bucket. |
| `RESTIC_NAMESPACE` | Tag and lock prefix, default `frappe`. Never change it for an existing repository. |
| `RESTIC_BACKUP_UPTIME_KUMA_URL` | Optional push monitor for scheduled and manual backups. |

Use a separate repository path per environment. The repository is initialised on the
first upload.

Then open **Restic Backup → Backup Settings**:

- **Enable Scheduled Backups** and add schedule rows.
- **Local Backup Sets** (default 4) are kept on the server after a successful upload.
- **Off-site Retention (Days)** (default 14) keeps snapshots in that window, and always the newest.
- **Email on Success** also emails System Managers about successful runs. They are always
  told about failures, in the notification bell and by email.

Leave Frappe's own backup encryption disabled: Restic encrypts the off-site copy, and
encrypted Frappe archives cannot be validated before upload.

## Usage

Open **Restic Backup → Backup Control** (`/app/restic-backup-control` on v15,
`/desk/restic-backup-control` on v16). From there you can run a backup, check off-site
snapshots and change settings. Only System Managers can access the page, the settings,
the history and the backup APIs; there is no restore endpoint on the web.

Scheduled backups are checked every 15 minutes. Retention runs daily; pruning and a
repository check run weekly.

To restore, see [Deployment → Restore](docs/deployment.md#restore). A restore always
takes a safety backup of the current site first.

## Documentation

- [Deployment and restore for containers](docs/deployment.md)
- [Development and tests](docs/development.md)
- [Validation results](docs/validation/)

## License

[MIT](LICENSE)
