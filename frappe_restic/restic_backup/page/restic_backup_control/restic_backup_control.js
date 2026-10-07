frappe.pages["restic-backup-control"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Backup Control"),
		single_column: true,
	});
	page.add_inner_button(__("Native Backups"), () => frappe.set_route("backups"));
	page.add_inner_button(__("Backup Runs"), () => frappe.set_route("List", "Restic Backup Run"));
	page.add_inner_button(__("Settings"), () =>
		frappe.set_route("Form", "Restic Backup Settings")
	);
	wrapper.backup_control = new ResticBackupControl(page, wrapper);
};

// Frappe v16 picks the sidebar for a page from the module of the last doctype opened, so opening
// this page from another app's screen keeps that app's sidebar. Pick again from this page's route.
frappe.pages["restic-backup-control"].on_page_show = function (wrapper) {
	frappe.app.sidebar?.set_workspace_sidebar?.();
	// Settings are edited on their own form, so reload what this page shows of them on return.
	wrapper.backup_control?.refresh();
};

class ResticBackupControl {
	constructor(page, wrapper) {
		this.page = page;
		this.$main = $(wrapper).find(".page-content");
		this.currentPage = 1;

		this.page.add_action_item(__("Check off-site backups"), () => this.reconcile());
		this.page.add_inner_button(__("Refresh"), () => this.refresh());
		this.$main.on("click", ".restic-backup-page", (event) => {
			this.currentPage = Number(event.currentTarget.dataset.page);
			this.refresh();
		});
		this.$main.on("click", ".restic-backup-details", (event) => {
			this.showDetails(this.data.items[Number(event.currentTarget.dataset.index)]);
		});
	}

	async refresh() {
		this.$main.html(`<div class="text-muted p-4">${__("Loading backup state...")}</div>`);
		try {
			this.data = await frappe.xcall(
				"frappe_restic.restic_backup.page.restic_backup_control.restic_backup_control.get_dashboard",
				{ page: this.currentPage }
			);
			this.render();
		} catch (error) {
			this.$main.html(
				`<div class="alert alert-danger m-4">${__("Could not load backup state.")}</div>`
			);
			throw error;
		}
	}

	render() {
		const data = this.data;
		const offsite = data.configuration.offsite_enabled;
		this.currentPage = data.page;
		this.page.set_primary_action(
			offsite ? __("Back up and upload") : __("Back up locally"),
			() => this.runBackup()
		);
		const uploadStatus = !offsite
			? __("Uploads disabled")
			: data.configuration.restic_ready
			? __("Uploads configured")
			: __("Credentials missing");
		this.$main.html(`
			<div class="restic-backup-control p-4">
				<div class="row mb-3">
					${this.card(__("Latest backup activity"), this.formatDate(data.summary.latest), "blue")}
					${this.card(__("Local backups"), data.summary.local_count, "blue")}
					${this.card(
						__("Off-site storage"),
						uploadStatus,
						offsite && !data.configuration.restic_ready ? "red" : "gray"
					)}
					${this.card(__("Scheduled backups"), data.settings.enabled ? __("Enabled") : __("Off"), "gray")}
				</div>
				<div class="d-flex justify-content-between align-items-center mb-3 flex-wrap">
					<h5 class="mb-1">${__("Backup history")}</h5>
					<span class="text-muted small">${__("Last confirmed off-site")}: ${this.formatDate(
			data.summary.last_verified
		)}</span>
				</div>
				${this.backupTable(data.items)}
				<div class="d-flex justify-content-between align-items-center mt-3">
					<span class="text-muted small">${
						data.total
							? __("{0}–{1} of {2}", [
									(data.page - 1) * 10 + 1,
									Math.min(data.page * 10, data.total),
									data.total,
							  ])
							: __("No backups recorded")
					}</span>
					<div>
						<button class="btn btn-default btn-sm restic-backup-page" data-page="${data.page - 1}" ${
			data.page <= 1 ? "disabled" : ""
		}>${__("Previous")}</button>
						<button class="btn btn-default btn-sm restic-backup-page" data-page="${data.page + 1}" ${
			data.page * 10 >= data.total ? "disabled" : ""
		}>${__("Next")}</button>
					</div>
				</div>
			</div>
		`);
	}

	card(title, value, indicator) {
		return `<div class="col-lg-3 col-sm-6 mb-2"><div class="card h-100"><div class="card-body">
			<div class="text-muted small">${frappe.utils.escape_html(title)}</div>
			<div class="mt-2"><span class="indicator-pill ${indicator}">${frappe.utils.escape_html(
			String(value)
		)}</span></div>
		</div></div></div>`;
	}

