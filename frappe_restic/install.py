"""Installation checks for the supported Frappe releases."""

import frappe
from frappe import _

SUPPORTED_MAJORS = ("15", "16")


def is_supported_version(version: str) -> bool:
	"""Return whether a Frappe version belongs to a supported major release."""
	return version.split(".", 1)[0] in SUPPORTED_MAJORS


def before_install() -> None:
	"""Refuse unsupported major versions before enabling scheduled work."""
	from frappe import __version__

	if not is_supported_version(__version__):
		frappe.throw(_("Restic Backups supports Frappe v15 and v16 only."))


def after_install() -> None:
	"""Deployments install after migrating, so after_migrate has not aligned the defaults."""
	from frappe_restic.restic_backup.backup_control import sync_backup_defaults

	sync_backup_defaults()
