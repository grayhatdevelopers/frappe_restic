from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_restic.patches import name_desk_entries_after_module

MODULE = "Restic Backup"
LEGACY = "Restic Backups"


def has_sidebars():
	# Sidebar exists from Frappe v16.53; v15 has neither sidebars nor desktop icons.
	return frappe.db.table_exists("Sidebar")


class TestDeskEntries(FrappeTestCase):
	def test_workspace_is_named_after_the_module(self):
		self.assertEqual(frappe.get_all("Workspace", filters={"module": MODULE}, pluck="name"), [MODULE])

	def test_module_has_the_shipped_sidebar_and_icon(self):
		if not has_sidebars():
			self.skipTest("Frappe v15 has no sidebars")
		from frappe.desk.doctype.sidebar.sidebar import get_sidebar

		# Without one, Frappe computes a sidebar from the module's contents.
		sidebar = get_sidebar(MODULE)
		self.assertEqual((sidebar.app, sidebar.standard), ("frappe_restic", 1))
		self.assertEqual(
			[item.link_to for item in sidebar.items],
			["restic-backup-control", "Restic Backup Run", "Restic Backup Settings"],
		)
		# Icons are imported only on sites using the icon grid, which finds a sidebar by its module.
		icon = frappe.get_file_json(frappe.get_app_path("frappe_restic", "desktop_icon", "restic_backup.json"))
		self.assertEqual((icon["link_type"], icon["link_to"]), ("Workspace Sidebar", MODULE))

	def test_patch_removes_the_entries_named_after_the_old_workspace(self):
		frappe.get_doc(
			{
				"doctype": "Workspace",
				"label": LEGACY,
				"title": LEGACY,
				"module": MODULE,
				"public": 1,
				"content": "[]",
			}
		).insert()
		doctypes = ["Workspace"]
		if has_sidebars():
			# Workspace Sidebar is a read-only archive now; sites upgraded from earlier v16 still hold rows.
			with patch.dict(frappe.flags, in_patch=True):
				frappe.get_doc({"doctype": "Workspace Sidebar", "title": LEGACY, "module": MODULE}).insert()
			frappe.get_doc(
				{
					"doctype": "Desktop Icon",
					"label": LEGACY,
					"icon_type": "Link",
					"link_type": "Workspace Sidebar",
					"link_to": LEGACY,
				}
			).insert()
			doctypes += ["Workspace Sidebar", "Desktop Icon"]

		name_desk_entries_after_module.execute()

		for doctype in doctypes:
			self.assertFalse(frappe.db.exists(doctype, LEGACY), doctype)
		self.assertTrue(frappe.db.exists("Workspace", MODULE))
