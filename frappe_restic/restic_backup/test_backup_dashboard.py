"""Backup history must agree with both disk inventory and later repository checks."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import frappe

from frappe_restic.restic_backup.page.restic_backup_control import restic_backup_control as dashboard


class TestBackupDashboard(TestCase):
	def test_deployment_without_run_is_visible_and_not_reported_as_failed_backup(self) -> None:
		backup = dict(
			name="20260918T103501Z-test",
			created="2026-09-18 15:35:01",
			status="Succeeded",
			snapshot_id="abc123",
			local_status="Available",
			retention_reason="Kept after failed deployment",
		)
		items = dashboard._backup_inventory([], [], [backup])
		self.assertEqual(items[0]["status"], "Succeeded")
		self.assertEqual(items[0]["remote_status"], "Upload recorded")
		self.assertIsNone(items[0]["remote_verified_at"])

	def test_reconciliation_overrides_stale_manifest_without_duplicate_row(self) -> None:
		backup = dict(
			name="deployment-one",
			created="2026-09-18 10:00:00",
			status="Succeeded",
			snapshot_id="abc123",
			local_status="Available",
		)
		run = frappe._dict(
			name="run-one",
			source="Deployment",
			local_path="private/deployment-backups/deployment-one",
			local_status="Available",
			status="Succeeded",
			restic_snapshot_id="abc123",
			remote_status="Missing",
			remote_verified_at="2026-09-18 11:00:00",
			creation="2026-09-18 12:00:00",
		)
		items = dashboard._backup_inventory([run], [], [backup])
		self.assertEqual(len(items), 1)
		self.assertEqual(items[0]["remote_status"], "Missing")
		self.assertEqual(items[0]["created"], backup["created"])

	def test_native_backup_matches_its_run_by_database_not_shared_directory(self) -> None:
		native = [dict(name="one", modified=1, bytes=10), dict(name="two", modified=2, bytes=20)]
		run = frappe._dict(
			name="manual-run",
			source="Manual",
			local_path="private/backups",
			status="Succeeded",
			local_status="Available",
			remote_status="Available",
			artifact_manifest=json.dumps([dict(kind="database", name="two-database.sql.gz")]),
		)
		with patch.object(dashboard, "_site_timestamp", side_effect=lambda value: str(value)):
			items = dashboard._backup_inventory([run], native, [])
		self.assertEqual(len(items), 2)
		self.assertEqual(items[0]["name"], "two")
		self.assertEqual(items[0]["source"], "Manual")

	def test_failed_deployment_marker_is_retention_not_backup_result(self) -> None:
		with TemporaryDirectory() as temporary:
			root = Path(temporary)
			folder = root / "private/deployment-backups/20260918T103501Z-test"
			folder.mkdir(parents=True)
			(folder / ".failed").touch()
			(folder / "backup-control.json").write_text(json.dumps({"status": "Succeeded"}))
			with (
				patch("frappe.get_site_path", side_effect=lambda *parts: str(root.joinpath(*parts))),
				patch.object(dashboard, "_site_timestamp", return_value="2026-09-18 15:35:01"),
			):
				backup = dashboard._deployment_backups()[0]
			self.assertEqual(backup["status"], "Succeeded")
			self.assertEqual(backup["retention_reason"], "Kept after failed deployment")
			self.assertEqual(backup["local_status"], "Invalid")

	def test_history_paginates_past_fifty_and_summary_does_not_change_with_page(self) -> None:
		backups = [
			dict(
				name=str(index),
				created=f"2026-09-18 10:{index:02d}:00",
				local_status="Present",
				snapshot_id=None,
			)
			for index in range(55)
		]
		settings = frappe._dict(backup_schedule=[])
		with (
			patch.object(dashboard, "_require_system_manager"),
			patch("frappe.get_single", return_value=settings),
			patch("frappe.get_all", return_value=[]),
			patch.object(dashboard, "_native_backups", return_value=[]),
			patch.object(dashboard, "_deployment_backups", return_value=backups),
		):
			first = dashboard.get_dashboard(page=1)
			last = dashboard.get_dashboard(page=6)
		self.assertEqual(first["total"], 55)
		self.assertEqual(len(first["items"]), 10)
		self.assertEqual(len(last["items"]), 5)
		self.assertEqual(first["summary"], last["summary"])
		self.assertEqual(first["summary"]["local_count"], 55)
