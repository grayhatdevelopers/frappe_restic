#!/usr/bin/env python3
"""Prune recognized backup artifacts, retaining recent and pinned recovery points."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
from pathlib import Path


def prune(
	root: Path,
	pattern: str,
	keep: int,
	days: int,
	protected: set[Path],
	directories: bool,
	dry_run: bool = False,
) -> int:
	"""Remove only old, excess direct children of an explicitly scoped root."""
	if keep < 1 or days < 0:
		raise ValueError("Backup retention requires keep >= 1 and days >= 0")
	if root.is_symlink():
		raise ValueError("Backup root must not be a symlink")
	root = root.resolve()
	if not root.exists():
		return 0
	candidates = []
	for path in root.iterdir():
		if path.is_symlink() or not re.fullmatch(pattern, path.name):
			continue
		if (directories and not path.is_dir()) or (not directories and not path.is_file()):
			continue
		candidates.append(path)
	candidates.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
	cutoff = time.time() - days * 86400
	removed = 0
	for path in candidates[keep:]:
		if path.resolve() in protected or path.stat().st_mtime >= cutoff:
			continue
		if directories and ((path / ".keep").exists() or (path / ".failed").exists()):
			continue
		# Never follow links within a backup directory during recursive cleanup.
		if directories and any(child.is_symlink() for child in path.rglob("*")):
			continue
		if path.resolve().parent != root:
			raise ValueError(f"Backup escaped retention root: {path}")
		print(f"{'Would remove' if dry_run else 'Removing'} backup: {path}")
		if not dry_run:
			if directories:
				shutil.rmtree(path)
			else:
				path.unlink()
		removed += 1
	print(
		f"Backup retention: {removed} {'eligible' if dry_run else 'removed'}; "
		f"{len(candidates) - removed} retained."
	)
	return removed


def main() -> int:
	"""Apply site-scoped local or deployment retention; malformed state fails closed."""
	parser = argparse.ArgumentParser(description=__doc__)
	parser.add_argument("mode", choices=("local", "server"))
	parser.add_argument("--root", type=Path, required=True)
	parser.add_argument("--site", required=True)
	parser.add_argument("--keep", type=int, default=10)
	parser.add_argument("--days", type=int, required=True)
	parser.add_argument("--dry-run", action="store_true")
	args = parser.parse_args()
	if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", args.site):
		parser.error("site must be a single site name")
	protected = set()
	if args.mode == "local":
		repo = args.root.resolve()
		guard = repo / ".local/frappe-patch-guard"
		state = json.loads((guard / "state.json").read_text(encoding="utf-8"))
		if state["version"] != 1 or args.site not in state["sites"]:
			raise ValueError("Unrecognized or missing patch guard state")
		for site in state["sites"].values():
			for patch in site["patches"].values():
				if patch.get("pre_run_backup"):
					protected.add((repo / patch["pre_run_backup"]).resolve())
		root = guard / "backups"
		pattern = re.escape(args.site) + r"-\d{8}T\d{6}Z-database\.sql\.gz"
	else:
		root = args.root / args.site / "private/deployment-backups"
		pattern = r"\d{8}T\d{6}Z-[a-zA-Z0-9_-]{6,}"
	prune(root, pattern, args.keep, args.days, protected, args.mode == "server", args.dry_run)
	return 0


if __name__ == "__main__":
	try:
		raise SystemExit(main())
	except (OSError, ValueError, KeyError, TypeError) as error:
		print(f"Backup retention failed; inspect retained backups: {error}", file=sys.stderr)
		raise SystemExit(1)
