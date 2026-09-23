(function () {
	const nativeBackupsOnLoad = frappe.pages.backups.on_page_load;

	frappe.pages.backups.on_page_load = function (wrapper) {
		nativeBackupsOnLoad.call(this, wrapper);

		if (!frappe.user.has_role("System Manager")) {
			return;
		}

		wrapper.page.add_inner_button(__("Backup Control"), () => {
			frappe.set_route("restic-backup-control");
		});
	};
})();
