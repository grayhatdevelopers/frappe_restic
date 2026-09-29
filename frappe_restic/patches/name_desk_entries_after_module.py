import frappe

LEGACY = "Restic Backups"


def execute():
	"""Remove the desk entries named after the old "Restic Backups" workspace.

	The workspace, sidebar and desktop icon are now named after the module, so Frappe v16 finds
	one sidebar for every backup screen instead of generating a second one for the module.
	"""
	# Workspace Sidebar and Desktop Icon exist from Frappe v16; v15 only has the workspace.
	for doctype in ("Desktop Icon", "Workspace Sidebar", "Workspace"):
		if frappe.db.table_exists(doctype) and frappe.db.exists(doctype, LEGACY):
			frappe.delete_doc(doctype, LEGACY, force=True, ignore_permissions=True)
