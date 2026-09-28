"""Versioned snapshot metadata and stopped-service recovery; no Frappe import required."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from frappe_restic.config import bench_root, namespace

IMAGE_COMMIT_FILE = Path(os.environ.get("RESTIC_IMAGE_COMMIT_FILE", "/home/frappe/source-commit"))
RELEASE_FILE = f".{namespace()}-deployed-release.json"
MANIFEST_FILE = "recovery.json"


def read_json(path: Path) -> dict[str, Any]:
	"""Read an object without accepting malformed recovery metadata."""
	value = json.loads(path.read_text(encoding="utf-8"))
	if not isinstance(value, dict):
		raise ValueError(f"Expected a JSON object: {path.name}")
	return value


def write_json(path: Path, value: dict[str, Any]) -> None:
	"""Atomically persist private metadata on the sites volume."""
	path.parent.mkdir(parents=True, exist_ok=True)
	temporary = path.with_suffix(".tmp")
	with temporary.open("w", encoding="utf-8") as stream:
		os.chmod(temporary, 0o600)
		json.dump(value, stream, indent=2)
		stream.flush()
		os.fsync(stream.fileno())
	os.replace(temporary, path)


def image_commit() -> str:
	"""Return build-time identity, never a mutable runtime environment assertion."""
	return IMAGE_COMMIT_FILE.read_text().strip() if IMAGE_COMMIT_FILE.is_file() else "unknown"


def deployed_commit(site_path: Path) -> str:
	"""Unknown legacy sites remain unknown until a successful deployment records them."""
	path = site_path / RELEASE_FILE
	return str(read_json(path).get("commit", "unknown")) if path.exists() else "unknown"


def record_release(site_path: Path) -> None:
	"""Record the image only after successful site operations."""
	write_json(site_path / RELEASE_FILE, {"commit": image_commit(), "recorded_at": utc_now()})


def utc_now() -> str:
	"""Return an unambiguous operation timestamp."""
	return datetime.now(timezone.utc).isoformat()


def file_roots(staged: Path) -> dict[str, str]:
	"""Locate the expanded Frappe tar layout, including its site prefix."""
	roots = {}
	for kind in ("public", "private"):
		matches = [path for path in (staged / kind).rglob("files") if path.is_dir() and path.parent.name == kind]
		if len(matches) != 1:
			raise ValueError(f"Expected exactly one {kind}/files directory in backup")
		roots[kind] = matches[0].relative_to(staged).as_posix()
	return roots


def snapshot_manifest(staged: Path, *, site: str, commit: str) -> dict[str, Any]:
	"""Describe a staged backup so recovery can validate it independently."""
	manifest = {"version": 1, "site": site, "commit": commit, "created_at": utc_now(), "file_roots": file_roots(staged)}
	write_json(staged / MANIFEST_FILE, manifest)
	return manifest


def validate_snapshot(root: Path, *, site: str) -> dict[str, Any]:
	"""Validate backup data independently of the code selected for deployment."""
	manifest = read_json(root / MANIFEST_FILE)
	if manifest.get("version") != 1 or manifest.get("site") != site:
		raise ValueError("Snapshot format or site does not match the restore target")
	for path in root.rglob("*"):
		if path.is_symlink() or not (path.is_file() or path.is_dir()):
			raise ValueError("Snapshot contains a symlink or special file")
	if not (root / "database.sql").is_file() or (root / "database.sql").stat().st_size == 0:
		raise ValueError("Snapshot database is missing or empty")
	read_json(root / "site_config.json")
	for kind in ("public", "private"):
		relative = manifest["file_roots"][kind]
		path = (root / relative).resolve()
		if not path.is_relative_to((root / kind).resolve()) or not path.is_dir():
			raise ValueError(f"Invalid {kind} file root")
	return manifest


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
	"""Run a checked operation without logging command arguments containing secrets."""
	try:
		subprocess.run(command, check=True, env=env, stdin=subprocess.DEVNULL, timeout=12 * 60 * 60)
	except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
		raise RuntimeError(f"Recovery command failed: {command[0]}; inspect preceding output") from None


def latest_snapshot(site: str) -> str:
	"""Resolve the newest snapshot for this site, never another site's backup."""
	tag = f"{namespace()}-site:{site}"
	try:
		result = subprocess.run(
			["restic", "snapshots", "--json", "--tag", tag],
			check=True, capture_output=True, text=True, timeout=60,
		)
	except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
		raise RuntimeError("Could not list backups for latest recovery") from error
	snapshots = [item for item in json.loads(result.stdout) if tag in item.get("tags", [])]
	if not snapshots:
		raise ValueError(f"No backups found for site {site}")
	selected = max(snapshots, key=lambda item: datetime.fromisoformat(item["time"].replace("Z", "+00:00")))
	snapshot = selected.get("id", "")
	if not re.fullmatch(r"[0-9a-f]{64}", snapshot):
		raise ValueError("Latest backup has an invalid snapshot ID")
	return snapshot


