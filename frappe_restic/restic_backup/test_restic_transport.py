"""Tests for backup validation and deduplication staging."""

from __future__ import annotations

import gzip
import io
import json
import tarfile
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from frappe_restic.restic_backup.restic_transport import (
	BackupTransportError,
	backup_with_restic,
	deployment_backup,
	discover_backup_set,
	release_identity,
	release_tag,
	run_maintenance,
	stable_staging_tree,
	stage_backup_set,
	validate_backup_set,
	write_local_deployment_manifest,
	_repository_is_missing,
	_restic_error,
)


class TestResticTransport(unittest.TestCase):
	def test_only_missing_config_allows_initialization(self) -> None:
		for detail in ("The specified key does not exist.", "NoSuchKey", "no such file or directory"):
			self.assertTrue(_repository_is_missing(CompletedProcess([], 1, "", f"Fatal: unable to open config file: {detail}")))
		for detail in ("Access Denied", "The specified bucket does not exist", "connection refused", "certificate verify failed"):
			self.assertFalse(_repository_is_missing(CompletedProcess([], 1, "", f"Fatal: unable to open config file: {detail}")))
		self.assertFalse(_repository_is_missing(CompletedProcess([], 1, "", "wrong password or no key found")))

	def test_error_keeps_cause_before_repository_url_and_redacts_secrets(self) -> None:
		with patch.dict("os.environ", {"RESTIC_PASSWORD": "test-private-password"}):
			error = _restic_error("probe", CompletedProcess([], 1, "", "Fatal: Access Denied test-private-password\nIs there a repository?\ns3:https://host/bucket"))
		self.assertIn("Access Denied", error)
		self.assertNotIn("test-private-password", error)

	def test_first_upload_initializes_and_handles_a_concurrent_initializer(self) -> None:
		missing = CompletedProcess([], 1, "", "Fatal: unable to open config file: The specified key does not exist.")
		ready = CompletedProcess([], 0, "[]", "")
		upload = CompletedProcess([], 0, '{"message_type":"summary","snapshot_id":"abc123"}', "")
		for initialization in (CompletedProcess([], 0, "created", ""), CompletedProcess([], 1, "", "already initialized")):
			with patch("frappe_restic.restic_backup.restic_transport._validate_environment"), patch(
				"frappe_restic.restic_backup.restic_transport._run", side_effect=[missing, initialization, ready, upload]
			) as run:
				self.assertEqual(backup_with_restic(".", host="test", tags=[]), "abc123")
				self.assertEqual(run.call_args_list[1].args[0][-1], "init")

	def test_repository_errors_stop_before_init_or_upload(self) -> None:
		for detail in ("Fatal: unable to open config file: Access Denied", "wrong password or no key found"):
			with patch("frappe_restic.restic_backup.restic_transport._validate_environment"), patch(
				"frappe_restic.restic_backup.restic_transport._run", return_value=CompletedProcess([], 1, "", detail)
			) as run:
				with self.assertRaises(BackupTransportError):
					backup_with_restic(".", host="test", tags=[])
				self.assertEqual(run.call_count, 1)

	def test_explicit_empty_environment_does_not_use_process_credentials(self) -> None:
		with patch.dict("os.environ", {"SOURCE_COMMIT": "unexpected"}):
			self.assertEqual(release_identity({}), "unknown")

	def test_rejects_database_truncated_after_valid_header(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			paths = self._create_backup_set(Path(temporary_directory))
			with gzip.open(paths["database"], "wb") as stream:
				stream.write(b"SELECT 1;\n" * 10000)
			data = paths["database"].read_bytes()
			paths["database"].write_bytes(data[:-8])
			with self.assertRaisesRegex(BackupTransportError, "Invalid database backup"):
				validate_backup_set(paths)

	def test_weekly_prunes_even_when_forget_removed_no_snapshots(self) -> None:
		with (
			patch("frappe_restic.restic_backup.restic_transport._validate_environment"),
			patch("frappe_restic.restic_backup.restic_transport._run") as run,
		):
			run_maintenance(site_tag="frappe-site:erp.local", keep_days=14, prune=True, check=True)
		commands = [call.args[0] for call in run.call_args_list]
		self.assertEqual(commands[0][commands[0].index("--group-by") + 1], "host")
		self.assertEqual(commands[0][commands[0].index("--tag") + 1], "frappe-site:erp.local")
		self.assertEqual(commands[1][-1], "prune")
		self.assertEqual(commands[2][-1], "check")

	def test_release_identity_prefers_coolify_source_commit(self) -> None:
		environment = {
			"SOURCE_COMMIT": "a" * 40,
			"RESTIC_RELEASE_ID": "manual-release",
			"RESTIC_IMAGE": "registry.example/application:manual",
		}

		self.assertEqual(release_identity(environment), "a" * 40)
		environment.pop("SOURCE_COMMIT")
		self.assertEqual(release_identity(environment), "unknown")
		self.assertEqual(release_tag("image/name:tag"), "frappe-release:image-name-tag")

	def test_validates_and_stages_complete_backup(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			root = Path(temporary_directory)
			paths = self._create_backup_set(root)

			artifacts = validate_backup_set(paths)
			staged = stage_backup_set(paths, root / "staged")

			self.assertEqual({item.kind for item in artifacts}, {"database", "config", "public", "private"})
			self.assertEqual((staged / "database.sql").read_text(), "SELECT 1;\n")
			self.assertTrue((staged / "public" / "public" / "files" / "readme.txt").is_file())
			self.assertTrue((staged / "private" / "private" / "files" / "secret.txt").is_file())

	def test_rejects_archive_path_traversal(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			root = Path(temporary_directory)
			paths = self._create_backup_set(root)
			with tarfile.open(paths["public"], "w:gz") as archive:
				payload = b"escape"
				member = tarfile.TarInfo("../../escape.txt")
				member.size = len(payload)
				archive.addfile(member, io.BytesIO(payload))

			with self.assertRaisesRegex(BackupTransportError, "Unsafe archive member"):
				validate_backup_set(paths)

	def test_discovers_latest_complete_set(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			root = Path(temporary_directory)
			paths = self._create_backup_set(root)
			discovered = discover_backup_set(root)
			self.assertEqual(discovered, paths)

	def test_stable_staging_tree_replaces_stale_data_and_cleans_up(self) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			root = Path(temporary_directory)
			paths = self._create_backup_set(root)
			staging_root = root / ".frappe-restic-application-stage"
			staging_root.mkdir()
			(staging_root / "stale.txt").write_text("stale", encoding="utf-8")

			with stable_staging_tree(paths, staging_root) as staged:
				self.assertEqual(staged, staging_root / "snapshot")
				self.assertFalse((staging_root / "stale.txt").exists())
				self.assertTrue((staged / "database.sql").is_file())

			self.assertFalse(staging_root.exists())

	@patch.dict(
		"os.environ",
		{
			"RESTIC_REPOSITORY": "s3:https://example.invalid/bucket/repo",
			"RESTIC_PASSWORD": "test-password",
			"AWS_ACCESS_KEY_ID": "test-key",
			"AWS_SECRET_ACCESS_KEY": "test-secret",
		},
		clear=True,
	)
	def test_backup_returns_snapshot_without_repository_listing(self) -> None:
		probe = CompletedProcess(["restic"], 0, "[]", "")
		upload = CompletedProcess(
			["restic"],
			0,
			'{"message_type":"summary","snapshot_id":"abc123"}\n',
			"",
		)
		with patch(
			"frappe_restic.restic_backup.restic_transport._run",
			side_effect=[probe, upload],
		) as run:
			snapshot_id = backup_with_restic(
				".",
				host="frappe-erp.local",
				tags=["frappe-site:erp.local", "frappe-source:manual"],
			)

		self.assertEqual(snapshot_id, "abc123")
		self.assertEqual(run.call_count, 2)
		self.assertIn("snapshots", run.call_args_list[0].args[0])
		self.assertNotIn("snapshots", run.call_args_list[1].args[0])
		self.assertIn(".", run.call_args_list[1].args[0])
		self.assertEqual(Path.cwd(), run.call_args_list[1].kwargs["cwd"])

	@patch.dict(
		"os.environ",
		{
			"SOURCE_COMMIT": "b" * 40,
			"RESTIC_IMAGE": "registry.example/frappe-worker:release",
		},
		clear=True,
	)
	@patch(
		"frappe_restic.restic_backup.restic_transport.release_identity",
		return_value="b" * 40,
	)
	@patch(
		"frappe_restic.restic_backup.restic_transport.deployed_commit",
		return_value="a" * 40,
	)
	@patch(
		"frappe_restic.restic_backup.restic_transport.backup_with_restic",
		return_value="snapshot123",
	)
	def test_deployment_manifest_and_snapshot_record_release(self, backup, _deployed, _incoming) -> None:
		with tempfile.TemporaryDirectory() as temporary_directory:
			root = Path(temporary_directory) / "deployment"
			root.mkdir()
			self._create_backup_set(root)

			local_manifest = write_local_deployment_manifest(root, site="erp.local")
			manifest = deployment_backup(root, site="erp.local")
			persisted = json.loads((root / "backup-control.json").read_text(encoding="utf-8"))

		self.assertEqual(local_manifest["release_identity"], "a" * 40)
		self.assertEqual(manifest["release_identity"], "a" * 40)
		self.assertEqual(manifest["incoming_release"], "b" * 40)
		self.assertNotIn("image_reference", manifest)
		self.assertEqual(persisted, manifest)
		self.assertIn("frappe-release:" + "a" * 40, backup.call_args.kwargs["tags"])

	def _create_backup_set(self, root: Path) -> dict[str, Path]:
		prefix = "20260916_120000-erp_local"
		paths = {
			"database": root / f"{prefix}-database.sql.gz",
			"config": root / f"{prefix}-site_config_backup.json",
			"public": root / f"{prefix}-files.tgz",
			"private": root / f"{prefix}-private-files.tgz",
		}
		with gzip.open(paths["database"], "wt") as stream:
			stream.write("SELECT 1;\n")
		paths["config"].write_text(json.dumps({"db_name": "test"}), encoding="utf-8")
		self._write_archive(paths["public"], "public/files/readme.txt", b"hello")
		self._write_archive(paths["private"], "private/files/secret.txt", b"secret")
		return paths

	def _write_archive(self, path: Path, name: str, payload: bytes) -> None:
		with tarfile.open(path, "w:gz") as archive:
			member = tarfile.TarInfo(name)
			member.size = len(payload)
			archive.addfile(member, io.BytesIO(payload))


if __name__ == "__main__":
	unittest.main()
