"""Exercise real recovery sequencing against temporary files and stubbed commands."""

import fcntl
import gzip
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from frappe_restic.restic_backup import recovery

# Restore tests patch the database check out; its own test needs the real one.
check_database_root = recovery.check_database_root


class TestRestoredEncryption(unittest.TestCase):
	def test_missing_key_is_allowed_only_without_encrypted_secrets(self) -> None:
		for rows, config, rejected in (
			([], {}, False),
			([(1,)], {}, True),
			([(1,)], {"encryption_key": "original"}, False),
		):
			with self.subTest(rows=rows, config=config):
				frappe = SimpleNamespace(conf=config, db=SimpleNamespace(sql=Mock(return_value=rows)))
				with patch.dict(sys.modules, {"frappe": frappe}):
					if rejected:
						with self.assertRaisesRegex(ValueError, "original key"):
							recovery.validate_restored_encryption()
					else:
						recovery.validate_restored_encryption()
				if not config:
					frappe.db.sql.assert_called_once_with(
						"SELECT 1 FROM `__Auth` WHERE encrypted = 1 LIMIT 1"
					)


class TestRecoveryIdentity(unittest.TestCase):
	def test_restart_keeps_attempt_and_new_container_changes_it(self) -> None:
		with patch.object(recovery.socket, "gethostname", return_value="container-one"):
			first = recovery.recovery_attempt("erp.test")
			self.assertEqual(first, recovery.recovery_attempt("erp.test"))
			self.assertNotEqual(first, recovery.recovery_attempt("other.test"))
		with patch.object(recovery.socket, "gethostname", return_value="container-two"):
			self.assertNotEqual(first, recovery.recovery_attempt("erp.test"))