def restore_database(site: str, database: str) -> None:
	"""Use Frappe's restore installer with grants usable by every Compose container."""
	import frappe
	from frappe.installer import install_db, is_partial, make_site_dirs, validate_database_sql
	from frappe.utils.synchronization import filelock

	os.chdir(bench_root() / "sites")
	frappe.init(site=site)
	try:
		if is_partial(database):
			raise ValueError("Partial database backups cannot restore a site")
		validate_database_sql(database, _raise=True)
		make_site_dirs()
		# Use the restore installer without new-site app installation or replacing
		# the scheduler setting imported from the backup.
		with filelock("bench_new_site", timeout=1):
			install_db(
				db_name=frappe.conf.db_name, root_login="root",
				root_password=os.environ["DB_ROOT_PASSWORD"],
				source_sql=database, force=True, db_type="mariadb",
				mariadb_user_host_login_scope="%",
			)
			validate_restored_encryption()
			frappe.db.commit()
	finally:
		frappe.destroy()


def validate_restored_encryption() -> None:
	"""Frappe creates a site key lazily; login password hashes do not need it."""
	import frappe

	if not frappe.conf.get("encryption_key") and frappe.db.sql(
		"SELECT 1 FROM `__Auth` WHERE encrypted = 1 LIMIT 1"
	):
		raise ValueError(
			"Restored database contains encrypted secrets but the backup has no site encryption key. "
			"Recover the original key from the source site's configuration before retrying."
		)


def replace_site_data(site: str, site_path: Path, content: Path, roots: dict[str, str], config: dict[str, Any]) -> None:
	"""Replace a stopped site's configuration, database and uploads with an expanded backup."""
	write_json(site_path / "site_config.json", config)
	run([sys.executable, "-m", "frappe_restic.restic_backup.recovery", "restore-database", "--site", site, "--database", str(content / "database.sql")])
	for kind in ("public", "private"):
		target = site_path / kind / "files"
		if not target.resolve().is_relative_to(site_path.resolve()) or target.is_symlink():
			raise ValueError("Refusing unsafe target upload directory")
		if target.exists():
			shutil.rmtree(target)
		shutil.copytree(content / roots[kind], target)


def restore_local_backup(site: str, site_path: Path, backup: Path, config: dict[str, Any]) -> None:
	"""Return a stopped site to a native backup set taken before a destructive operation."""
	from frappe_restic.restic_backup.restic_transport import discover_backup_set, stable_staging_tree

	with stable_staging_tree(discover_backup_set(backup), site_path / "private" / ".frappe-restic-rollback-stage") as content:
		replace_site_data(site, site_path, content, file_roots(content), config)
	run(["bench", "--site", site, "clear-cache"])


def recovery_attempt(site: str) -> str:
	"""Identify the Docker job internally; restarts keep identity, new jobs do not."""
	return hashlib.sha256(f"{site}:{socket.gethostname()}".encode()).hexdigest()[:24]


