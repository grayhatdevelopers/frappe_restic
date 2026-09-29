import frappe
from frappe.tests.utils import FrappeTestCase

from frappe_restic.patches import name_desk_entries_after_module

MODULE = "Restic Backup"
LEGACY = "Restic Backups"


def has_sidebars():
	# Workspace Sidebar and Desktop Icon exist from Frappe v16.
	return frappe.db.table_exists("Workspace Sidebar")


class TestDeskEntries(FrappeTestCase):
	def test_workspace_is_named_after_the_module(self):
		self.assertEqual(frappe.get_all("Workspace", filters={"module": MODULE}, pluck="name"), [MODULE])

	def test_one_sidebar_and_icon_for_the_module(self):
		if not has_sidebars():
			self.skipTest("Frappe v15 has no sidebars")
		from frappe.desk.doctype.workspace_sidebar.workspace_sidebar import (
			auto_generate_sidebar_from_module,
		)

		sidebars = frappe.get_all(
			"Workspace Sidebar", filters={"module": MODULE, "for_user": ["is", "not set"]}, pluck="name"
		)
		self.assertEqual(sidebars, [MODULE])
		# Frappe generates a second sidebar for a module that has none named after it.
		self.assertNotIn(MODULE, [sidebar.title for sidebar in auto_generate_sidebar_from_module()])
		icons = frappe.get_all("Desktop Icon", filters={"link_to": ["in", [MODULE, LEGACY]]}, pluck="name")
		self.assertEqual(icons, [MODULE])

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
