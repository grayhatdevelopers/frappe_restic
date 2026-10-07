"""Regressions for scheduled backups and durable inventory state."""

import os
import tempfile
from datetime import datetime
from pathlib import Path
from unittest import TestCase
from unittest.mock import MagicMock, patch

import frappe

from frappe_restic.restic_backup import backup_control as control
from frappe_restic.restic_backup.recovery import read_json, write_json


class TestBackupControl(TestCase):
	def setUp(self) -> None:
		clock = patch.object(control, "now_datetime", return_value=datetime(2026, 9, 22, 0, 2))
		clock.start()
		self.addCleanup(clock.stop)

	def test_previous_day_schedule_runs_in_midnight_grace_window(self) -> None:
		settings = frappe._dict(
			enabled=1,
			backup_schedule=[frappe._dict(days="Monday", backup_time="23:55:00")],
		)
		with (
			patch("frappe.get_single", return_value=settings),
			patch.object(control, "now_datetime", return_value=datetime(2026, 9, 22, 0, 2)),
			patch.object(control, "_enqueue_backup") as enqueue,
		):
			control.schedule_due_backups()
		self.assertEqual(enqueue.call_count, 1)
		self.assertEqual(enqueue.call_args.kwargs["scheduled_for"], datetime(2026, 9, 21, 23, 55))

	def test_inventory_overrides_stale_deployment_manifest(self) -> None:
		runs = []

		def import_manifest():
			runs.append(frappe._dict(name="deployment", restic_snapshot_id="abc123"))
			return 1

		with (
			patch.object(control, "restic_is_configured", return_value=True),
			patch.object(control, "list_snapshots", return_value=[]),
			patch.object(control, "_site_tag", return_value="frappe-site:erp.local"),
			patch.object(control, "_import_deployment_manifests", side_effect=import_manifest),
			patch("frappe.get_all", side_effect=lambda *args, **kwargs: list(runs)),
			patch("frappe.db", new_callable=MagicMock) as database,
		):
			result = control._reconcile_remote_unlocked()
		self.assertEqual(result, {"remote_runs": 1, "deployment_runs": 1})
		self.assertEqual(database.set_value.call_args.args[2]["remote_status"], "Missing")

	def test_enqueue_failure_does_not_leave_run_queued(self) -> None:
		run = MagicMock()
		run.name = "run123"
		with (
			patch("frappe.db", new_callable=MagicMock) as database,
			patch("frappe.get_doc") as get_doc,
			patch("frappe.log_error"),
			patch("frappe.get_traceback", return_value="queue unavailable"),
			patch.object(control, "enqueue", side_effect=RuntimeError("queue unavailable")),
			patch.object(control, "_fail_run") as fail,
		):
			database.get_value.return_value = None
			get_doc.return_value.insert.return_value = run
			with self.assertRaisesRegex(RuntimeError, "queue unavailable"):
				control._enqueue_backup(run_key="manual:test", source="Manual")
		fail.assert_called_once_with("run123", "RuntimeError: queue unavailable")

	def test_retention_failure_preserves_successful_remote_backup(self) -> None:
		lock = MagicMock()
		with (
			patch.dict(os.environ, {"RESTIC_OFFSITE_BACKUP_ENABLED": "1"}),
			patch.object(control, "_new_backup_lock", return_value=lock),
			patch.object(control, "_set_run") as set_run,
			patch.object(control, "_create_full_native_backup", return_value={"database": "/backup/db"}),
			patch.object(control, "validate_backup_set", return_value=[]),
			patch.object(control, "_relative_site_path", return_value="private/backups"),
			patch.object(control, "restic_is_configured", return_value=True),
			patch.object(control, "stable_staging_tree"),
			patch.object(control, "snapshot_manifest"),
			patch.object(control, "_stable_host", return_value="frappe-test"),
			patch.object(control, "_site_tag", return_value="frappe-site:test"),
			patch.object(control, "_run_source", return_value="Manual"),
			patch.object(control, "backup_with_restic", return_value="abc123"),
			patch.object(control, "_settings_int", return_value=4),
			patch.object(control, "_prune_local_sets", side_effect=OSError("read-only filesystem")),
			patch.object(control, "_notify_run"),
			patch.object(control, "_push_heartbeat"),
			patch.object(control, "_fail_run") as fail,
			patch("frappe.get_site_path", return_value="/site"),
			patch("frappe.logger"),
		):
			control.execute_backup("run123")
		fail.assert_not_called()
		self.assertTrue(any(call.kwargs.get("status") == "Succeeded" for call in set_run.call_args_list))

	def test_local_backup_succeeds_without_reading_credentials_or_contacting_remote(self) -> None:
		with (
			patch.dict(os.environ, {"RESTIC_OFFSITE_BACKUP_ENABLED": "0"}),
			patch.object(control, "_new_backup_lock", return_value=MagicMock()),
			patch.object(control, "_set_run") as updates,
			patch.object(control, "_create_full_native_backup", return_value={"database": "/local/db"}),
			patch.object(control, "validate_backup_set", return_value=[]),
			patch.object(control, "_relative_site_path", return_value="private/backups"),
			patch.object(control, "_prune_completed_local_backup") as retention,
			patch.object(control, "_notify_run"),
			patch.object(control, "restic_is_configured") as credentials,
			patch.object(control, "backup_with_restic") as upload,
			patch.object(control, "_push_heartbeat") as heartbeat,
		):
			control.execute_backup("local-test")
		credentials.assert_not_called()
		upload.assert_not_called()
		heartbeat.assert_not_called()
		retention.assert_called_once()
		self.assertTrue(
			any(
				call.kwargs.get("status") == "Succeeded"
				and call.kwargs.get("remote_status") == "Not Attempted"
				for call in updates.call_args_list
			)
		)

	def test_disabled_offsite_blocks_queued_and_scheduled_remote_maintenance(self) -> None:
		with (
			patch.dict(os.environ, {"RESTIC_OFFSITE_BACKUP_ENABLED": "0"}),
			patch.object(control, "enqueue") as enqueue,
			patch.object(control, "run_maintenance") as maintenance,
			patch.object(control, "restic_is_configured") as credentials,
		):
			control.daily_maintenance()
			control.weekly_maintenance()
			control.maintenance_job(weekly=True)
		enqueue.assert_not_called()
		maintenance.assert_not_called()
		credentials.assert_not_called()

	def test_failures_recorded_while_stopped_are_alerted_once(self) -> None:
		with tempfile.TemporaryDirectory() as directory:
			receipts = Path(directory)
			write_json(
				receipts / "rolled-back.json",
				{"status": "Rolled Back", "snapshot": "abc", "error": "migrate failed"},
			)
			write_json(receipts / "completed.json", {"status": "Completed", "snapshot": "abc"})
			with (
				patch.object(control, "_receipt_directory", return_value=receipts),
				patch.object(control, "_import_deployment_manifests"),
				patch("frappe.get_all", side_effect=[["deployment-run"], []]) as runs,
				patch.object(control, "_notify_run") as notify,
				patch.object(control, "_send_alert", return_value="Sent") as alert,
				patch.object(control, "_notify_system_managers") as desk,
				patch("frappe.log_error"),
			):
				control.report_operation_outcomes()
				control.report_operation_outcomes()
			self.assertEqual(runs.call_args.kwargs["filters"]["notification_status"], ["is", "not set"])
			notify.assert_called_once_with("deployment-run", succeeded=False)
			alert.assert_called_once()
			desk.assert_called_once_with(*alert.call_args.args)
			self.assertIn("returned to its state before the restore", alert.call_args.args[1])
			self.assertEqual(read_json(receipts / "rolled-back.json")["notification_status"], "Sent")
			self.assertNotIn("notification_status", read_json(receipts / "completed.json"))

	def test_failed_run_reaches_every_system_manager_in_the_desk(self) -> None:
		run = frappe._dict(name="run123", source="Scheduled", status="Failed", error_summary="disk full")
		with (
			patch("frappe.get_doc", return_value=run),
			patch("frappe.get_all", return_value=["admin@example.com", "ops@example.com"]) as users,
			patch.object(control, "get_users_with_role", return_value=["Administrator", "ops@example.com"]),
			patch.object(control, "enqueue_create_notification") as notify,
			patch.object(control, "_send_alert", return_value="Not requested"),
			patch.object(control, "_set_run") as set_run,
		):
			control._notify_run("run123", succeeded=False)
			control._notify_run("run123", succeeded=True)
		self.assertEqual(users.call_args.kwargs["filters"], {"name": ["in", ["Administrator", "ops@example.com"]]})
		notify.assert_called_once()
		recipients, notification = notify.call_args.args
		self.assertEqual(recipients, ["admin@example.com", "ops@example.com"])
		self.assertEqual(notification["type"], "Alert")
		self.assertIn("FAILED", notification["subject"])
		self.assertIn("disk full", notification["email_content"])
		self.assertEqual(
			(notification["document_type"], notification["document_name"]),
			("Restic Backup Run", "run123"),
		)
		set_run.assert_called_with("run123", notification_status="Not requested")

	def test_desk_notification_failure_does_not_block_the_email(self) -> None:
		run = frappe._dict(name="run123", source="Manual", status="Failed")
		with (
			patch("frappe.get_doc", return_value=run),
			patch.object(control, "get_users_with_role", side_effect=RuntimeError("database gone")),
			patch("frappe.logger"),
			patch.object(control, "_send_alert", return_value="Sent") as alert,
			patch.object(control, "_set_run") as set_run,
		):
			control._notify_run("run123", succeeded=False)
		alert.assert_called_once()
		set_run.assert_called_once_with("run123", notification_status="Sent")

	def test_schedule_checkbox_does_not_disable_retention_for_manual_remote_backups(self) -> None:
		with (
			patch.dict(os.environ, {"RESTIC_OFFSITE_BACKUP_ENABLED": "1"}),
			patch.object(control, "restic_is_configured", return_value=True),
			patch("frappe.get_single", return_value=frappe._dict(enabled=0, remote_keep_days=14)),
			patch.object(control, "_new_backup_lock", return_value=MagicMock()),
			patch.object(control, "_site_tag", return_value="frappe-site:erp.test"),
			patch.object(control, "run_maintenance") as maintenance,
			patch.object(control, "_reconcile_remote_unlocked"),
		):
			control.maintenance_job()
		maintenance.assert_called_once()