def restore_site(bench_root: Path, *, skip_safety_backup: bool = False) -> None:
	"""Restore one explicitly selected snapshot with durable one-use request receipts."""
	import fcntl

	site = os.environ.get("SITE_NAME", "")
	snapshot = os.environ.get("RESTIC_RESTORE_SNAPSHOT", "")
	request = recovery_attempt(site)
	if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", site) or site == "all":
		raise ValueError("SITE_NAME must identify one site")
	if snapshot != "latest" and not re.fullmatch(r"[0-9a-f]{8,64}", snapshot):
		raise ValueError("RESTIC_RESTORE_SNAPSHOT must be latest or a hexadecimal snapshot ID")
	if not os.environ.get("DB_ROOT_PASSWORD"):
		raise ValueError("DB_ROOT_PASSWORD is required for restore")
	sites = bench_root / "sites"
	site_path = sites / site
	if site_path.is_symlink():
		raise ValueError("Refusing a symlink site directory")
	receipt = sites / f".{namespace()}-recovery" / site / f"{request}.json"
	blocked = sites / f".{namespace()}-recovery-blocked"
	identity = {"site": site, "snapshot": snapshot, "commit": image_commit()}
	with (sites / f".{namespace()}-runtime.lock").open("a") as lock:
		try:
			fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
		except BlockingIOError as error:
			raise ValueError("Stop frontend, backend, websocket, scheduler and all queues before recovery") from error
		if blocked.exists() and read_json(blocked).get("site") != site:
			raise ValueError("Another site's incomplete recovery blocks this bench")
		if receipt.exists():
			previous = read_json(receipt)
			previous_identity = {**previous, "snapshot": previous.get("snapshot_selector", previous.get("snapshot"))}
			if all(previous_identity.get(key) == value for key, value in identity.items()) and previous.get("status") == "Completed" and not blocked.exists():
				print("This restore already completed; database and files were not touched.")
				return
			raise ValueError("Restore request already consumed or interrupted; inspect its receipt before starting a new restore deployment")
		identity["snapshot_selector"] = snapshot
		if snapshot == "latest":
			snapshot = latest_snapshot(site)
			identity["snapshot"] = snapshot
			print(f"Latest backup for {site}: {snapshot}", flush=True)
		with tempfile.TemporaryDirectory(prefix="frappe-restore-") as temporary:
			root = Path(temporary)
			print("Downloading and verifying selected snapshot...", flush=True)
			run(["restic", "restore", snapshot, "--target", str(root), "--verify"])
			manifests = list(root.rglob(MANIFEST_FILE))
			if len(manifests) != 1:
				raise ValueError("Snapshot has no unique recovery.json; legacy snapshots require manual recovery")
			content = manifests[0].parent
			manifest = validate_snapshot(content, site=site)
			identity["backup_commit"] = manifest.get("commit", "unknown")
			site_path.mkdir(parents=True, exist_ok=True)
			config_path = site_path / "site_config.json"
			current = read_json(config_path) if config_path.exists() else {}
			if current.get("db_type", "mariadb") != "mariadb":
				raise ValueError("Automated recovery currently supports MariaDB only")
			write_json(receipt, {**identity, "status": "Started", "started_at": utc_now()})
			rollback = None
			if current and not skip_safety_backup:
				safety = site_path / "private" / "deployment-backups" / f"before-restore-{request}"
				try:
					safety.mkdir(parents=True, exist_ok=False)
					(safety / ".keep").touch()
					run(["bench", "--site", site, "backup", "--with-files", "--ignore-backup-conf", "--backup-path", str(safety)])
				except Exception as error:
					write_json(receipt, {**identity, "status": "Failed", "error": str(error)[:500], "failed_at": utc_now()})
					print("Safety backup failed; the site was not changed.", file=sys.stderr)
					raise
				# An already blocked site is not a state worth returning to.
				rollback = None if blocked.exists() else safety
			write_json(blocked, {**identity, "request": request})
			try:
				config = read_json(content / "site_config.json")
				# Keep keys/settings from the backup, but never its old server/database connection.
				for key in ("db_name", "db_user", "db_password", "db_host", "db_port", "db_socket", "redis_cache", "redis_queue", "redis_socketio", "host_name"):
					config.pop(key, None)
				# A site-specific connection survives; otherwise common_site_config supplies it.
				for key in ("db_name", "db_user", "db_password", "db_host", "db_port", "db_socket", "host_name"):
					if key in current:
						config[key] = current[key]
				if not config.get("db_name"):
					config["db_name"] = "_" + hashlib.sha256(site.encode()).hexdigest()[:16]
				if not config.get("db_password"):
					config["db_password"] = secrets.token_urlsafe(24)
				config.update(db_type="mariadb", maintenance_mode=1, pause_scheduler=1)
				print("Restoring database and replacing uploaded files...", flush=True)
				replace_site_data(site, site_path, content, manifest["file_roots"], config)
				configure = os.environ.get("RESTIC_CONFIGURE_EXECUTABLE")
				if configure:
					run([configure], env={**os.environ, "RESTIC_RECOVERY_FINALIZING": "1"})
				# Fresh volumes need the Redis configuration before any queue operation.
				# Never replay jobs queued against the pre-restore database.
				run(["bench", "purge-jobs", "--site", site])
				print("Migrating restored database to the deployed application...", flush=True)
				run([sys.executable, "-m", "frappe_restic.deployment", "install", "--site", site])
				run(["bench", "--site", site, "migrate"])
				run(["bench", "--site", site, "execute", "frappe.cache_manager.clear_global_cache"])
				run(["bench", "--site", site, "clear-cache"])
				run(["bench", "--site", site, "clear-website-cache"])
				run(["bench", "--site", site, "execute", "frappe.get_installed_apps"])
				record_release(site_path)
				config = read_json(config_path)
				config.update(maintenance_mode=0, pause_scheduler=0)
				write_json(config_path, config)
				write_json(receipt, {**identity, "status": "Completed", "completed_at": utc_now()})
				blocked.unlink()
				print("Recovery and migration completed.")
			except Exception as error:
				if rollback:
					roll_back_restore(site, site_path, rollback, current, receipt=receipt, identity=identity, error=error, blocked=blocked)
				raise
			finally:
				if blocked.exists() and config_path.exists():
					config = read_json(config_path)
					config.update(maintenance_mode=1, pause_scheduler=1)
					write_json(config_path, config)


