"""Backup settings regressions."""

from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.model.document import Document
from frappe.tests.utils import FrappeTestCase

from frappe_restic.restic_backup.backup_control import (
	_require_system_manager,
	schedule_due_backups,
)
from frappe_restic.restic_backup.doctype.restic_backup_settings.restic_backup_settings import (
	parse_backup_schedule,
)
from frappe_restic.restic_backup.page.restic_backup_control.restic_backup_control import (
	save_settings,
)


class TestResticBackupSettings(FrappeTestCase):
	def test_settings_use_compact_schedule_editor_instead_of_frappe_grid(self) -> None:
		app_path = Path(frappe.get_app_path("frappe_restic"))
		page_script = (
			app_path / "restic_backup" / "page" / "restic_backup_control" / "restic_backup_control.js"
		).read_text(encoding="utf-8")
		stylesheet = (app_path / "public" / "scss" / "restic_backups.bundle.scss").read_text(encoding="utf-8")

		self.assertNotIn('fieldtype: "Table"', page_script)
		self.assertNotIn('fieldtype: "MultiSelectPills"', page_script)
		self.assertIn("renderScheduleEditor", page_script)
		self.assertIn("setScheduleEditorVisibility", page_script)
		self.assertIn("Boolean(Number(values.enabled))", page_script)
		self.assertIn('<details class="card restic-backup-offsite">', page_script)
		self.assertIn("restic-backup-day", page_script)
		self.assertIn("restic-backup-offsite-summary", stylesheet)
		self.assertIn("restic-backup-schedule-row", stylesheet)
		self.assertIn("overflow-wrap: anywhere", stylesheet)

	def test_defaults_expand_to_independent_schedule_rows(self) -> None:
		settings = frappe.get_doc({"doctype": "Restic Backup Settings"})
		load = Document.load_from_db

		def load_other_documents(document):
			if document is not settings:
				return load(document)

		with patch.object(Document, "load_from_db", new=load_other_documents):
			settings.load_from_db()

		self.assertEqual(2, len(settings.backup_schedule))
		self.assertEqual(
			"Monday,Tuesday,Wednesday,Thursday,Friday,Saturday",
			settings.backup_schedule[0].days,
		)
		self.assertEqual("13:00:00", str(settings.backup_schedule[0].backup_time))
		self.assertEqual("20:00:00", str(settings.backup_schedule[-1].backup_time))

	def test_backup_control_is_system_manager_only(self) -> None:
		settings_roles = {
			permission.role for permission in frappe.get_meta("Restic Backup Settings").permissions
		}
		run_roles = {permission.role for permission in frappe.get_meta("Restic Backup Run").permissions}
		page_roles = {row.role for row in frappe.get_doc("Page", "restic-backup-control").roles}

		self.assertEqual({"System Manager"}, settings_roles)
		self.assertEqual({"System Manager"}, run_roles)
		self.assertEqual({"System Manager"}, page_roles)
		self.assertFalse(frappe.db.exists("Role", "Restic Backup Manager"))

	def test_non_system_manager_cannot_call_backup_endpoints(self) -> None:
		with patch("frappe.get_roles", return_value=["Sales User"]):
			with self.assertRaises(frappe.PermissionError):
				_require_system_manager()

	def test_system_manager_can_call_backup_endpoints(self) -> None:
		with patch("frappe.get_roles", return_value=["System Manager"]):
			_require_system_manager()

	def test_system_manager_can_save_settings_from_backup_control(self) -> None:
		values = {
			"enabled": 1,
			"backup_schedule": [
				{
					"days": [
						"Monday",
						"Tuesday",
						"Wednesday",
						"Thursday",
						"Friday",
						"Saturday",
					],
					"backup_time": "12:30:00",
				},
				{"days": ["Sunday"], "backup_time": "02:00:00"},
			],
			"local_backup_limit": 5,
			"remote_keep_days": 21,
			"email_on_success": 1,
		}

		with patch("frappe.get_roles", return_value=["System Manager"]):
			result = save_settings(values)

		self.assertEqual(
			result["schedule"],
			[
				{"days": ["Sunday"], "backup_time": "02:00:00"},
				{
					"days": [
						"Monday",
						"Tuesday",
						"Wednesday",
						"Thursday",
						"Friday",
						"Saturday",
					],
					"backup_time": "12:30:00",
				},
			],
		)
		self.assertEqual(result["local_backup_limit"], 5)
		self.assertEqual(result["remote_keep_days"], 21)
		self.assertEqual(result["email_on_success"], 1)

	def test_schedule_rejects_duplicate_day_and_time(self) -> None:
		rows = [
			frappe._dict(days=["Sunday"], backup_time="02:00:00"),
			frappe._dict(days=["Saturday", "Sunday"], backup_time="02:00:00"),
		]

		with self.assertRaises(frappe.ValidationError):
			parse_backup_schedule(rows)

	def test_sunday_only_row_does_not_require_individual_workday_rows(self) -> None:
		settings = frappe._dict(
			enabled=1,
			backup_schedule=[
				frappe._dict(
					days="Monday,Tuesday,Wednesday,Thursday,Friday,Saturday",
					backup_time="13:00:00",
				),
				frappe._dict(days="Sunday", backup_time="02:00:00"),
			],
		)

		with (
			patch("frappe.get_single", return_value=settings),
			patch(
				"frappe_restic.restic_backup.backup_control.now_datetime",
				return_value=datetime(2026, 9, 20, 2, 5),
			),
			patch("frappe_restic.restic_backup.backup_control._enqueue_backup") as enqueue_backup,
		):
			schedule_due_backups()

		enqueue_backup.assert_called_once()
		self.assertEqual("Scheduled", enqueue_backup.call_args.kwargs["source"])
