"""Installation checks for the supported Frappe release."""
import frappe


def after_install() -> None:
    """Refuse unsupported major versions before enabling scheduled work."""
    from frappe import __version__
    if __version__.split(".", 1)[0] != "15":
        frappe.throw("Restic Backups currently supports Frappe v15 only.")