def roll_back_restore(site: str, site_path: Path, safety: Path, original: dict[str, Any], *, receipt: Path, identity: dict[str, Any], error: Exception, blocked: Path) -> None:
	"""Return a failed restore to the site's safety backup; startup stays blocked if that fails."""
	failure = {**identity, "error": str(error)[:500]}
	print("Restore failed; returning the site to its safety backup...", file=sys.stderr, flush=True)
	try:
		restore_local_backup(site, site_path, safety, {**original, "maintenance_mode": 1, "pause_scheduler": 1})
		write_json(site_path / "site_config.json", original)
	except Exception:
		write_json(receipt, {**failure, "status": "Rollback Failed", "failed_at": utc_now()})
		print("Rollback failed; startup stays blocked until a restore succeeds.", file=sys.stderr)
		return
	write_json(receipt, {**failure, "status": "Rolled Back", "rolled_back_at": utc_now()})
	blocked.unlink()
	print("The site was returned to its state before the restore.", file=sys.stderr)


def main() -> None:
	"""Container entrypoint for restoration and successful-release recording."""
	parser = argparse.ArgumentParser()
	parser.add_argument("operation", choices=("restore", "record-release", "restore-database"))
	parser.add_argument("--site")
	parser.add_argument("--database")
	parser.add_argument("--skip-safety-backup", action="store_true", help="Emergency console recovery only: existing site cannot be backed up")
	args = parser.parse_args()
	root = bench_root()
	os.chdir(root)
	if args.operation == "record-release":
		record_release(root / "sites" / args.site)
	elif args.operation == "restore-database":
		restore_database(args.site, args.database)
	else:
		restore_site(root, skip_safety_backup=args.skip_safety_backup)


if __name__ == "__main__":
	main()
