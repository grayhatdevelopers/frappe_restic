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

from frappe_restic.config import bench_root
from frappe_restic.restic_backup.recovery import record_release
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
    """Create and validate a deployment recovery point; pin failures."""
    root = bench_root() / "sites" / site / "private" / "deployment-backups"
    root.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(
        prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-"), dir=root
    ))
    # Emit the path on stdout; callers can capture it and pin it if migration fails.
    print(path, flush=True)
    try:
        subprocess.run(
            ["bench", "--site", site, "backup", "--with-files", "--ignore-backup-conf",
             "--backup-path", str(path)], check=True, stdout=sys.stderr,
        )
        command = [sys.executable, "-m", "frappe_restic.restic_backup.restic_transport",
                   str(path), "--site", site]
        subprocess.run([*command, "--local-only"], check=True, stdout=sys.stderr)
        if os.environ.get("RESTIC_OFFSITE_BACKUP_ENABLED", "0") == "1":
            subprocess.run(command, check=True, stdout=sys.stderr)
    except BaseException:
        (path / ".failed").touch()
        raise
    return path


def main() -> None:
    """Run with runtime services stopped and the bench virtualenv on PATH."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("backup", "install", "complete"))
    parser.add_argument("--site", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", args.site) or args.site == "all":
        parser.error("--site must identify one site directory")
    os.chdir(bench_root())
    if (bench_root() / "sites" / args.site).is_symlink():
        parser.error("Refusing a symlink site directory")
    if args.operation == "backup":
        backup(args.site)
    elif args.operation == "install":
        install(args.site)
    else:
        record_release(bench_root() / "sites" / args.site)
        root = bench_root() / "sites" / args.site / "private" / "deployment-backups"
        prune(root, r"\d{8}T\d{6}Z-[A-Za-z0-9_-]+", 10, 30, set(), True)


if __name__ == "__main__":
    main()
