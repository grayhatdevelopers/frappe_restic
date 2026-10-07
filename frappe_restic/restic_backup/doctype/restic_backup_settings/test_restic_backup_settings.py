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
	get_configuration,
)


class TestResticBackupSettings(FrappeTestCase):
	def test_settings_form_edits_the_schedule_with_day_pills_instead_of_the_grid(self) -> None:
		app_path = Path(frappe.get_app_path("frappe_restic"))
		module_path = app_path / "restic_backup"
		form_script = (
			module_path / "doctype" / "restic_backup_settings" / "restic_backup_settings.js"
		).read_text(encoding="utf-8")
		page_script = (module_path / "page" / "restic_backup_control" / "restic_backup_control.js").read_text(
			encoding="utf-8"
		)
		stylesheet = (app_path / "public" / "scss" / "restic_backups.bundle.scss").read_text(encoding="utf-8")
		meta = frappe.get_meta("Restic Backup Settings")

		self.assertTrue(meta.get_field("backup_schedule").hidden)
		self.assertEqual("HTML", meta.get_field("schedule_editor").fieldtype)
		self.assertEqual("HTML", meta.get_field("offsite_configuration").fieldtype)
		self.assertIn("restic-backup-day", form_script)
		self.assertIn('<details class="card restic-backup-offsite">', form_script)
		self.assertIn('frappe.set_route("Form", "Restic Backup Settings")', page_script)
		self.assertNotIn("openSettings", page_script)
		self.assertIn(".restic-backup-settings {", stylesheet)
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

	def test_saving_settings_sorts_the_schedule_by_time(self) -> None:
		workdays = "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday"
		settings = frappe.get_single("Restic Backup Settings")
		settings.update(
			{"enabled": 1, "local_backup_limit": 5, "remote_keep_days": 21, "email_on_success": 1}
		)
		settings.set(
			"backup_schedule",
			[
				{"days": workdays, "backup_time": "12:30"},
				{"days": "Sunday", "backup_time": "02:00:00"},
			],
		)
		settings.save()

		self.assertEqual(
			[(row.days, str(row.backup_time)) for row in settings.backup_schedule],
			[("Sunday", "02:00:00"), (workdays, "12:30:00")],
		)
		self.assertEqual(5, frappe.db.get_single_value("System Settings", "backup_limit"))

	def test_only_system_managers_can_read_the_storage_configuration(self) -> None:
		with patch("frappe.get_roles", return_value=["Sales User"]):
			with self.assertRaises(frappe.PermissionError):
				get_configuration()
		with patch("frappe.get_roles", return_value=["System Manager"]):
			configuration = get_configuration()
		self.assertIn("RESTIC_REPOSITORY", [row["name"] for row in configuration["required_variables"]])
		self.assertTrue(
			all(set(row) == {"name", "configured"} for row in configuration["required_variables"])
		)

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