class TestRecovery(unittest.TestCase):
	def setUp(self) -> None:
		self.temporary = tempfile.TemporaryDirectory()
		self.addCleanup(self.temporary.cleanup)
		self.root = Path(self.temporary.name)
		self.site = self.root / "sites" / "erp.test"
		self.site.mkdir(parents=True)
		recovery.write_json(
			self.site / "site_config.json", {"db_name": "current_db", "db_password": "current-password"}
		)
		for kind in ("public", "private"):
			(self.site / kind / "files").mkdir(parents=True)
			(self.site / kind / "files" / "newer.txt").write_text("must disappear")
		self.commit = "a" * 40
		self.commands = []
		self.failure = None
		self.rollback_failure = False
		self.manifest_commit = self.commit
		self.manifest_site = "erp.test"
		self.environment = patch.dict(
			os.environ,
			{
				"SITE_NAME": "erp.test",
				"RESTIC_RESTORE_SNAPSHOT": "b" * 64,
				"DB_ROOT_PASSWORD": "unused-test-password",
				"RESTIC_CONFIGURE_EXECUTABLE": "/usr/local/bin/configure-bench",
			},
		)
		self.attempt = patch.object(recovery, "recovery_attempt", return_value="restore-test-001")
		self.attempt.start()
		self.addCleanup(self.attempt.stop)
		self.environment.start()
		self.addCleanup(self.environment.stop)
		self.identity = patch.object(recovery, "image_commit", return_value=self.commit)
		self.identity.start()
		self.addCleanup(self.identity.stop)
		self.runner = patch.object(recovery, "run", side_effect=self.run_command)
		self.runner.start()
		self.addCleanup(self.runner.stop)
		self.database_root = patch.object(recovery, "check_database_root")
		self.database_root.start()
		self.addCleanup(self.database_root.stop)

	def run_command(self, command: list[str], **kwargs) -> None:
		self.commands.append(command)
		if command[0] == "restic":
			stage = Path(command[command.index("--target") + 1]) / "snapshot"
			stage.mkdir()
			(stage / "database.sql").write_text("SELECT 1;")
			recovery.write_json(
				stage / "site_config.json",
				{
					"encryption_key": "original-key",
					"db_host": "old-host",
					"db_user": "old-user",
					"db_password": "old-password",
				},
			)
			for kind in ("public", "private"):
				files = stage / kind / "erp.test" / kind / "files"
				files.mkdir(parents=True)
				(files / "restored.txt").write_text("original upload")
			recovery.snapshot_manifest(stage, site=self.manifest_site, commit=self.manifest_commit)
		if command[:2] == ["bench", "--site"] and "backup" in command:
			self.write_native_backup(Path(command[command.index("--backup-path") + 1]))
		if self.failure and self.failure in command:
			raise RuntimeError("simulated operation failure")
		if (
			self.rollback_failure
			and "restore-database" in command
			and ".frappe-restic-rollback-stage" in command[-1]
		):
			raise RuntimeError("simulated rollback failure")

	def write_native_backup(self, directory: Path) -> None:
		"""Write the set `bench backup --with-files` produces for the current site."""
		prefix = directory / "20260928_000000-erp_test"
		with gzip.open(f"{prefix}-database.sql.gz", "wb") as stream:
			stream.write(b"SELECT 'safety';")
		shutil.copy(self.site / "site_config.json", f"{prefix}-site_config_backup.json")
		for kind, suffix in (("public", "files"), ("private", "private-files")):
			with tarfile.open(f"{prefix}-{suffix}.tar", "w") as archive:
				archive.add(self.site / kind / "files", arcname=f"erp.test/{kind}/files")

	def receipt(self) -> dict:
		return recovery.read_json(self.root / "sites/.frappe-recovery/erp.test/restore-test-001.json")

	def database_restores(self) -> list[str]:
		return [command[-1] for command in self.commands if "restore-database" in command]

	def test_restore_replaces_files_preserves_keys_and_migrates_before_completion(self) -> None:
		recovery.restore_site(self.root)
		config = recovery.read_json(self.site / "site_config.json")
		self.assertEqual(config["encryption_key"], "original-key")
		self.assertEqual(config["db_name"], "current_db")
		self.assertEqual(config["db_password"], "current-password")
		# Connection comes from common_site_config, never from the backup or a hard-coded host.
		self.assertNotIn("db_host", config)
		self.assertNotIn("db_port", config)
		# v16 separates the database user; the backup's user does not exist on this server.
		self.assertNotIn("db_user", config)
		self.assertEqual(config["maintenance_mode"], 0)
		for kind in ("public", "private"):
			self.assertFalse((self.site / kind / "files" / "newer.txt").exists())
			self.assertTrue((self.site / kind / "files" / "restored.txt").is_file())
		migrated = self.commands.index(["bench", "--site", "erp.test", "migrate"])
		purged = self.commands.index(["bench", "purge-jobs", "--site", "erp.test"])
		installed = self.commands.index(
			[sys.executable, "-m", "frappe_restic.deployment", "install", "--site", "erp.test"]
		)
		cleared = self.commands.index(["bench", "--site", "erp.test", "clear-cache"])
		self.assertLess(purged, migrated)
		# A previous release's cached module map breaks migrate.
		stale_cache = self.commands.index(
			["bench", "--site", "erp.test", "execute", "frappe.cache_manager.clear_global_cache"]
		)
		self.assertLess(purged, stale_cache)
		self.assertLess(stale_cache, migrated)
		# An older release's schema cannot take an app install before it is migrated.
		self.assertLess(migrated, installed)
		self.assertLess(installed, cleared)
		self.assertTrue(any("backup" in command for command in self.commands))
		self.assertTrue(any("purge-jobs" in command for command in self.commands))
		self.assertEqual(recovery.deployed_commit(self.site), self.commit)
		self.assertFalse((self.root / "sites/.frappe-recovery-blocked").exists())

	def test_site_specific_database_connection_is_preserved(self) -> None:
		recovery.write_json(
			self.site / "site_config.json",
			{
				"db_name": "current_db",
				"db_password": "current-password",
				"db_host": "mariadb.internal",
				"db_port": 3307,
			},
		)
		recovery.restore_site(self.root)
		config = recovery.read_json(self.site / "site_config.json")
		self.assertEqual(config["db_host"], "mariadb.internal")
		self.assertEqual(config["db_port"], 3307)

	def test_completed_request_does_not_restore_twice(self) -> None:
		recovery.restore_site(self.root)
		self.commands.clear()
		recovery.restore_site(self.root)
		self.assertEqual(self.commands, [])

	def test_new_restore_job_can_restore_again_without_operator_identifier(self) -> None:
		recovery.restore_site(self.root)
		self.commands.clear()
		with patch.object(recovery, "recovery_attempt", return_value="new-container-attempt"):
			recovery.restore_site(self.root)
		self.assertTrue(any(command[0] == "restic" for command in self.commands))
		self.assertTrue((self.root / "sites/.frappe-recovery/erp.test/new-container-attempt.json").exists())

	def test_retired_safety_environment_cannot_disable_backup(self) -> None:
		with patch.dict(os.environ, {"RESTIC_RESTORE_SKIP_SAFETY_BACKUP": "1"}):
			recovery.restore_site(self.root)
		self.assertTrue(any("backup" in command for command in self.commands))

	def test_explicit_emergency_console_option_can_skip_safety_backup(self) -> None:
		recovery.restore_site(self.root, skip_safety_backup=True)
		self.assertFalse(any("backup" in command for command in self.commands))

	def test_latest_is_pinned_and_completed_restart_does_not_resolve_again(self) -> None:
		with (
			patch.dict(os.environ, {"RESTIC_RESTORE_SNAPSHOT": "latest"}),
			patch.object(recovery, "latest_snapshot", return_value="d" * 64) as latest,
		):
			recovery.restore_site(self.root)
			receipt = recovery.read_json(self.root / "sites/.frappe-recovery/erp.test/restore-test-001.json")
			self.assertEqual(receipt["snapshot"], "d" * 64)
			self.assertEqual(receipt["snapshot_selector"], "latest")
			self.commands.clear()
			recovery.restore_site(self.root)
			latest.assert_called_once_with("erp.test")
			self.assertEqual(self.commands, [])

	def test_latest_resolution_failure_does_not_touch_site(self) -> None:
		with (
			patch.dict(os.environ, {"RESTIC_RESTORE_SNAPSHOT": "latest"}),
			patch.object(recovery, "latest_snapshot", side_effect=ValueError("No backups")),
			self.assertRaisesRegex(ValueError, "No backups"),
		):
			recovery.restore_site(self.root)
		self.assertEqual(self.commands, [])
		self.assertTrue((self.site / "public/files/newer.txt").exists())

	def test_wrong_database_root_password_touches_nothing_and_can_be_retried(self) -> None:
		with (
			patch.object(recovery, "check_database_root", side_effect=ValueError("DB_ROOT_PASSWORD")),
			self.assertRaisesRegex(ValueError, "DB_ROOT_PASSWORD"),
		):
			recovery.restore_site(self.root)
		self.assertEqual(self.commands, [])
		self.assertFalse((self.root / "sites/.frappe-recovery-blocked").exists())
		self.assertFalse((self.root / "sites/.frappe-recovery/erp.test/restore-test-001.json").exists())
		recovery.restore_site(self.root)
		self.assertEqual(self.receipt()["status"], "Completed")

	def test_database_root_check_uses_site_connection_and_hides_password(self) -> None:
		try:
			import MySQLdb as driver
		except ImportError:
			import pymysql as driver

		recovery.write_json(self.root / "sites/common_site_config.json", {"db_host": "db", "db_port": 3307})
		denied = driver.OperationalError(1045, "Access denied for user 'root'")
		with (
			patch.object(driver, "connect", side_effect=denied) as connect,
			self.assertRaises(ValueError) as raised,
		):
			check_database_root(self.root / "sites", "erp.test")
		self.assertEqual(connect.call_args.kwargs["host"], "db")
		self.assertEqual(connect.call_args.kwargs["port"], 3307)
		self.assertEqual(connect.call_args.kwargs["user"], "root")
		self.assertIn("1045", str(raised.exception))
		self.assertNotIn("unused-test-password", str(raised.exception))

	def test_latest_selection_is_by_time_and_scoped_to_site(self) -> None:
		entries = [
			{"id": "a" * 64, "time": "2026-09-16T12:00:00Z", "tags": ["frappe-site:erp.test"]},
			{"id": "b" * 64, "time": "2026-09-17T12:00:00Z", "tags": ["frappe-site:erp.test"]},
			{"id": "c" * 64, "time": "2026-09-18T12:00:00Z", "tags": ["frappe-site:another"]},
		]
		with patch.object(
			recovery.subprocess,
			"run",
			return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(entries)),
		) as listing:
			self.assertEqual(recovery.latest_snapshot("erp.test"), "b" * 64)
			self.assertEqual(listing.call_args.args[0][-2:], ["--tag", "frappe-site:erp.test"])
		with patch.object(
			recovery.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, stdout="[]")
		):
			with self.assertRaisesRegex(ValueError, "No backups"):
				recovery.latest_snapshot("erp.test")

	def test_fresh_volume_configures_redis_before_purging_jobs(self) -> None:
		shutil.rmtree(self.site)
		recovery.restore_site(self.root)
		configured = self.commands.index(["/usr/local/bin/configure-bench"])
		purged = self.commands.index(["bench", "purge-jobs", "--site", "erp.test"])
		self.assertLess(configured, purged)
		self.assertFalse(any("backup" in command for command in self.commands))

	def test_wrong_site_does_not_touch_target(self) -> None:
		self.manifest_site = "other.test"
		with self.assertRaises(ValueError):
			recovery.restore_site(self.root)
		self.assertTrue((self.site / "public/files/newer.txt").exists())
		self.assertFalse((self.root / "sites/.frappe-recovery-blocked").exists())
		self.assertTrue(all(command[0] == "restic" for command in self.commands))

	def test_different_backup_commit_is_migrated_and_recorded_separately(self) -> None:
		self.manifest_commit = "c" * 40
		recovery.restore_site(self.root)
		receipt = recovery.read_json(self.root / "sites/.frappe-recovery/erp.test/restore-test-001.json")
		self.assertEqual(receipt["backup_commit"], self.manifest_commit)
		self.assertEqual(receipt["commit"], self.commit)
		self.assertIn(["bench", "--site", "erp.test", "migrate"], self.commands)

	def test_unknown_backup_revision_does_not_prevent_data_recovery(self) -> None:
		self.manifest_commit = "unknown"
		recovery.restore_site(self.root)
		self.assertEqual(recovery.deployed_commit(self.site), self.commit)

	def test_safety_backup_failure_leaves_running_site_untouched(self) -> None:
		self.failure = "--backup-path"
		with self.assertRaises(RuntimeError):
			recovery.restore_site(self.root)
		self.assertFalse((self.root / "sites/.frappe-recovery-blocked").exists())
		self.assertEqual(
			recovery.read_json(self.site / "site_config.json"),
			{"db_name": "current_db", "db_password": "current-password"},
		)
		self.assertTrue((self.site / "public/files/newer.txt").exists())
		self.assertEqual(self.database_restores(), [])
		self.assertEqual(self.receipt()["status"], "Failed")
		with self.assertRaisesRegex(ValueError, "already consumed"):
			recovery.restore_site(self.root)

	def test_failure_after_replacing_data_returns_site_to_safety_backup(self) -> None:
		original = recovery.read_json(self.site / "site_config.json")
		self.failure = "migrate"
		with self.assertRaisesRegex(RuntimeError, "simulated operation failure"):
			recovery.restore_site(self.root)
		self.assertFalse((self.root / "sites/.frappe-recovery-blocked").exists())
		self.assertEqual(recovery.read_json(self.site / "site_config.json"), original)
		for kind in ("public", "private"):
			self.assertTrue((self.site / kind / "files" / "newer.txt").is_file())
			self.assertFalse((self.site / kind / "files" / "restored.txt").exists())
		self.assertEqual(len(self.database_restores()), 2)
		self.assertIn(".frappe-restic-rollback-stage", self.database_restores()[1])
		self.assertEqual(self.receipt()["status"], "Rolled Back")
		self.assertIn("simulated operation failure", self.receipt()["error"])
		self.assertEqual(recovery.deployed_commit(self.site), "unknown")
		with self.assertRaisesRegex(ValueError, "already consumed"):
			recovery.restore_site(self.root)

	def test_failed_rollback_blocks_startup(self) -> None:
		self.failure = "migrate"
		self.rollback_failure = True
		with self.assertRaisesRegex(RuntimeError, "simulated operation failure"):
			recovery.restore_site(self.root)
		self.assertTrue((self.root / "sites/.frappe-recovery-blocked").exists())
		self.assertEqual(recovery.read_json(self.site / "site_config.json")["maintenance_mode"], 1)
		self.assertEqual(self.receipt()["status"], "Rollback Failed")

	def test_fresh_volume_failure_blocks_startup_and_does_not_record_release(self) -> None:
		shutil.rmtree(self.site)
		self.failure = "migrate"
		with self.assertRaises(RuntimeError):
			recovery.restore_site(self.root)
		self.assertTrue((self.root / "sites/.frappe-recovery-blocked").exists())
		self.assertEqual(recovery.read_json(self.site / "site_config.json")["maintenance_mode"], 1)
		self.assertEqual(recovery.deployed_commit(self.site), "unknown")
		self.assertEqual(self.receipt()["status"], "Started")
		with self.assertRaisesRegex(ValueError, "already consumed"):
			recovery.restore_site(self.root)

	def test_site_already_blocked_is_not_rolled_back_or_unblocked(self) -> None:
		recovery.write_json(self.root / "sites/.frappe-recovery-blocked", {"site": "erp.test"})
		self.failure = "restore-database"
		with self.assertRaises(RuntimeError):
			recovery.restore_site(self.root)
		self.assertTrue((self.root / "sites/.frappe-recovery-blocked").exists())
		self.assertEqual(len(self.database_restores()), 1)
		self.assertEqual(recovery.read_json(self.site / "site_config.json")["maintenance_mode"], 1)

	def test_emergency_restore_without_safety_backup_blocks_on_failure(self) -> None:
		self.failure = "restore-database"
		with self.assertRaises(RuntimeError):
			recovery.restore_site(self.root, skip_safety_backup=True)
		self.assertTrue((self.root / "sites/.frappe-recovery-blocked").exists())
		self.assertEqual(len(self.database_restores()), 1)

	def test_running_application_lock_prevents_any_restore_command(self) -> None:
		with (self.root / "sites/.frappe-runtime.lock").open("a") as lock:
			fcntl.flock(lock, fcntl.LOCK_SH)
			with self.assertRaisesRegex(ValueError, "Stop frontend"):
				recovery.restore_site(self.root)
		self.assertEqual(self.commands, [])

	def test_unknown_previous_release_is_not_inferred_from_incoming_image(self) -> None:
		self.assertEqual(recovery.deployed_commit(self.site), "unknown")

	@unittest.skipUnless(shutil.which("restic"), "Restic binary required")
	def test_real_restic_snapshot_manifest_and_verified_restore(self) -> None:
		input_root = self.root / "input"
		input_root.mkdir()
		self.run_command(["restic", "--target", str(input_root)])
		stage = input_root / "snapshot"
		environment = {
			**os.environ,
			"RESTIC_REPOSITORY": str(self.root / "repository"),
			"RESTIC_PASSWORD": "isolated-test-only",
		}

		def restic(*args, cwd=None):
			return subprocess.run(
				["restic", *args], env=environment, cwd=cwd, check=True, capture_output=True, text=True
			)

		restic("init")
		result = restic("backup", ".", "--json", cwd=stage)
		snapshot = json.loads(result.stdout.splitlines()[-1])["snapshot_id"]
		manifest = json.loads(restic("dump", snapshot, "/recovery.json").stdout)
		self.assertEqual(manifest["commit"], self.commit)
		output = self.root / "verified"
		restic("restore", snapshot, "--target", str(output), "--verify")
		recovery.validate_snapshot(output, site="erp.test")


if __name__ == "__main__":
	unittest.main()
