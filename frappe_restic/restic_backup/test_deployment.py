"""Deployment boundaries preserve recovery points when commands fail."""
import os
import subprocess
import tempfile
import json
import unittest
from pathlib import Path
from unittest.mock import call, patch

from frappe_restic import deployment
from frappe_restic.config import namespace


class TestDeployment(unittest.TestCase):
    def test_backup_failure_pins_directory_and_stops_upload(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            deployment, "bench_root", return_value=Path(directory)
        ), patch.object(deployment.subprocess, "run", side_effect=subprocess.CalledProcessError(1, "bench")) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                deployment.backup("site.test")
            self.assertEqual(run.call_count, 1)
            self.assertEqual(len(list(Path(directory).rglob(".failed"))), 1)

    def test_offsite_failure_continues_on_validated_local_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            deployment, "bench_root", return_value=Path(directory)
        ), patch.dict(os.environ, {"RESTIC_OFFSITE_BACKUP_ENABLED": "1"}), patch.object(
            deployment.subprocess, "run",
            side_effect=[None, None, subprocess.CalledProcessError(1, "restic")],
        ) as run:
            path = deployment.backup("site.test")
            self.assertIn("--local-only", run.call_args_list[1].args[0])
            # The only copy is local: keep it out of retention.
            self.assertTrue((path / ".failed").exists())

    def test_deploy_requires_database_root_password_before_any_change(self) -> None:
        with patch.dict(os.environ, {"DB_ROOT_PASSWORD": ""}), patch.object(
            deployment, "backup"
        ) as backup, self.assertRaisesRegex(ValueError, "DB_ROOT_PASSWORD"):
            deployment.deploy("site.test")
        backup.assert_not_called()

    def test_existing_install_is_not_repeated(self) -> None:
        with patch.object(deployment.subprocess, "run", return_value=subprocess.CompletedProcess(
            [], 0, "frappe 15.0.0\nfrappe_restic 0.1.0\n"
        )) as run:
            deployment.install("site.test")
            self.assertEqual(run.call_count, 1)

    def test_namespace_preserves_existing_repository_identity(self) -> None:
        with patch.dict(os.environ, {"RESTIC_NAMESPACE": "existing_project"}):
            self.assertEqual(namespace(), "existing_project")
        with patch.dict(os.environ, {"RESTIC_NAMESPACE": "../escape"}):
            with self.assertRaises(ValueError):
                namespace()


class TestDeploy(unittest.TestCase):
    """A deployment changes a running site only after its recovery point exists."""

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.site = self.root / "sites" / "site.test"
        self.backup_path = self.site / "private" / "deployment-backups" / "20260928T000000Z-x"
        self.backup_path.mkdir(parents=True)
        (self.site / "site_config.json").write_text(json.dumps({"db_name": "db"}))
        self.commands = []
        self.failure = None
        for target, kwargs in (
            (patch.dict(os.environ, {"DB_ROOT_PASSWORD": "unused"}), {}),
            (patch.object(deployment, "bench_root", return_value=self.root), {}),
            (patch.object(deployment, "backup", side_effect=self.record("backup", self.backup_path)), {}),
            (patch.object(deployment, "install", side_effect=self.record("install")), {}),
            (patch.object(deployment.subprocess, "run", side_effect=self.bench), {}),
            (patch.object(deployment, "prune"), {}),
        ):
            target.start()
            self.addCleanup(target.stop)
        self.rollback = patch.object(deployment, "restore_local_backup")
        self.restore = self.rollback.start()
        self.addCleanup(self.rollback.stop)

    def record(self, name, result=None):
        def step(*args, **kwargs):
            self.commands.append([name])
            if self.failure == name:
                raise subprocess.CalledProcessError(1, name)
            return result
        return step

    def bench(self, command, **kwargs):
        self.commands.append(command[3:])
        if self.failure in command:
            raise subprocess.CalledProcessError(1, command)

    def test_backup_failure_changes_nothing(self) -> None:
        self.failure = "backup"
        with self.assertRaises(subprocess.CalledProcessError):
            deployment.deploy("site.test")
        self.assertEqual(self.commands, [["backup"]])

    def test_successful_deploy_pauses_only_after_backup(self) -> None:
        deployment.deploy("site.test")
        self.assertEqual(self.commands, [
            ["backup"],
            ["set-config", "-p", "maintenance_mode", "1"],
            ["set-config", "-p", "pause_scheduler", "1"],
            ["install"], ["migrate"], ["clear-cache"], ["clear-website-cache"],
            ["set-config", "-p", "pause_scheduler", "0"],
            ["set-config", "-p", "maintenance_mode", "0"],
        ])
        self.assertTrue((self.site / f".{namespace()}-deployed-release.json").exists())
        self.restore.assert_not_called()

    def test_migration_failure_returns_site_to_backup_as_it_was(self) -> None:
        self.failure = "migrate"
        with self.assertRaises(subprocess.CalledProcessError):
            deployment.deploy("site.test")
        # The original configuration, without the deployment's maintenance flags.
        self.restore.assert_called_once_with("site.test", self.site, self.backup_path, {"db_name": "db"})
        self.assertTrue((self.backup_path / ".failed").exists())
        self.assertFalse((self.root / "sites" / f".{namespace()}-recovery-blocked").exists())
        self.assertFalse((self.site / f".{namespace()}-deployed-release.json").exists())

    def test_failed_rollback_blocks_startup(self) -> None:
        self.failure = "migrate"
        self.restore.side_effect = RuntimeError("rollback failed")
        with self.assertRaises(subprocess.CalledProcessError):
            deployment.deploy("site.test")
        blocked = json.loads((self.root / "sites" / f".{namespace()}-recovery-blocked").read_text())
        self.assertEqual(blocked["site"], "site.test")
