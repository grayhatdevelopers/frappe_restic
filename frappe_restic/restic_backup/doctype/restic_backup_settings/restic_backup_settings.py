"""Operator-visible backup schedule and retention policy."""

from __future__ import annotations

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, get_time

WEEKDAYS = (
	"Monday",
	"Tuesday",
	"Wednesday",
	"Thursday",
	"Friday",
	"Saturday",
	"Sunday",
)


class ResticBackupSettings(Document):
	"""Validate schedule inputs without storing repository credentials."""

	def load_from_db(self) -> ResticBackupSettings:
		"""Provide disabled-by-default schedule rows until the policy is saved."""
		super().load_from_db()
		if not self.backup_schedule:
			for backup_time in ("13:00:00", "20:00:00"):
				self.append(
					"backup_schedule",
					{
						"days": "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday",
						"backup_time": backup_time,
					},
				)
		return self

	def validate(self) -> None:
		schedule = parse_backup_schedule(self.backup_schedule)
		self.set("backup_schedule", [])
		for days, backup_time in schedule:
			self.append(
				"backup_schedule",
				{"days": ",".join(days), "backup_time": backup_time},
			)

		if not 1 <= cint(self.local_backup_limit) <= 30:
			frappe.throw(_("Local Backup Sets must be between 1 and 30."))
		if not 1 <= cint(self.remote_keep_days) <= 3650:
			frappe.throw(_("Off-site Retention must be between 1 and 3650 days."))

		frappe.db.set_single_value("System Settings", "backup_limit", cint(self.local_backup_limit))


def parse_working_days(value: str | None) -> list[str]:
	"""Return a unique, validated weekday list."""
	days = list(dict.fromkeys(part.strip().title() for part in (value or "").split(",") if part.strip()))
	invalid = [day for day in days if day not in WEEKDAYS]
	if invalid:
		frappe.throw(_("Invalid working day(s): {0}").format(", ".join(invalid)))
	if not days:
		frappe.throw(_("At least one working day is required."))
	return days


def parse_backup_schedule(rows) -> list[tuple[list[str], str]]:
	"""Return validated rows of selected weekdays and one time per row."""
	schedule: list[tuple[list[str], str]] = []
	seen_slots: set[tuple[str, str]] = set()
	for row in rows or []:
		days_value = row.get("days") or []
		if isinstance(days_value, (list, tuple)):
			days_value = ",".join(str(day) for day in days_value)
		days = parse_working_days(str(days_value))
		if not row.get("backup_time"):
			frappe.throw(_("Each backup schedule row requires a time."))
		backup_time = get_time(row.get("backup_time")).strftime("%H:%M:%S")
		for day in days:
			slot = (day, backup_time)
			if slot in seen_slots:
				frappe.throw(_("Duplicate backup schedule entry: {0} at {1}.").format(day, backup_time))
			seen_slots.add(slot)
		schedule.append((days, backup_time))

	if not schedule:
		frappe.throw(_("At least one backup schedule row is required."))
	return sorted(schedule, key=lambda row: row[1])
