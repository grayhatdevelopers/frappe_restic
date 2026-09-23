"""Deployment boundaries preserve recovery points when commands fail."""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_offsite_failure_retains_validated_local_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.object(
            deployment, "bench_root", return_value=Path(directory)
        ), patch.dict(os.environ, {"RESTIC_OFFSITE_BACKUP_ENABLED": "1"}), patch.object(
            deployment.subprocess, "run",
            side_effect=[None, None, subprocess.CalledProcessError(1, "restic")],
        ) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                deployment.backup("site.test")
            self.assertIn("--local-only", run.call_args_list[1].args[0])
            self.assertEqual(len(list(Path(directory).rglob(".failed"))), 1)

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
