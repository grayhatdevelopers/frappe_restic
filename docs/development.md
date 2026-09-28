# Development and tests

Everything runs in disposable Docker containers; the host needs only Docker and Bash.
The checkout is mounted into the bench and `bench build` writes into it as the image's
`frappe` user (uid 1000), so on Linux `frappe_restic/public` must be writable by uid 1000.

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

## Releases

Pull requests go to `develop` and are squash-merged, so their titles must follow
[Conventional Commits](https://www.conventionalcommits.org/) (`fix:`, `feat:`, `docs:`, ...).
A bot keeps a `develop` → `main` pull request open; merging it (as a merge commit) runs
semantic-release, which picks the next version from those titles, bumps
`frappe_restic/__init__.py`, tags and publishes the GitHub release:

| Since the last release | Next version |
|---|---|
| a breaking change (`feat!:` or a `BREAKING CHANGE:` footer) | major |
| a `feat:` | minor |
| only `fix:` / `perf:` | patch |
| nothing else (`docs:`, `ci:`, `test:`, `chore:`) | no release |

The workflows need a `RELEASE_TOKEN` repository secret: a fine-grained token for this repository
with read/write Contents, Pull requests and Issues, owned by an admin so the version bump can
pass `main`'s pull request rule.
