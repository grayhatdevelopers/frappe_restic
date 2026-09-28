"""Small, Frappe-independent Restic transport used by jobs and deployments."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.parse
import urllib.request
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from frappe_restic.config import namespace
from frappe_restic.restic_backup.recovery import deployed_commit, image_commit, snapshot_manifest

REQUIRED_RESTIC_ENV = (
	"RESTIC_REPOSITORY",
	"RESTIC_PASSWORD",
	"AWS_ACCESS_KEY_ID",
	"AWS_SECRET_ACCESS_KEY",
)
ARTIFACT_SUFFIXES = {
	"database": ("-database.sql.gz", "-database.sql"),
	"config": ("-site_config_backup.json",),
	"public": ("-files.tar", "-files.tgz"),
	"private": ("-private-files.tar", "-private-files.tgz"),
}


def release_identity(environment: dict[str, str] | None = None) -> str:
	"""Return the Git revision without accepting manually assigned image/release labels."""
	if environment is None and image_commit() != "unknown":
		return image_commit()
	env = os.environ if environment is None else environment
	return env.get("SOURCE_COMMIT", "").strip() or "unknown"


def release_tag(identity: str) -> str:
	"""Return a bounded Restic tag for a release identity."""
	safe_identity = "".join(
		character if character.isalnum() or character in "._-" else "-" for character in identity
	)
	return f"{namespace()}-release:{safe_identity[:80]}"


class BackupTransportError(RuntimeError):
	"""Raised when local validation or Restic transport fails."""


@dataclass(frozen=True)
class Artifact:
	"""Validated backup artifact metadata safe to persist in a run record."""

	kind: str
	name: str
	size: int
	sha256: str


def restic_is_configured(environment: dict[str, str] | None = None) -> bool:
	"""Return whether all required Restic/B2 settings are present."""
	env = os.environ if environment is None else environment
	return all(env.get(name) for name in REQUIRED_RESTIC_ENV)


def validate_backup_set(paths: dict[str, str | Path]) -> list[Artifact]:
	"""Validate a complete Frappe backup and return its checksummed manifest."""
	missing = [kind for kind in ARTIFACT_SUFFIXES if not paths.get(kind)]
	if missing:
		raise BackupTransportError(f"Backup set is missing: {', '.join(sorted(missing))}")

	artifacts: list[Artifact] = []
	for kind in ARTIFACT_SUFFIXES:
		path = Path(paths[kind]).resolve()
		if not path.is_file() or path.stat().st_size <= 0:
			raise BackupTransportError(f"{kind.title()} backup is missing or empty: {path.name}")
		try:
			_validate_artifact(kind, path)
		except (OSError, EOFError, ValueError, tarfile.TarError) as error:
			raise BackupTransportError(f"Invalid {kind} backup: {path.name}") from error
		artifacts.append(Artifact(kind=kind, name=path.name, size=path.stat().st_size, sha256=_sha256(path)))
	return artifacts


def stage_backup_set(paths: dict[str, str | Path], destination: str | Path) -> Path:
	"""Expand compressed artifacts into a stable tree so Restic can deduplicate."""
	destination_path = Path(destination).resolve()
	destination_path.mkdir(mode=0o700, parents=True, exist_ok=False)

	database_path = Path(paths["database"])
	with gzip.open(database_path, "rb") as source, (destination_path / "database.sql").open("wb") as target:
		shutil.copyfileobj(source, target)
	shutil.copy2(paths["config"], destination_path / "site_config.json")

	for kind in ("public", "private"):
		target = destination_path / kind
		target.mkdir(mode=0o700)
		with tarfile.open(paths[kind], "r:*") as archive:
			_safe_extract(archive, target)
	return destination_path


@contextmanager
def stable_staging_tree(paths: dict[str, str | Path], staging_root: str | Path) -> Iterable[Path]:
	"""Build and remove a fixed-name plaintext tree for stable Restic snapshots."""
	root = Path(staging_root)
	if not root.name.startswith(".frappe-restic-") or root.is_symlink():
		raise BackupTransportError("Refusing an unsafe Restic staging directory.")
	resolved_root = root.resolve()
	if resolved_root.exists():
		shutil.rmtree(resolved_root)
	resolved_root.mkdir(mode=0o700, parents=True)
	try:
		yield stage_backup_set(paths, resolved_root / "snapshot")
	finally:
		if resolved_root.exists():
			shutil.rmtree(resolved_root)


def backup_with_restic(
	staged_path: str | Path,
	*,
	host: str,
	tags: Iterable[str],
	connections: int = 2,
) -> str:
	"""Upload a staged backup and return the exact snapshot ID from JSON output."""
	_validate_environment()
	if not 1 <= connections <= 5:
		raise BackupTransportError("Restic connection count must be between 1 and 5.")

	base = ["restic", "-o", f"s3.connections={connections}"]
	probe = _run([*base, "snapshots", "--json", "--latest", "1"], check=False)
	if probe.returncode != 0:
		if not _repository_is_missing(probe):
			raise BackupTransportError(_restic_error("Restic repository is not ready", probe))
		initialized = _run([*base, "init"], check=False)
		# Another backup may have initialized the repository after our probe.
		ready = _run([*base, "snapshots", "--json", "--latest", "1"], check=False)
		if ready.returncode != 0:
			failure = initialized if initialized.returncode != 0 else ready
			raise BackupTransportError(_restic_error("Restic repository initialization failed", failure))

	command = [*base, "backup", ".", "--json", "--host", host]
	for tag in tags:
		command.extend(["--tag", tag])
	result = _run(command, cwd=Path(staged_path).resolve(), timeout=12 * 60 * 60)
	for line in reversed(result.stdout.splitlines()):
		try:
			payload = json.loads(line)
		except json.JSONDecodeError:
			continue
		if payload.get("message_type") == "summary" and payload.get("snapshot_id"):
			return str(payload["snapshot_id"])
	raise BackupTransportError("Restic completed without returning a snapshot ID.")


def list_snapshots(*, site_tag: str, connections: int = 2) -> list[dict[str, Any]]:
	"""Read the repository inventory once for controlled reconciliation."""
	_validate_environment()
	result = _run(["restic", "-o", f"s3.connections={connections}", "snapshots", "--json", "--tag", site_tag])
	try:
		payload = json.loads(result.stdout or "[]")
	except json.JSONDecodeError as error:
		raise BackupTransportError("Restic returned invalid snapshot inventory JSON.") from error
	return payload if isinstance(payload, list) else []


def run_maintenance(
	*, site_tag: str, keep_days: int, connections: int = 2, prune: bool = False, check: bool = False
) -> None:
	"""Apply snapshot retention and optionally run expensive weekly operations."""
	_validate_environment()
	base = ["restic", "-o", f"s3.connections={connections}"]
	command = [
		*base,
		"forget",
		"--tag",
		site_tag,
		"--group-by",
		"host",
		"--keep-within",
		f"{keep_days}d",
		"--keep-last",
		"1",
	]
	_run(command)
	# Daily forget may already have removed every expired snapshot. Always prune
	# in the weekly window, even when today's forget removes nothing.
	if prune:
		_run([*base, "prune"])
	if check:
		_run([*base, "check"])


def deployment_backup(
	backup_directory: str | Path,
	*,
	site: str,
	connections: int = 2,
) -> dict[str, Any]:
	"""Validate and upload a deployment backup without requiring migrated schema."""
	directory = Path(backup_directory).resolve()
	paths = discover_backup_set(directory)
	artifacts = validate_backup_set(paths)
	release = deployed_commit(directory.parents[2])
	with stable_staging_tree(paths, directory.parent / ".frappe-restic-deployment-stage") as staged_path:
		snapshot_manifest(staged_path, site=site, commit=release)
		snapshot_id = backup_with_restic(
			staged_path,
			host=_stable_host(site),
			tags=[f"{namespace()}-site:{site}", f"{namespace()}-source:deployment", release_tag(release)],
			connections=connections,
		)
	manifest = {
		"version": 1,
		"site": site,
		"source": "Deployment",
		"status": "Succeeded",
		"release_identity": release,
		"incoming_release": release_identity(),
		"restic_snapshot_id": snapshot_id,
		"artifacts": [asdict(item) for item in artifacts],
	}
	_manifest_path(directory).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
	return manifest


def write_local_deployment_manifest(backup_directory: str | Path, *, site: str) -> dict[str, Any]:
	"""Persist validation evidence even when off-site deployment upload is disabled."""
	directory = Path(backup_directory).resolve()
	artifacts = validate_backup_set(discover_backup_set(directory))
	manifest = {
		"version": 1,
		"site": site,
		"source": "Deployment",
		"status": "Local Only",
		"release_identity": deployed_commit(directory.parents[2]),
		"incoming_release": release_identity(),
		"artifacts": [asdict(item) for item in artifacts],
	}
	_manifest_path(directory).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
	return manifest


def discover_backup_set(directory: str | Path) -> dict[str, Path]:
	"""Find exactly one latest complete Frappe backup set in a directory."""
	root = Path(directory).resolve()
	database_files = sorted(
		root.glob("*-database.sql.gz"), key=lambda path: path.stat().st_mtime, reverse=True
	)
	for database in database_files:
		prefix = database.name[: -len("-database.sql.gz")]
		paths = {
			"database": database,
			"config": root / f"{prefix}-site_config_backup.json",
			"public": _first_existing(root / f"{prefix}-files.tgz", root / f"{prefix}-files.tar"),
			"private": _first_existing(
				root / f"{prefix}-private-files.tgz", root / f"{prefix}-private-files.tar"
			),
		}
		if all(path and path.is_file() for path in paths.values()):
			return paths
	raise BackupTransportError(f"No complete Frappe backup set found in {root}")


def _validate_artifact(kind: str, path: Path) -> None:
	if kind == "database":
		with gzip.open(path, "rb") as stream:
			for _chunk in iter(lambda: stream.read(1024 * 1024), b""):
				pass
	elif kind == "config":
		with path.open(encoding="utf-8") as stream:
			if not isinstance(json.load(stream), dict):
				raise BackupTransportError("Site configuration backup is not a JSON object.")
	else:
		with tarfile.open(path, "r:*") as archive:
			for member in archive.getmembers():
				_validate_archive_member(member)


def _safe_extract(archive: tarfile.TarFile, destination: Path) -> None:
	for member in archive.getmembers():
		_validate_archive_member(member)
	# Explicit so Python 3.11 (v15) and 3.14 (v16) extract identically.
	archive.extractall(destination, filter="data")


def _validate_archive_member(member: tarfile.TarInfo) -> None:
	path = PurePosixPath(member.name)
	if (
		path.is_absolute()
		or ".." in path.parts
		or member.issym()
		or member.islnk()
		or not (member.isfile() or member.isdir())
	):
		raise BackupTransportError(f"Unsafe archive member: {member.name}")


def _sha256(path: Path) -> str:
	digest = hashlib.sha256()
	with path.open("rb") as stream:
		for chunk in iter(lambda: stream.read(1024 * 1024), b""):
			digest.update(chunk)
	return digest.hexdigest()


def _run(
	command: list[str], *, check: bool = True, cwd: Path | None = None, timeout: int = 60 * 60
) -> subprocess.CompletedProcess[str]:
	try:
		result = subprocess.run(
			command,
			capture_output=True,
			check=False,
			text=True,
			timeout=timeout,
			env=os.environ.copy(),
			cwd=cwd,
		)
	except FileNotFoundError as error:
		raise BackupTransportError("Restic is not installed in this container.") from error
	except subprocess.TimeoutExpired as error:
		raise BackupTransportError(f"Restic exceeded its {timeout}-second safety timeout.") from error
	if check and result.returncode != 0:
		raise BackupTransportError(_restic_error("Restic command failed", result))
	return result


def _restic_error(prefix: str, result: subprocess.CompletedProcess[str]) -> str:
	detail = (result.stderr or result.stdout or "unknown error").strip()
	for name in ("RESTIC_PASSWORD", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
		secret = os.environ.get(name)
		if secret:
			detail = detail.replace(secret, "<redacted>")
	return f"{prefix} (exit {result.returncode}): {detail[:2000]}"


def _repository_is_missing(result: subprocess.CompletedProcess[str]) -> bool:
	message = f"{result.stdout}\n{result.stderr}".lower()
	# Failure to open config alone can mean denied access, DNS, TLS or network errors.
	# Only a missing config object/file is permission to initialize; not a missing bucket.
	return "unable to open config file" in message and any(
		missing in message
		for missing in (
			"the specified key does not exist",
			"nosuchkey",
			"no such file or directory",
			"config file does not exist",
		)
	)


def _validate_environment() -> None:
	missing = [name for name in REQUIRED_RESTIC_ENV if not os.environ.get(name)]
	if missing:
		raise BackupTransportError(f"Missing off-site backup setting(s): {', '.join(missing)}")


def _stable_host(site: str) -> str:
	return (
		namespace()
		+ "-"
		+ "".join(character if character.isalnum() or character in "._-" else "-" for character in site)
	)


def _first_existing(*paths: Path) -> Path:
	return next((path for path in paths if path.is_file()), paths[0])


def _manifest_path(directory: Path) -> Path:
	return directory / "backup-control.json"


def main() -> int:
	"""CLI used by the pre-migration site-operation container."""
	parser = argparse.ArgumentParser()
	parser.add_argument("deployment_directory")
	parser.add_argument("--site", required=True)
	parser.add_argument("--connections", type=int, default=2)
	parser.add_argument("--local-only", action="store_true")
	args = parser.parse_args()
	try:
		if args.local_only:
			manifest = write_local_deployment_manifest(args.deployment_directory, site=args.site)
		else:
			manifest = deployment_backup(
				args.deployment_directory,
				site=args.site,
				connections=args.connections,
			)
			_push_deployment_heartbeat(
				True, f"Deployment backup {manifest['restic_snapshot_id'][:12]} completed"
			)
	except BackupTransportError as error:
		_mark_deployment_failure(Path(args.deployment_directory), error)
		_push_deployment_heartbeat(False, str(error))
		print(f"Off-site backup failed: {error}", file=sys.stderr)
		return 1
	print(json.dumps(manifest))
	return 0


def _mark_deployment_failure(directory: Path, error: BackupTransportError) -> None:
	manifest_path = _manifest_path(directory.resolve())
	payload: dict[str, Any] = {}
	try:
		payload = json.loads(manifest_path.read_text(encoding="utf-8"))
	except (OSError, json.JSONDecodeError):
		pass
	payload.update({"status": "Failed", "error": str(error)[:500]})
	manifest_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _push_deployment_heartbeat(succeeded: bool, message: str) -> None:
	url = os.environ.get("RESTIC_DEPLOYMENT_BACKUP_UPTIME_KUMA_URL", "").strip()
	if not url:
		return
	separator = "&" if "?" in url else "?"
	query = urllib.parse.urlencode(
		{"status": "up" if succeeded else "down", "msg": message[:200], "ping": ""}
	)
	try:
		with urllib.request.urlopen(f"{url}{separator}{query}", timeout=15):
			pass
	except (OSError, TimeoutError, ValueError):
		print("WARNING: deployment backup heartbeat delivery failed", file=sys.stderr)


if __name__ == "__main__":
	raise SystemExit(main())
