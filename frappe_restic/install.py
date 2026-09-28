"""Installation checks for the supported Frappe releases."""
import frappe

SUPPORTED_MAJORS = ("15", "16")


def is_supported_version(version: str) -> bool:
    """Return whether a Frappe version belongs to a supported major release."""
    return version.split(".", 1)[0] in SUPPORTED_MAJORS


def after_install() -> None:
    """Refuse unsupported major versions before enabling scheduled work."""
    from frappe import __version__
    if not is_supported_version(__version__):
        frappe.throw("Restic Backups supports Frappe v15 and v16 only.")
