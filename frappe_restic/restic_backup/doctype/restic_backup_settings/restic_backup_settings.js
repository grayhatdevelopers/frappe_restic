const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

frappe.ui.form.on("Restic Backup Settings", {
	refresh(frm) {
		frm.page.set_title(__("Backup Settings"));
		frm.add_custom_button(__("Back to Backup Control"), () => {
			frappe.set_route("restic-backup-control");
		});
		bind_schedule_editor(frm);
		render_schedule_editor(frm);
		render_storage_configuration(frm);
	},

	validate(frm) {
		const problem = schedule_problem(frm.doc.backup_schedule || []);
		if (problem) {
			frappe.msgprint(problem);
			frappe.validated = false;
		}
	},
});

function selected_days(row) {
	return (row.days || "")
		.split(",")
		.map((day) => day.trim())
		.filter(Boolean);
}

// A time loaded from the database has no leading zero ("9:00:00"), which a time input rejects.
function clock_time(value) {
	if (!value) return "";
	return String(value)
		.split(":")
		.slice(0, 2)
		.map((part) => part.padStart(2, "0"))
		.join(":");
}

// The schedule is stored in the hidden table; this editor only reads and writes its rows.
function render_schedule_editor(frm) {
	const rows = (frm.doc.backup_schedule || [])
		.map((row, index) => {
			const days = selected_days(row);
			const day_buttons = WEEKDAYS.map((day) => {
				const selected = days.includes(day);
				return `<button type="button"
					class="btn btn-sm ${selected ? "btn-primary" : "btn-default"} restic-backup-day"
					data-index="${index}"
					data-day="${day}"
					aria-pressed="${selected}">${__(day.slice(0, 3))}</button>`;
			}).join("");
			return `<div class="restic-backup-schedule-row">
				<div class="restic-backup-schedule-days">
					<div class="restic-backup-field-label">${__("Days")}</div>
					<div class="restic-backup-day-list">${day_buttons}</div>
				</div>
				<label class="restic-backup-schedule-time">
					<span class="restic-backup-field-label">${__("Time")}</span>
					<input type="time" class="form-control" data-index="${index}"
						value="${frappe.utils.escape_html(clock_time(row.backup_time))}">
				</label>
				<button type="button" class="btn btn-link text-danger restic-backup-remove"
					data-index="${index}" aria-label="${__("Remove schedule")}">${__("Remove")}</button>
			</div>`;
		})
		.join("");

	frm.fields_dict.schedule_editor.$wrapper.addClass("restic-backup-settings")
		.html(`<div class="restic-backup-schedule-editor">
			<p class="text-muted small mb-0">${__(
				"Each row runs once at the selected time on every selected day."
			)}</p>
			${rows || `<div class="text-muted restic-backup-empty">${__("No schedule rows yet.")}</div>`}
			<button type="button" class="btn btn-default btn-sm restic-backup-add">
				${__("Add schedule")}
			</button>
		</div>`);
}

function bind_schedule_editor(frm) {
	const $wrapper = frm.fields_dict.schedule_editor.$wrapper;
	const row_at = (element) => frm.doc.backup_schedule[Number(element.dataset.index)];

	$wrapper.off(".resticBackupSchedule");
	$wrapper.on("click.resticBackupSchedule", ".restic-backup-day", (event) => {
		const row = row_at(event.currentTarget);
		const day = event.currentTarget.dataset.day;
		const days = selected_days(row);
		row.days = WEEKDAYS.filter((value) =>
			value === day ? !days.includes(day) : days.includes(value)
		).join(",");
		frm.dirty();
		render_schedule_editor(frm);
	});
	$wrapper.on("change.resticBackupSchedule", 'input[type="time"]', (event) => {
		row_at(event.currentTarget).backup_time = event.currentTarget.value;
		frm.dirty();
	});
	$wrapper.on("click.resticBackupSchedule", ".restic-backup-remove", (event) => {
		const row = row_at(event.currentTarget);
		frappe.model.clear_doc(row.doctype, row.name);
		frm.dirty();
		render_schedule_editor(frm);
	});
	$wrapper.on("click.resticBackupSchedule", ".restic-backup-add", () => {
		frm.add_child("backup_schedule", { days: "", backup_time: "" });
		frm.dirty();
		render_schedule_editor(frm);
	});
}

function schedule_problem(rows) {
	if (!rows.length) {
		return __("Add at least one backup schedule.");
	}
	const scheduled_slots = new Map();
	for (const [index, row] of rows.entries()) {
		const days = selected_days(row);
		const time = clock_time(row.backup_time);
		if (!days.length || !time) {
			return __("Choose at least one day and a time in schedule row {0}.", [index + 1]);
		}
		for (const day of days) {
			const slot = `${day}|${time}`;
			if (scheduled_slots.has(slot)) {
				return __("Schedule rows {0} and {1} both include {2} at {3}.", [
					scheduled_slots.get(slot),
					index + 1,
					__(day),
					time,
				]);
			}
			scheduled_slots.set(slot, index + 1);
		}
	}
	return null;
}

async function render_storage_configuration(frm) {
	const configuration = await frappe.xcall(
		"frappe_restic.restic_backup.page.restic_backup_control.restic_backup_control.get_configuration"
	);
	frm.fields_dict.offsite_configuration.$wrapper
		.addClass("restic-backup-settings")
		.html(storage_configuration_html(configuration));
}

function storage_configuration_html(configuration) {
	const status_rows = configuration.required_variables
		.map((variable) => {
			const indicator = variable.configured ? "green" : "red";
			const status = variable.configured ? __("Configured") : __("Missing");
			return `<div class="restic-backup-env-row">
				<code>${frappe.utils.escape_html(variable.name)}</code>
				<span class="indicator-pill ${indicator}">${status}</span>
			</div>`;
		})
		.join("");
	const deployment_status = configuration.offsite_enabled
		? `<span class="indicator-pill green">${__("Enabled")}</span>`
		: `<span class="indicator-pill orange">${__("Disabled")}</span>`;
	const storage_status = configuration.restic_ready
		? `<span class="indicator-pill green">${__("Configured")}</span>`
		: `<span class="indicator-pill red">${__("Credentials missing")}</span>`;
	return `<p class="text-muted">${
		configuration.offsite_enabled
			? __("Off-site uploads are enabled by deployment configuration.")
			: __(
					"Off-site uploads are disabled by deployment configuration. Scheduled and manual backups stay local."
			  )
	}</p>
	<details class="card restic-backup-offsite">
		<summary class="restic-backup-offsite-summary">
			<strong>${__("Storage configuration")}</strong>
			${storage_status}
		</summary>
		<div class="card-body border-top">
			<p class="mb-1">${__(
				"Set these in the deployment environment, not in Frappe, then restart or redeploy."
			)}</p>
			<p class="text-muted small mb-3">${__(
				"Coolify: application Environment Variables. Docker Compose: the .env file supplied to Compose."
			)}</p>
			${status_rows}
			<div class="restic-backup-env-row pt-3">
				<span><code>RESTIC_OFFSITE_BACKUP_ENABLED</code> <span class="text-muted">(${__(
					"all backup uploads"
				)})</span></span>
				${deployment_status}
			</div>
			<p class="text-muted small mt-3 mb-0">${__(
				"RESTIC_REPOSITORY format: s3:https://s3.<region>.backblazeb2.com/<bucket>/restic. Use a bucket-restricted Backblaze application key."
			)}</p>
		</div>
	</details>`;
}
