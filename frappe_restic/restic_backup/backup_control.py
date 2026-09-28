"""Frappe control plane for local backups and encrypted Restic snapshots."""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from frappe_restic.config import namespace

import frappe
from frappe import _
from frappe.utils import cint, get_time, now_datetime
from frappe.utils.background_jobs import enqueue
from frappe.utils.backups import BackupGenerator, get_backup_path
from redis.exceptions import LockError

from frappe_restic.restic_backup.recovery import image_commit, read_json, snapshot_manifest, write_json

from frappe_restic.restic_backup.doctype.restic_backup_settings.restic_backup_settings import (
	parse_backup_schedule,
	parse_recipients,
)
from frappe_restic.restic_backup.restic_transport import (
	BackupTransportError,
	backup_with_restic,
	list_snapshots,
	release_identity,
	release_tag,
	restic_is_configured,
	run_maintenance,
	stable_staging_tree,
	validate_backup_set,
)

SYSTEM_MANAGER_ROLE = "System Manager"
BACKUP_PAGE = "restic-backup-control"
CONNECTIONS = 2
RESTORE_OUTCOMES = {
	"Failed": "The safety backup failed, so the site was not changed.",
	"Rolled Back": "The site was returned to its state before the restore.",
}


def sync_backup_defaults() -> None:
	"""Align Frappe's native downloadable-backup limit with the configured policy."""
	frappe.db.set_single_value(
		"System Settings",
		"backup_limit",
		_settings_int("local_backup_limit", 4),
	)


def schedule_due_backups() -> None:
	"""Enqueue each enabled weekday/time row once, with a 15-minute grace window."""
	settings = frappe.get_single("Restic Backup Settings")
	if not cint(settings.enabled):
		return

	current = now_datetime()
	for days, backup_time in parse_backup_schedule(settings.backup_schedule):
		for date in (current.date() - timedelta(days=1), current.date()):
			if date.strftime("%A") not in days:
				continue
			slot = datetime.combine(date, get_time(backup_time))
			if slot <= current < slot + timedelta(minutes=15):
				run_key = f"scheduled:{frappe.local.site}:{slot.isoformat()}"
				_enqueue_backup(run_key=run_key, source="Scheduled", scheduled_for=slot)


@frappe.whitelist(methods=["POST"])
def enqueue_manual_backup() -> dict[str, str]:
	"""Queue a manual full backup; return the run immediately for UI polling."""
	_require_system_manager()
	run_key = f"manual:{frappe.local.site}:{now_datetime().strftime('%Y%m%dT%H%M%S%f')}"
	return _enqueue_backup(run_key=run_key, source="Manual")


def run_offsite_test() -> dict[str, str]:
	"""Run an explicit CLI-only upload synchronously; never enable background jobs."""
	_require_system_manager()
	if not _env_flag("RESTIC_OFFSITE_BACKUP_ENABLED"):
		raise BackupTransportError("This command requires a process-local off-site opt-in.")
	previous = frappe.flags.get("restic_backup_test")
	frappe.flags.restic_backup_test = True
	try:
		result = _enqueue_backup(
			run_key=f"test:{frappe.local.site}:{now_datetime().isoformat()}", source="Manual", run_inline=True
		)
	finally:
		frappe.flags.restic_backup_test = previous
	run = frappe.get_doc("Restic Backup Run", result["name"])
	if run.status != "Succeeded" or not run.restic_snapshot_id:
		raise BackupTransportError(run.error_summary or "Off-site test did not complete")
	return {"name": run.name, "snapshot": run.restic_snapshot_id, "status": run.status}


