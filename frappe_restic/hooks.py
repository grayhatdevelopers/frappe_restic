app_name = "frappe_restic"
app_title = "Restic Backups"
app_publisher = "Grayhat Developers"
app_description = "Backup scheduling, encrypted snapshots and recovery for Frappe"
app_email = ""
app_license = "MIT"
required_apps = ["frappe"]

page_js = {"backups": "public/js/restic_backups_page.js"}
before_install = "frappe_restic.install.after_install"
after_install = "frappe_restic.install.after_install"
after_migrate = "frappe_restic.restic_backup.backup_control.sync_backup_defaults"
scheduler_events = {
    "daily_long": ["frappe_restic.restic_backup.backup_control.daily_maintenance"],
    "weekly_long": ["frappe_restic.restic_backup.backup_control.weekly_maintenance"],
    "cron": {"*/15 * * * *": [
        "frappe_restic.restic_backup.backup_control.schedule_due_backups",
        "frappe_restic.restic_backup.backup_control.report_operation_outcomes",
    ]},
}

app_include_css = ["restic_backups.bundle.css"]
