(() => {
	const escape = frappe.utils.escape_html;
	const colors = {
		Succeeded: "green", Available: "green", Failed: "red", Invalid: "red",
		Missing: "red", Running: "blue", Uploading: "blue", Queued: "orange",
		Pending: "orange", "Local Only": "orange", Expired: "gray", "Not Attempted": "gray",
	};
	const cell = (label, content, detail = "") =>
		`<span class="restic-run-cell"><span class="restic-run-label">${escape(__(label))}</span>
		${content}${detail ? `<span class="restic-run-detail" title="${escape(detail)}">${escape(detail)}</span>` : ""}</span>`;
	const badge = (value, fieldname) =>
		`<span class="indicator-pill ${colors[value] || "gray"} filterable"
			data-filter="${fieldname},=,${escape(value || "")}">${escape(__(value || "Not recorded"))}</span>`;
	const date = (value) => value ? frappe.datetime.str_to_user(value) : __("Not recorded");

	frappe.listview_settings["Restic Backup Run"] = {
		hide_name_column: true,
		hide_name_filter: true,
		add_fields: ["source", "started_at", "creation", "status", "local_status",
			"remote_status", "remote_verified_at", "error_summary", "local_bytes", "duration_seconds"],
		onload(listview) {
			listview.page.set_title(__("Backup Runs"));
			listview.page.main.addClass("restic-backup-runs");
			// Frappe counts the hidden tag column toward its screen-dependent limit.
			// Preserve saved custom layouts while keeping recovery information visible by default.
			if (!listview.list_view_settings.fields) {
				listview.list_view_settings.fields = JSON.stringify(
					["status", "local_status", "remote_status", "local_bytes", "duration_seconds"]
						.map((fieldname) => ({ fieldname }))
				);
				listview.list_view_settings.total_fields = 7;
				listview.setup_columns();
				listview.render_header(true);
			}
			listview.page.add_inner_button(__("Backup Control"), () => frappe.set_route("restic-backup-control"));
			frappe.breadcrumbs.add({ type: "Custom", route: "/app/restic-backup-control", label: __("Backup Control") });
		},
		formatters: {
			// Frappe inserts subject text with textContent, not HTML.
			started_at(value, df, doc) {
				return `${date(doc.started_at || doc.creation)}\n${__(doc.source || "Backup")} · ${__(doc.started_at ? "Started" : "Created")}`;
			},
			status(value, df, doc) {
				return cell("Result", badge(value, "status"), doc.error_summary);
			},
			local_status(value) {
				return cell("Local · last known", badge(value, "local_status"));
			},
			remote_status(value, df, doc) {
				const checked = doc.remote_verified_at
					? __("Checked {0}", [date(doc.remote_verified_at)]) : __("Not verified");
				return cell("Off-site · last known", badge(value, "remote_status"), checked);
			},
			local_bytes(value, df, doc) {
				const bytes = Number(doc.local_bytes);
				let display = "—";
				if (Number.isFinite(bytes) && bytes > 0) {
					const unit = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), 4);
					display = `${(bytes / 1024 ** unit).toFixed(unit ? 1 : 0)} ${["B", "KiB", "MiB", "GiB", "TiB"][unit]}`;
				}
				return cell("Local size", escape(display));
			},
			duration_seconds(value, df, doc) {
				const seconds = Number(doc.duration_seconds);
				let display = "—";
				if (doc.duration_seconds != null && Number.isFinite(seconds) && seconds >= 0
					&& !["Queued", "Running"].includes(doc.status)) {
					display = seconds < 60 ? `${seconds.toFixed(1)} s`
						: `${Math.floor(seconds / 60)} min ${Math.floor(seconds % 60)} s`;
				}
				return cell("Duration", escape(display));
			},
		},
	};
})();