def _enqueue_backup(*, run_key: str, source: str, scheduled_for: datetime | None = None, run_inline: bool = False) -> dict[str, str]:
	existing = frappe.db.get_value("Restic Backup Run", {"run_key": run_key}, "name")
	if existing:
		return {"name": existing, "status": "already_queued"}

	run = frappe.get_doc(
		{
			"doctype": "Restic Backup Run",
			"run_key": run_key,
			"source": source,
			"status": "Queued",
			"scheduled_for": scheduled_for,
			"local_status": "Pending",
			"remote_status": "Not Attempted",
		}
	).insert(ignore_permissions=True)
	frappe.db.commit()
	if run_inline:
		execute_backup(run.name)
		return {"name": run.name, "status": "completed"}
	try:
		enqueue(
			"frappe_restic.restic_backup.backup_control.execute_backup",
			queue="long",
			timeout=14 * 60 * 60,
			job_id=f"restic-backup-{frappe.local.site}-{run.name}",
			run_name=run.name,
		)
	except Exception as error:  # noqa: BLE001 - preserve the durable run on queue failure
		_fail_run(run.name, _safe_error(error))
		frappe.log_error(title="Could not queue Restic backup", message=frappe.get_traceback())
		raise
	return {"name": run.name, "status": "queued"}


def execute_backup(run_name: str) -> None:
	"""Create, validate, stage, encrypt, upload, retain, and report one backup."""
	lock = _new_backup_lock()
	if not lock.acquire(blocking=True):
		_fail_run(run_name, "Another backup or maintenance operation is already running.")
		return

	started = now_datetime()
	offsite = _env_flag("RESTIC_OFFSITE_BACKUP_ENABLED")
	try:
		_set_run(run_name, status="Running", started_at=started, remote_status="Not Attempted")
		paths = _create_full_native_backup()
		artifacts = validate_backup_set(paths)
		local_bytes = sum(item.size for item in artifacts)
		local_path = _relative_site_path(Path(paths["database"]).parent)
		release = release_identity()
		_set_run(
			run_name,
			local_path=local_path,
			local_status="Available",
			local_bytes=local_bytes,
			artifact_manifest=json.dumps([asdict(item) for item in artifacts], separators=(",", ":")),
			remote_status="Uploading" if offsite else "Not Attempted",
			release_identity=release,
		)
		if not offsite:
			completed = now_datetime()
			_set_run(
				run_name, status="Succeeded", remote_status="Not Attempted",
				completed_at=completed, duration_seconds=(completed - started).total_seconds(),
				error_summary=None,
			)
			_prune_completed_local_backup(run_name, paths)
			_notify_run(run_name, succeeded=True)
			return

		if not restic_is_configured():
			raise BackupTransportError("Off-site Restic/B2 credentials are not fully configured.")
		with stable_staging_tree(
			paths, frappe.get_site_path("private", ".frappe-restic-application-stage")
		) as staged:
			snapshot_manifest(staged, site=frappe.local.site, commit=image_commit())
			snapshot_id = backup_with_restic(
				staged,
				host=_stable_host(),
				tags=[
					_site_tag(),
					f"{namespace()}-source:{_run_source(run_name).lower()}",
					release_tag(release),
				],
				connections=CONNECTIONS,
			)

		completed = now_datetime()
		_set_run(
			run_name,
			status="Succeeded",
			remote_status="Available",
			restic_snapshot_id=snapshot_id,
			remote_verified_at=completed,
			completed_at=completed,
			duration_seconds=(completed - started).total_seconds(),
			error_summary=None,
		)
		_prune_completed_local_backup(run_name, paths)
		_notify_run(run_name, succeeded=True)
		_push_heartbeat(succeeded=True, message=f"Backup {snapshot_id[:12]} completed")
	except Exception as error:  # noqa: BLE001 - every job failure must be persisted and alerted
		frappe.log_error(title=f"Restic backup failed: {run_name}", message=frappe.get_traceback())
		_fail_run(run_name, _safe_error(error), started=started)
		if not offsite:
			_set_run(run_name, remote_status="Not Attempted")
		_notify_run(run_name, succeeded=False)
		if offsite:
			_push_heartbeat(succeeded=False, message=_safe_error(error))
	finally:
		_release_lock(lock)


def _prune_completed_local_backup(run_name: str, paths: dict[str, str]) -> None:
	"""Retention errors must not rewrite a successful backup outcome."""
	try:
		_prune_local_sets(_settings_int("local_backup_limit", 4), protected_paths=set(paths.values()))
	except OSError:
		frappe.logger("restic_backup").exception("Backup succeeded but local retention failed")
		_set_run(run_name, error_summary="Backup succeeded; local retention failed. Inspect server logs.")


