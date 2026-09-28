"""Read-only dashboard API backed by local state and cached run records."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import frappe
from frappe import _
from frappe.utils import cint
from frappe.utils.backups import get_backup_path

from frappe_restic.restic_backup.backup_control import _require_system_manager
from frappe_restic.restic_backup.doctype.restic_backup_settings.restic_backup_settings import (
	parse_working_days,
)
from frappe_restic.restic_backup.restic_transport import (
	REQUIRED_RESTIC_ENV,
	_run,
	restic_is_configured,
)

EDITABLE_SETTING_FIELDS = {
	"enabled",
	"backup_schedule",
	"local_backup_limit",
	"remote_keep_days",
	"notification_recipients",
	"email_on_success",
}


@frappe.whitelist()
def get_recovery_details(run_name: str | None = None, snapshot_id: str | None = None) -> dict[str, str]:
	"""Read the selected snapshot's recovery metadata only on explicit operator action."""
	_require_system_manager()
	snapshot = snapshot_id or ""
	if run_name:
		snapshot = frappe.get_doc("Restic Backup Run", run_name).restic_snapshot_id or ""
	if not re.fullmatch(r"[0-9a-f]{8,64}", snapshot):
		frappe.throw(_("This run has no usable off-site snapshot ID."))
	result = _run(["restic", "dump", snapshot, "/recovery.json"], timeout=60)
	manifest = json.loads(result.stdout)
	if (
		not isinstance(manifest, dict)
		or manifest.get("version") != 1
		or manifest.get("site") != frappe.local.site
	):
		frappe.throw(_("Snapshot recovery metadata does not match this site."))
	commit = manifest.get("commit", "")
	if not isinstance(commit, str):
		commit = "unknown"
	return {
		"commit": commit,
		"snapshot": snapshot,
		"site": frappe.local.site,
	}