	backupTable(items) {
		const escape = frappe.utils.escape_html;
		const rows = items
			.map(
				(item, index) => `<tr>
			<td class="text-nowrap">${this.formatDate(item.created)}</td>
			<td>${escape(item.source)}</td>
			<td>${escape(__(item.status || "Not recorded"))}</td>
			<td>${escape(__(item.local_status))}</td>
			<td>${escape(__(item.remote_status))}${
					item.remote_verified_at
						? `<div class="small text-muted">${this.formatDate(
								item.remote_verified_at
						  )}</div>`
						: ""
				}</td>
			<td class="text-nowrap">${item.bytes ? this.formatBytes(item.bytes) : "—"}</td>
			<td><button class="btn btn-default btn-xs restic-backup-details" data-index="${index}">${__(
					"Details"
				)}</button>${
					item.retention_reason
						? `<div class="small text-muted mt-1">${escape(
								__(item.retention_reason)
						  )}</div>`
						: ""
				}</td>
		</tr>`
			)
			.join("");
		return `<div class="card table-responsive"><table class="table mb-0">
			<thead><tr><th>${__("Created")}</th><th>${__("Source")}</th><th>${__(
			"Backup result"
		)}</th><th>${__("On this server")}</th><th>${__("Off-site · last known")}</th><th>${__(
			"Local size"
		)}</th><th>${__("Details")}</th></tr></thead>
			<tbody>${
				rows ||
				`<tr><td colspan="7" class="text-muted p-4">${__(
					"No backups found. Create a backup using the button above."
				)}</td></tr>`
			}</tbody>
		</table></div>`;
	}

	showDetails(item) {
		const escape = frappe.utils.escape_html;
		const fields = [
			[__("Backup"), item.name],
			[__("Backup result"), item.status || __("Not recorded")],
			[__("On this server"), item.local_status],
			[__("Off-site · last known"), item.remote_status],
			[__("Snapshot"), item.snapshot_id || __("Not recorded")],
			[
				__("Last off-site check"),
				item.remote_verified_at
					? frappe.datetime.str_to_user(item.remote_verified_at)
					: __("Not checked"),
			],
			[__("Release"), item.release_identity || __("Not recorded")],
			[__("Automatic cleanup"), item.retention_reason || __("Normal retention policy")],
		];
		const dialog = new frappe.ui.Dialog({
			title: __("Backup details"),
			fields: [
				{
					fieldtype: "HTML",
					options: `<dl>${fields
						.map(
							([label, value]) =>
								`<dt>${escape(
									label
								)}</dt><dd style="overflow-wrap:anywhere">${escape(
									String(value)
								)}</dd>`
						)
						.join("")}</dl>
				${
					item.retention_reason
						? `<p>${__(
								"Automatic cleanup skips this backup. This does not indicate encryption or off-site availability."
						  )}</p>`
						: ""
				}
				${item.error_summary ? `<p class="text-danger">${escape(item.error_summary)}</p>` : ""}
				<p class="text-muted small">${__(
					"Off-site status reflects the last recorded result. Use Check off-site backups to update it."
				)}</p>`,
				},
			],
		});
		if (item.snapshot_id && !["Missing", "Failed"].includes(item.remote_status)) {
			dialog.set_primary_action(__("Recovery details"), () => {
				dialog.hide();
				this.showRecoveryDetails(item.run_name, item.snapshot_id).catch((error) => {
					console.error("Could not read recovery details", error);
				});
			});
		}
		dialog.show();
	}

	formatDate(value) {
		return value
			? frappe.utils.escape_html(frappe.datetime.str_to_user(value))
			: __("Not recorded");
	}

	formatBytes(bytes) {
		if (!bytes) return "0 B";
		const units = ["B", "KB", "MB", "GB", "TB"];
		const index = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), units.length - 1);
		return `${(bytes / 1024 ** index).toFixed(index ? 1 : 0)} ${units[index]}`;
	}

	async runBackup() {
		const result = await frappe.xcall(
			"frappe_restic.restic_backup.backup_control.enqueue_manual_backup"
		);
		frappe.show_alert({
			message: `${__("Backup queued")}: ${result.name}`,
			indicator: "blue",
		});
		window.setTimeout(() => this.refresh(), 2000);
	}

	async showRecoveryDetails(runName, snapshotId) {
		const details = await frappe.xcall(
			"frappe_restic.restic_backup.page.restic_backup_control.restic_backup_control.get_recovery_details",
			{ run_name: runName, snapshot_id: snapshotId }
		);
		const values = `SITE_OPERATION=restore\nRESTIC_RESTORE_SNAPSHOT=${details.snapshot}`;
		frappe.msgprint({
			title: __("Recovery deployment details"),
			wide: true,
			message: `<p>${__(
				"This replaces the site's database and uploaded files. Nothing has been restored yet."
			)}</p>
				<p>${__(
					"Select the application revision to deploy in your deployment platform. The backup will be restored, then migrated to that code before the application starts."
				)}</p>
				<p>${__("Set these Environment Variables:")}</p><pre>${frappe.utils.escape_html(values)}</pre>
				<p>${__(
					"Use RESTIC_RESTORE_SNAPSHOT=latest instead to restore the newest backup for this site."
				)}</p>
				<p>${__(
					"Stop the application before deploying. After success, remove the restore values and set SITE_OPERATION=migrate for ordinary deployments."
				)}</p>
				<p>${__(
					"For other deployments, stop runtime services and use the recovery entrypoint shipped with the Restic Backups app, following its installation guide. Recovery uses the selected application version and the repository credentials configured on the server."
				)}</p>`,
		});
	}

	async reconcile() {
		await frappe.xcall("frappe_restic.restic_backup.backup_control.reconcile_remote");
		frappe.show_alert({ message: __("Off-site reconciliation queued"), indicator: "blue" });
	}
}