@frappe.whitelist(methods=["POST"])
def reconcile_remote() -> dict[str, int]:
	"""Queue one controlled repository inventory read."""
	_require_system_manager()
	enqueue(
		"frappe_restic.restic_backup.backup_control.reconcile_remote_job",
		queue="long",
		timeout=60 * 60,
		job_id=f"restic-backup-reconcile-{frappe.local.site}",
	)
	return {"queued": 1}


def reconcile_remote_job() -> dict[str, int]:
	"""Refresh cached snapshot availability and import deployment manifests."""
	lock = _new_backup_lock()
	if not lock.acquire(blocking=True):
		return {"remote_runs": 0, "deployment_runs": 0, "skipped": 1}
	try:
		return _reconcile_remote_unlocked()
	finally:
		_release_lock(lock)


def _reconcile_remote_unlocked() -> dict[str, int]:
	"""Perform one repository inventory read while the caller holds the site lock."""
	if not restic_is_configured():
		raise BackupTransportError("Off-site Restic/B2 credentials are not fully configured.")
	snapshots = list_snapshots(site_tag=_site_tag(), connections=CONNECTIONS)
	remote_ids = {str(snapshot.get("id")) for snapshot in snapshots if snapshot.get("id")}
	verified_at = now_datetime()
	imported = _import_deployment_manifests()
	updated = 0
	for run in frappe.get_all(
		"Restic Backup Run", filters={"restic_snapshot_id": ["is", "set"]}, fields=["name", "restic_snapshot_id"]
	):
		available = any(snapshot_id.startswith(run.restic_snapshot_id) for snapshot_id in remote_ids)
		frappe.db.set_value(
			"Restic Backup Run",
			run.name,
			{"remote_status": "Available" if available else "Missing", "remote_verified_at": verified_at},
			update_modified=False,
		)
		updated += 1
	frappe.db.commit()
	return {"remote_runs": updated, "deployment_runs": imported}


def daily_maintenance() -> None:
	"""Queue maintenance with an explicit timeout beyond Frappe's 25-minute default."""
	_enqueue_maintenance(weekly=False)


def weekly_maintenance() -> None:
	"""Queue repository pruning and checking in the weekly window."""
	_enqueue_maintenance(weekly=True)


def _enqueue_maintenance(*, weekly: bool) -> None:
	if not _env_flag("RESTIC_OFFSITE_BACKUP_ENABLED"):
		return
	enqueue(
		"frappe_restic.restic_backup.backup_control.maintenance_job",
		queue="long",
		timeout=5 * 60 * 60,
		job_id=f"restic-backup-maintenance-{frappe.local.site}-{'weekly' if weekly else 'daily'}",
		deduplicate=True,
		weekly=weekly,
	)


def maintenance_job(*, weekly: bool = False) -> None:
	"""Retain and reconcile snapshots, adding prune/check in the weekly window."""
	if not _env_flag("RESTIC_OFFSITE_BACKUP_ENABLED") or not restic_is_configured():
		return
	settings = frappe.get_single("Restic Backup Settings")
	lock = _new_backup_lock()
	if not lock.acquire(blocking=True):
		return
	try:
		run_maintenance(
			site_tag=_site_tag(),
			keep_days=cint(settings.remote_keep_days) or 14,
			connections=CONNECTIONS,
			prune=weekly,
			check=weekly,
		)
		_reconcile_remote_unlocked()
	finally:
		_release_lock(lock)


