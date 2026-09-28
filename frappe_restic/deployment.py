"""Executable deployment boundaries, independent of any ERP application."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from frappe_restic.config import bench_root, namespace
from frappe_restic.restic_backup.recovery import read_json, record_release, restore_local_backup, write_json
from frappe_restic.retention import prune


def install(site: str) -> None:
	"""Install after the deployment safety backup, before migration."""
	result = subprocess.run(
		["bench", "--site", site, "list-apps"], check=True, capture_output=True, text=True
	)
	installed = {line.split()[0] for line in result.stdout.splitlines() if line.strip()}
	if "frappe_restic" not in installed:
		subprocess.run(["bench", "--site", site, "install-app", "frappe_restic"], check=True)


def backup(site: str) -> Path:
	"""Create and validate a deployment recovery point; pin failures and local-only copies."""
	root = bench_root() / "sites" / site / "private" / "deployment-backups"
	root.mkdir(parents=True, exist_ok=True)
	path = Path(tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-"), dir=root))
	# Emit the path on stdout; callers can capture it and pin it if migration fails.
	print(path, flush=True)
	try:
		subprocess.run(
			[
				"bench",
				"--site",
				site,
				"backup",
				"--with-files",
				"--ignore-backup-conf",
				"--backup-path",
				str(path),
			],
			check=True,
			stdout=sys.stderr,
		)
		command = [
			sys.executable,
			"-m",
			"frappe_restic.restic_backup.restic_transport",
			str(path),
			"--site",
			site,
		]
		subprocess.run([*command, "--local-only"], check=True, stdout=sys.stderr)
	except BaseException:
		(path / ".failed").touch()
		raise
	if os.environ.get("RESTIC_OFFSITE_BACKUP_ENABLED", "0") == "1":
		try:
			subprocess.run(command, check=True, stdout=sys.stderr)
		except subprocess.CalledProcessError:
			# The validated local backup protects this deployment; the site reports the failure.
			(path / ".failed").touch()
			print("WARNING: off-site upload failed; deploying on the local backup.", file=sys.stderr)
	return path


def deploy(site: str) -> None:
	"""Back up, then migrate; a failed migration returns the site to that backup."""
	if not os.environ.get("DB_ROOT_PASSWORD"):
		raise ValueError("DB_ROOT_PASSWORD is required to roll back a failed migration")
	path = backup(site)
	site_path = bench_root() / "sites" / site
	original = read_json(site_path / "site_config.json")
	_set_config(site, maintenance_mode=1, pause_scheduler=1)
	try:
		install(site)
		for command in ("migrate", "clear-cache", "clear-website-cache"):
			subprocess.run(["bench", "--site", site, command], check=True)
		record_release(site_path)
	except Exception:
		(path / ".failed").touch()
		_roll_back(site, site_path, path, original)
		raise
	_set_config(site, pause_scheduler=0, maintenance_mode=0)
	try:
		_prune(site)
	except (OSError, ValueError) as error:
		print(f"WARNING: deployment succeeded but retention failed: {error}", file=sys.stderr)


def _roll_back(site: str, site_path: Path, path: Path, original: dict) -> None:
	print("Deployment failed; returning the site to its pre-deployment backup.", file=sys.stderr, flush=True)
	try:
		restore_local_backup(site, site_path, path, original)
	except Exception as error:  # the migration failure is the error to raise
		write_json(
			bench_root() / "sites" / f".{namespace()}-recovery-blocked",
			{"site": site, "reason": "deployment rollback failed", "backup": str(path)},
		)
		print(f"Rollback failed ({error}); startup is blocked until the site is restored.", file=sys.stderr)
		return
	print(
		"Site returned to its pre-deployment state; redeploy the previous release to serve it.",
		file=sys.stderr,
	)


def _set_config(site: str, **values: int) -> None:
	for key, value in values.items():
		subprocess.run(["bench", "--site", site, "set-config", "-p", key, str(value)], check=True)


def _prune(site: str) -> None:
	root = bench_root() / "sites" / site / "private" / "deployment-backups"
	prune(root, r"\d{8}T\d{6}Z-[A-Za-z0-9_-]+", 10, 30, set(), True)


def main() -> None:
	"""Run with runtime services stopped and the bench virtualenv on PATH."""
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("operation", choices=("deploy", "backup", "install", "complete"))
	parser.add_argument("--site", required=True)
	args = parser.parse_args()
	if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", args.site) or args.site == "all":
		parser.error("--site must identify one site directory")
	os.chdir(bench_root())
	if (bench_root() / "sites" / args.site).is_symlink():
		parser.error("Refusing a symlink site directory")
	if args.operation == "deploy":
		deploy(args.site)
	elif args.operation == "backup":
		backup(args.site)
	elif args.operation == "install":
		install(args.site)
	else:
		record_release(bench_root() / "sites" / args.site)
		_prune(args.site)


if __name__ == "__main__":
	main()
