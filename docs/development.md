# Development and tests

Everything runs in disposable Docker containers; the host needs only Docker and Bash.
The checkout is mounted into the bench and `bench build` writes into it as the image's
`frappe` user (uid 1000), so on Linux the checkout must be writable by uid 1000.

## Test suite

Unit and Frappe tests in a throwaway bench, removed afterwards:

```bash
FRAPPE_IMAGE=frappe/erpnext:v15.121.3 tests/docker/run.sh
FRAPPE_IMAGE=frappe/erpnext:v16.36.0 DB_IMAGE=mariadb:11.8 tests/docker/run.sh
```

It covers permissions, schedules, archive validation, deployment and restore, plus
site-less unit tests (`tests/docker/in-bench.sh`).

## End-to-end drill

Deploys, breaks and restores a real site against a real Restic repository: upload and
migration failures, rollbacks, restores into fresh volumes, replay protection, the runtime
guard, and a backup from the restored site. See the header of `tests/drill/run.sh`.

1. Copy `.env.test.example` to `.env.test` (ignored by git). Use a test bucket and a
   repository path nothing else writes to; the drill only runs against a path ending in
   `/frappe-restic-ci-local`.
2. Run:

   ```bash
   FRAPPE_IMAGE=frappe/erpnext:v16.36.0 tests/drill/run.sh
   ```

The drill forgets the snapshots it created and removes its containers, volumes and
image. Set `KEEP=1` to keep them for inspection. Record results in `docs/validation/`.