def _create_full_native_backup() -> dict[str, str]:
	if cint(frappe.get_system_settings("encrypt_backup")):
		raise BackupTransportError(
			"Frappe backup encryption is enabled. Disable it for Restic backups; Restic provides off-site encryption "
			"and the Frappe setting produces archives that cannot be validated before upload."
		)
	generator = BackupGenerator(
		frappe.conf.db_name,
		frappe.conf.db_name,
		frappe.conf.db_password,
		db_socket=frappe.conf.db_socket,
		db_host=frappe.conf.db_host,
		db_port=frappe.conf.db_port,
		db_type=frappe.conf.db_type,
		ignore_conf=True,
		compress_files=True,
	)
	generator.get_backup(ignore_files=False, force=True)
	return {
		"database": generator.backup_path_db,
		"config": generator.backup_path_conf,
		"public": generator.backup_path_files,
		"private": generator.backup_path_private_files,
	}


def _prune_local_sets(limit: int, *, protected_paths: set[str]) -> None:
	backup_root = Path(get_backup_path()).resolve()
	protected = {Path(path).resolve() for path in protected_paths}
	databases = sorted(backup_root.glob("*-database.sql.gz"), key=lambda path: path.stat().st_mtime, reverse=True)
	for database in databases[limit:]:
		prefix = database.name[: -len("-database.sql.gz")]
		candidates = list(backup_root.glob(f"{prefix}-*"))
		if any(path.resolve() in protected for path in candidates):
			continue
		for path in candidates:
			if path.is_file():
				path.unlink()


def _import_deployment_manifests() -> int:
	root = Path(frappe.get_site_path("private", "deployment-backups"))
	if not root.is_dir():
		return 0
	imported = 0
	for manifest_path in sorted(root.glob("*/backup-control.json")):
		try:
			payload = json.loads(manifest_path.read_text(encoding="utf-8"))
		except (OSError, json.JSONDecodeError):
			frappe.logger("restic_backup").warning("Invalid deployment manifest: %s", manifest_path)
			continue
		if not isinstance(payload, dict):
			frappe.logger("restic_backup").warning("Invalid deployment manifest: %s", manifest_path)
			continue
		run_key = f"deployment:{frappe.local.site}:{manifest_path.parent.name}"
		remote_status = "Failed" if payload.get("status") == "Failed" else "Not Attempted"
		if payload.get("restic_snapshot_id"):
			remote_status = "Available"
		values = {
			"source": "Deployment",
			"status": payload.get("status", "Local Only"),
			"local_path": _relative_site_path(manifest_path.parent),
			"local_status": "Available" if payload.get("artifacts") else "Invalid",
			"artifact_manifest": json.dumps(payload.get("artifacts", []), separators=(",", ":")),
			"remote_status": remote_status,
			"restic_snapshot_id": payload.get("restic_snapshot_id"),
			"release_identity": payload.get("release_identity") or "unknown",
			"error_summary": payload.get("error"),
		}
		existing = frappe.db.get_value("Restic Backup Run", {"run_key": run_key}, "name")
		if existing:
			frappe.db.set_value("Restic Backup Run", existing, values, update_modified=False)
		else:
			frappe.get_doc({"doctype": "Restic Backup Run", "run_key": run_key, **values}).insert(
				ignore_permissions=True
			)
		imported += 1
	return imported


def _set_run(run_name: str, **values: Any) -> None:
	frappe.db.set_value("Restic Backup Run", run_name, values, update_modified=False)
	frappe.db.commit()


def _fail_run(run_name: str, message: str, *, started: datetime | None = None) -> None:
	completed = now_datetime()
	values: dict[str, Any] = {
		"status": "Failed",
		"remote_status": "Failed",
		"completed_at": completed,
		"error_summary": message[:2000],
	}
	if started:
		values["duration_seconds"] = (completed - started).total_seconds()
	_set_run(run_name, **values)


def report_operation_outcomes() -> None:
	"""Alert once about deployment and restore failures recorded while services were stopped."""
	_import_deployment_manifests()
	for run_name in frappe.get_all(
		"Restic Backup Run",
		filters={"source": "Deployment", "status": "Failed", "notification_status": ["is", "not set"]},
		pluck="name",
	):
		_notify_run(run_name, succeeded=False)
	for path in sorted(_receipt_directory().glob("*.json")):
		receipt = read_json(path)
		outcome = RESTORE_OUTCOMES.get(receipt.get("status"))
		if not outcome or receipt.get("notification_status"):
			continue
		message = (
			f"Restore of snapshot {receipt.get('snapshot') or 'unknown'} failed: "
			f"{receipt.get('error') or 'unknown error'}.<br>{outcome}"
		)
		frappe.log_error(title=f"Restic restore failed: {frappe.local.site}", message=message)
		receipt["notification_status"] = _send_alert(f"FAILED: {frappe.local.site} restore", message)
		write_json(path, receipt)


