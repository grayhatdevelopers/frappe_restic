frappe.ui.form.on("Restic Backup Settings", {
	refresh(frm) {
		frm.page.set_title(__("Backup Settings"));
		frappe.breadcrumbs.add({
			type: "Custom",
			route: frappe.router.make_url(["restic-backup-control"]),
			label: __("Backup Control"),
		});
		frm.add_custom_button(__("Back to Backup Control"), () => {
			frappe.set_route("restic-backup-control");
		});
	},
});
