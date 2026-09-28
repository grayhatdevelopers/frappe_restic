frappe.pages["restic-backup-control"].on_page_load = function (wrapper) {
	frappe.breadcrumbs.add("Setup");
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Backup Control"),
		single_column: true,
	});
	page.add_inner_button(__("Native Backups"), () => frappe.set_route("backups"));
	page.add_inner_button(__("Backup Runs"), () => frappe.set_route("List", "Restic Backup Run"));
	const backupControl = new ResticBackupControl(page, wrapper);
	page.add_inner_button(__("Settings"), () => backupControl.openSettings());
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
		this.refresh();
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

	async openSettings() {
		if (!this.data) await this.refresh();
		const settings = this.data.settings;
		const weekdays = [
			"Monday",
			"Tuesday",
			"Wednesday",
			"Thursday",
			"Friday",
			"Saturday",
			"Sunday",
		];
		const schedule = settings.schedule.map((entry) => ({
			...entry,
			days: [...entry.days],
		}));
		const dialog = new frappe.ui.Dialog({
			title: __("Backup Settings"),
			size: "large",
			fields: [
				{ fieldtype: "Section Break", label: __("Schedule") },
				{
					fieldtype: "Check",
					fieldname: "enabled",
					label: __("Enable scheduled backups"),
					default: settings.enabled,
					description: __(
						"Manual backups remain available when the schedule is disabled."
					),
				},
				{
					fieldtype: "HTML",
					options: `<p class="text-muted">${
						this.data.configuration.offsite_enabled
							? __("Off-site uploads are enabled by deployment configuration.")
							: __(
									"Off-site uploads are disabled by deployment configuration. Scheduled and manual backups stay local."
							  )
					}</p>`,
				},
				{
					fieldtype: "HTML",
					fieldname: "backup_schedule",
				},
				{ fieldtype: "Section Break", label: __("Off-site storage") },
				{
					fieldtype: "HTML",
					fieldname: "offsite_configuration",
					options: this.offsiteConfigurationHtml(this.data.configuration),
				},
				{ fieldtype: "Section Break", label: __("Retention") },
				{
					fieldtype: "Int",
					fieldname: "local_backup_limit",
					label: __("Local backup sets"),
					reqd: true,
					default: settings.local_backup_limit,
					description: __("Complete backup sets kept on this server."),
				},
				{ fieldtype: "Column Break" },
				{
					fieldtype: "Int",
					fieldname: "remote_keep_days",
					label: __("Off-site retention (days)"),
					reqd: true,
					default: settings.remote_keep_days,
					description: __("The newest off-site snapshot is always retained."),
				},
				{ fieldtype: "Section Break", label: __("Email notifications") },
				{
					fieldtype: "Data",
					fieldname: "notification_recipients",
					label: __("Recipients"),
					default: settings.notification_recipients,
					description: __("Separate multiple email addresses with commas."),
				},
				{ fieldtype: "Column Break" },
				{
					fieldtype: "Check",
					fieldname: "email_on_success",
					label: __("Email successful runs"),
					default: settings.email_on_success,
					description: __("Failure notifications are always sent."),
				},
			],
			primary_action_label: __("Save"),
			primary_action: async (values) => {
				const enabled = Boolean(Number(values.enabled));
				if (enabled && !this.validateSchedule(schedule)) return;
				const primaryButton = dialog.get_primary_btn();
				primaryButton.prop("disabled", true);
				try {
					this.data.settings = await frappe.xcall(
						"frappe_restic.restic_backup.page.restic_backup_control.restic_backup_control.save_settings",
						{
							values: {
								...values,
								backup_schedule: enabled ? schedule : settings.schedule,
							},
						}
					);
					dialog.hide();
					this.render();
					frappe.show_alert({
						message: __("Backup settings saved"),
						indicator: "green",
					});
				} finally {
					primaryButton.prop("disabled", false);
				}
			},
		});
		dialog.show();
		dialog.$wrapper.addClass("restic-backup-settings-dialog");
		dialog.fields_dict.enabled.$input.on("change.resticBackupSchedule", () => {
			this.setScheduleEditorVisibility(dialog);
		});
		this.renderScheduleEditor(dialog.fields_dict.backup_schedule.$wrapper, schedule, weekdays);
		this.setScheduleEditorVisibility(dialog);
	}

	setScheduleEditorVisibility(dialog) {
		const enabled = Boolean(Number(dialog.get_value("enabled")));
		dialog.fields_dict.backup_schedule.$wrapper.toggle(enabled);
	}

	renderScheduleEditor($wrapper, schedule, weekdays) {
		const render = () => {
			const rows = schedule
				.map((entry, index) => {
					const dayButtons = weekdays
						.map((day) => {
							const selected = entry.days.includes(day);
							return `<button type="button"
						class="btn btn-sm restic-backup-day${selected ? " is-selected" : ""}"
						data-index="${index}"
						data-day="${day}"
						aria-pressed="${selected}">${__(day.slice(0, 3))}</button>`;
						})
						.join("");
					return `<div class="restic-backup-schedule-row" data-index="${index}">
					<div class="restic-backup-schedule-days">
						<div class="restic-backup-field-label">${__("Days")}</div>
						<div class="restic-backup-day-list">${dayButtons}</div>
					</div>
					<label class="restic-backup-schedule-time">
						<span class="restic-backup-field-label">${__("Time")}</span>
						<input type="time" class="form-control" data-index="${index}"
							value="${frappe.utils.escape_html(entry.backup_time || "")}">
					</label>
					<button type="button" class="btn btn-link text-danger restic-backup-remove"
						data-index="${index}" aria-label="${__("Remove schedule")}">${__("Remove")}</button>
				</div>`;
				})
				.join("");

			$wrapper.html(`<div class="restic-backup-schedule-editor">
				<p class="text-muted small mb-0">${__(
					"Each row runs once at the selected time on every selected day."
				)}</p>
				${rows || `<div class="text-muted restic-backup-empty">${__("No schedule rows yet.")}</div>`}
				<button type="button" class="btn btn-default btn-sm restic-backup-add">
					${__("Add schedule")}
				</button>
			</div>`);
		};

		$wrapper.off(".resticBackupSchedule");
		$wrapper.on("click.resticBackupSchedule", ".restic-backup-day", (event) => {
			const button = event.currentTarget;
			const index = Number(button.dataset.index);
			const day = button.dataset.day;
			const selectedDays = schedule[index].days;
			if (selectedDays.includes(day)) {
				schedule[index].days = selectedDays.filter((value) => value !== day);
			} else {
				schedule[index].days = weekdays.filter(
					(value) => selectedDays.includes(value) || value === day
				);
			}
			render();
		});
		$wrapper.on("change.resticBackupSchedule", 'input[type="time"]', (event) => {
			const index = Number(event.currentTarget.dataset.index);
			schedule[index].backup_time = event.currentTarget.value;
		});
		$wrapper.on("click.resticBackupSchedule", ".restic-backup-remove", (event) => {
			schedule.splice(Number(event.currentTarget.dataset.index), 1);
			render();
		});
		$wrapper.on("click.resticBackupSchedule", ".restic-backup-add", () => {
			schedule.push({ days: [], backup_time: "" });
			render();
		});
		render();
	}

	validateSchedule(schedule) {
		if (!schedule.length) {
			frappe.msgprint(__("Add at least one backup schedule."));
			return false;
		}
		const invalidIndex = schedule.findIndex(
			(entry) => !entry.days.length || !entry.backup_time
		);
		if (invalidIndex !== -1) {
			frappe.msgprint(
				__("Choose at least one day and a time in schedule row {0}.", [invalidIndex + 1])
			);
			return false;
		}
		const scheduledSlots = new Map();
		for (const [index, entry] of schedule.entries()) {
			for (const day of entry.days) {
				const slot = `${day}|${entry.backup_time}`;
				if (scheduledSlots.has(slot)) {
					frappe.msgprint(
						__("Schedule rows {0} and {1} both include {2} at {3}.", [
							scheduledSlots.get(slot),
							index + 1,
							__(day),
							entry.backup_time,
						])
					);
					return false;
				}
				scheduledSlots.set(slot, index + 1);
			}
		}
		return true;
	}

	offsiteConfigurationHtml(configuration) {
		const statusRows = configuration.required_variables
			.map((variable) => {
				const indicator = variable.configured ? "green" : "red";
				const status = variable.configured ? __("Configured") : __("Missing");
				return `<div class="restic-backup-env-row">
				<code>${frappe.utils.escape_html(variable.name)}</code>
				<span class="indicator-pill ${indicator}">${status}</span>
			</div>`;
			})
			.join("");
		const deploymentStatus = configuration.offsite_enabled
			? `<span class="indicator-pill green">${__("Enabled")}</span>`
			: `<span class="indicator-pill orange">${__("Disabled")}</span>`;
		const storageStatus = configuration.restic_ready
			? `<span class="indicator-pill green">${__("Configured")}</span>`
			: `<span class="indicator-pill red">${__("Credentials missing")}</span>`;
		return `<details class="card restic-backup-offsite">
			<summary class="restic-backup-offsite-summary">
				<strong>${__("Storage configuration")}</strong>
				${storageStatus}
			</summary>
			<div class="card-body border-top">
			<p class="mb-1">${__(
				"Set these in the deployment environment, not in Frappe, then restart or redeploy."
			)}</p>
			<p class="text-muted small mb-3">${__(
				"Coolify: application Environment Variables. Docker Compose: the .env file supplied to Compose."
			)}</p>
			${statusRows}
			<div class="restic-backup-env-row pt-3">
				<span><code>RESTIC_OFFSITE_BACKUP_ENABLED</code> <span class="text-muted">(${__(
					"all backup uploads"
				)})</span></span>
				${deploymentStatus}
			</div>
			<p class="text-muted small mt-3 mb-0">${__(
				"RESTIC_REPOSITORY format: s3:https://s3.<region>.backblazeb2.com/<bucket>/restic. Use a bucket-restricted Backblaze application key."
			)}</p>
		</div></details>`;
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