def _receipt_directory() -> Path:
	return Path(frappe.get_site_path("..", f".{namespace()}-recovery", frappe.local.site))


def _notify_run(run_name: str, *, succeeded: bool) -> None:
	if frappe.flags.get("restic_backup_test"):
		_set_run(run_name, notification_status="Not requested")
		return
	run = frappe.get_doc("Restic Backup Run", run_name)
	status = _send_alert(
		f"{'Succeeded' if succeeded else 'FAILED'}: {frappe.local.site} {(run.source or 'backup').lower()} backup",
		f"Backup run {run.name} finished with status {run.status}.<br>"
		f"Local status: {run.local_status or 'unknown'}<br>"
		f"Off-site status: {run.remote_status or 'unknown'}<br>"
		f"Snapshot: {run.restic_snapshot_id or 'none'}<br>"
		f"Details: {run.error_summary or 'none'}",
		succeeded=succeeded,
	)
	_set_run(run_name, notification_status=status)


def _send_alert(subject: str, message: str, *, succeeded: bool = False) -> str:
	settings = frappe.get_single("Restic Backup Settings")
	recipients = parse_recipients(settings.notification_recipients)
	if not recipients or (succeeded and not cint(settings.email_on_success)):
		return "Not requested"
	try:
		frappe.sendmail(recipients=recipients, subject=subject, message=message, delayed=False)
	except Exception:  # noqa: BLE001 - notification failure must not replace the operation outcome
		frappe.logger("restic_backup").exception("Could not send backup notification")
		return "Failed"
	return "Sent"


def _push_heartbeat(*, succeeded: bool, message: str) -> None:
	url = os.environ.get("RESTIC_BACKUP_UPTIME_KUMA_URL", "").strip()
	if not url:
		return
	separator = "&" if "?" in url else "?"
	query = urllib.parse.urlencode({"status": "up" if succeeded else "down", "msg": message[:200], "ping": ""})
	try:
		with urllib.request.urlopen(f"{url}{separator}{query}", timeout=15) as response:
			if response.status >= 400:
				raise OSError(f"HTTP {response.status}")
	except (OSError, TimeoutError, ValueError):
		frappe.logger("restic_backup").warning("Backup heartbeat delivery failed", exc_info=True)


def _settings_int(fieldname: str, default: int) -> int:
	return cint(frappe.db.get_single_value("Restic Backup Settings", fieldname)) or default


def _new_backup_lock():
	return frappe.cache.lock(
		f"restic-backup:{frappe.local.site}", timeout=15 * 60 * 60, blocking_timeout=1
	)


def _release_lock(lock) -> None:
	try:
		lock.release()
	except LockError:
		frappe.logger("restic_backup").warning("Could not release backup lock", exc_info=True)


def _site_tag() -> str:
	return f"{namespace()}-site:{frappe.local.site}"


def _stable_host() -> str:
	return namespace() + "-" + "".join(
		character if character.isalnum() or character in "._-" else "-" for character in frappe.local.site
	)


def _relative_site_path(path: Path) -> str:
	return str(path.resolve().relative_to(Path(frappe.get_site_path()).resolve())).replace("\\", "/")


def _run_source(run_name: str) -> str:
	return frappe.db.get_value("Restic Backup Run", run_name, "source") or "manual"


def _safe_error(error: Exception) -> str:
	return f"{type(error).__name__}: {error!s}"[:2000]


def _env_flag(name: str) -> bool:
	return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _require_system_manager() -> None:
	roles = set(frappe.get_roles())
	if SYSTEM_MANAGER_ROLE not in roles:
		frappe.throw(_("System Manager privileges are required."), frappe.PermissionError)
