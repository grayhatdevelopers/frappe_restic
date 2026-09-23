frappe.listview_settings["Restic Backup Run"] = {
	onload(listview) {
		listview.page.set_title(__("Backup Runs"));
		frappe.breadcrumbs.add({
			type: "Custom",
			route: "/app/restic-backup-control",
			label: __("Backup Control"),
		});
	},
};