@frappe.whitelist()
def get_dashboard(page: int = 1) -> dict[str, Any]:
	"""Return cached remote state plus local inventories; never contact B2."""
	_require_system_manager()
	settings = frappe.get_single("Restic Backup Settings")
	runs = frappe.get_all(
		"Restic Backup Run",
		fields=[
			"name",
			"creation",
			"source",
			"status",
			"started_at",
			"completed_at",
			"duration_seconds",
			"local_path",
			"local_status",
			"local_bytes",
			"artifact_manifest",
			"remote_status",
			"restic_snapshot_id",
			"remote_verified_at",
			"error_summary",
			"release_identity",
		],
		order_by="creation desc",
		limit_page_length=0,
	)
	for run in runs:
		run.local_status = _effective_local_status(run)
	items = _backup_inventory(runs, _native_backups(), _deployment_backups())
	page = min(max(1, cint(page)), max(1, (len(items) + 9) // 10))
	return {
		"settings": _settings_payload(settings),
		"configuration": {
			"restic_ready": restic_is_configured(),
			"required_variables": [
				{"name": name, "configured": bool(os.environ.get(name))} for name in REQUIRED_RESTIC_ENV
			],
			"offsite_enabled": _env_flag("RESTIC_OFFSITE_BACKUP_ENABLED"),
			"uptime_kuma_ready": bool(os.environ.get("RESTIC_BACKUP_UPTIME_KUMA_URL")),
		},
		"items": items[(page - 1) * 10 : page * 10],
		"page": page,
		"total": len(items),
		"summary": {
			"latest": items[0]["created"] if items else None,
			"local_count": sum(item["local_status"] in {"Available", "Present"} for item in items),
			"last_verified": max(
				(
					str(item["remote_verified_at"])
					for item in items
					if item.get("remote_verified_at") and item["remote_status"] == "Available"
				),
				default=None,
			),
		},
	}


def _backup_inventory(runs: list, native: list, deployment: list) -> list[dict[str, Any]]:
	"""Merge disk and recorded backups without treating a failed deployment as a failed backup."""
	items = {}
	for backup in deployment:
		items[backup["name"]] = {
			**backup,
			"source": "Deployment",
			"local_status": backup["local_status"],
			"remote_status": "Upload recorded" if backup.get("snapshot_id") else "Not recorded",
			"remote_verified_at": None,
			"run_name": None,
		}
	for backup in native:
		items[backup["name"]] = {
			**backup,
			"source": "Native",
			"local_status": "Present",
			"remote_status": "Not recorded",
			"remote_verified_at": None,
			"run_name": None,
			"created": _site_timestamp(backup["modified"]),
		}
	for run in runs:
		key = run.name
		if run.source == "Deployment" and run.local_path:
			key = Path(run.local_path).name
		else:
			try:
				artifacts = json.loads(run.artifact_manifest or "[]")
			except (TypeError, json.JSONDecodeError):
				artifacts = []
			for artifact in artifacts if isinstance(artifacts, list) else []:
				if isinstance(artifact, dict) and artifact.get("kind") == "database":
					key = str(artifact.get("name", "")).removesuffix("-database.sql.gz") or key
		previous = items.get(key, {})
		items[key] = {
			**previous,
			"name": key,
			"run_name": run.name,
			"source": run.source,
			"created": previous.get("created") or str(run.started_at or run.completed_at or run.creation),
			"status": run.status,
			"local_status": run.local_status,
			"remote_status": run.remote_status or "Not Attempted",
			"remote_verified_at": run.remote_verified_at,
			"snapshot_id": run.restic_snapshot_id,
			"release_identity": run.release_identity,
			"bytes": previous.get("bytes") or run.local_bytes or 0,
			"error_summary": run.error_summary,
		}
	return sorted(items.values(), key=lambda item: item["created"], reverse=True)


def _site_timestamp(timestamp: float) -> str:
	from frappe.utils import convert_utc_to_system_timezone

	return str(
		convert_utc_to_system_timezone(datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None))
	)


@frappe.whitelist(methods=["POST"])
def save_settings(values: str | dict[str, Any]) -> dict[str, Any]:
	"""Save operator-editable backup policy without exposing the internal singleton form."""
	_require_system_manager()
	payload = frappe.parse_json(values) if isinstance(values, str) else values
	if not isinstance(payload, dict):
		frappe.throw(_("Backup settings must be an object."))

	settings = frappe.get_single("Restic Backup Settings")
	for fieldname in EDITABLE_SETTING_FIELDS:
		if fieldname in payload:
			settings.set(fieldname, payload[fieldname])
	settings.save()
	return _settings_payload(settings)


def _settings_payload(settings) -> dict[str, Any]:
	return {
		"enabled": cint(settings.enabled),
		"schedule": [
			{
				"days": parse_working_days(row.days),
				"backup_time": str(row.backup_time),
			}
			for row in settings.backup_schedule
		],
		"local_backup_limit": cint(settings.local_backup_limit) or 4,
		"remote_keep_days": cint(settings.remote_keep_days) or 14,
		"notification_recipients": settings.notification_recipients or "",
		"email_on_success": cint(settings.email_on_success),
	}


def _env_flag(name: str) -> bool:
	return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _effective_local_status(run: frappe._dict) -> str:
	if run.local_status != "Available" or not run.local_path:
		return run.local_status
	root = (Path(frappe.get_site_path()) / run.local_path).resolve()
	site_root = Path(frappe.get_site_path()).resolve()
	if root != site_root and site_root not in root.parents:
		return "Invalid"
	if not root.exists():
		return "Expired"
	try:
		artifacts = json.loads(run.get("artifact_manifest") or "[]")
	except json.JSONDecodeError:
		return "Invalid"
	if (
		not isinstance(artifacts, list)
		or not artifacts
		or any(not isinstance(item, dict) for item in artifacts)
	):
		return "Invalid"
	artifact_names = [str(item.get("name", "")) for item in artifacts]
	if any(not name or Path(name).name != name for name in artifact_names):
		return "Invalid"
	if artifact_names and not all((root / name).is_file() for name in artifact_names):
		return "Expired"
	return "Available"


def _native_backups() -> list[dict[str, Any]]:
	root = Path(get_backup_path()).resolve()
	if not root.is_dir():
		return []
	result = []
	for database in sorted(
		root.glob("*-database.sql.gz"), key=lambda path: path.stat().st_mtime, reverse=True
	):
		prefix = database.name[: -len("-database.sql.gz")]
		artifacts = [path for path in root.glob(f"{prefix}-*") if path.is_file()]
		result.append(
			{
				"name": prefix,
				"database": database.name,
				"artifact_count": len(artifacts),
				"bytes": sum(path.stat().st_size for path in artifacts),
				"modified": database.stat().st_mtime,
			}
		)
	return result


def _deployment_backups() -> list[dict[str, Any]]:
	root = Path(frappe.get_site_path("private", "deployment-backups"))
	if not root.is_dir():
		return []
	result = []
	for directory in sorted((path for path in root.iterdir() if path.is_dir()), reverse=True):
		manifest = {}
		manifest_path = directory / "backup-control.json"
		try:
			manifest = (
				json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
			)
		except (OSError, json.JSONDecodeError):
			manifest = {"status": "Invalid manifest"}
		if not isinstance(manifest, dict):
			manifest = {"status": "Invalid manifest"}
		files = [path for path in directory.iterdir() if path.is_file()]
		artifacts = manifest.get("artifacts")
		local_status = (
			"Present" if any(path.name.endswith("-database.sql.gz") for path in files) else "Invalid"
		)
		if artifacts:
			local_status = _effective_local_status(
				frappe._dict(
					local_status="Available",
					local_path=f"private/deployment-backups/{directory.name}",
					artifact_manifest=json.dumps(artifacts),
				)
			)
		try:
			created = (
				datetime.strptime(directory.name.split("-", 1)[0], "%Y%m%dT%H%M%SZ")
				.replace(tzinfo=timezone.utc)
				.timestamp()
			)
		except ValueError:
			created = directory.stat().st_mtime
		result.append(
			{
				"name": directory.name,
				"created": _site_timestamp(created),
				"status": manifest.get("status") or "Not recorded",
				"local_status": local_status,
				"retention_reason": "Kept after failed deployment"
				if (directory / ".failed").exists()
				else "Kept for recovery"
				if (directory / ".keep").exists()
				else "",
				"snapshot_id": manifest.get("restic_snapshot_id"),
				"release_identity": manifest.get("release_identity"),
				"artifact_count": len([path for path in files if not path.name.startswith(".")]),
				"bytes": sum(path.stat().st_size for path in files),
			}
		)
	return result
