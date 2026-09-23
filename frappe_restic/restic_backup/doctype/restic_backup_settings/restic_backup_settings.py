"""Operator-visible backup schedule and retention policy."""

from __future__ import annotations

import re

import frappe
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
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ResticBackupSettings(Document):
	"""Validate schedule inputs without storing repository credentials."""

	def load_from_db(self) -> ResticBackupSettings:
		"""Provide disabled-by-default schedule rows until the policy is saved."""
		super().load_from_db()
		if not self.backup_schedule:
			for backup_time in ("13:00:00", "20:00:00"):
				self.append("backup_schedule", {
					"days": "Monday,Tuesday,Wednesday,Thursday,Friday,Saturday",
					"backup_time": backup_time,
				})
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
			frappe.throw("Local Backup Sets must be between 1 and 30.")
		if not 1 <= cint(self.remote_keep_days) <= 3650:
			frappe.throw("Remote Retention must be between 1 and 3650 days.")

		for email in parse_recipients(self.notification_recipients):
			if not EMAIL_PATTERN.match(email):
				frappe.throw(f"Invalid notification email address: {email}")

		frappe.db.set_single_value("System Settings", "backup_limit", cint(self.local_backup_limit))


def parse_working_days(value: str | None) -> list[str]:
	"""Return a unique, validated weekday list."""
	days = list(dict.fromkeys(part.strip().title() for part in (value or "").split(",") if part.strip()))
	invalid = [day for day in days if day not in WEEKDAYS]
	if invalid:
		frappe.throw(f"Invalid working day(s): {', '.join(invalid)}")
	if not days:
		frappe.throw("At least one working day is required.")
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
			frappe.throw("Each backup schedule row requires a time.")
		backup_time = get_time(row.get("backup_time")).strftime("%H:%M:%S")
		for day in days:
			slot = (day, backup_time)
			if slot in seen_slots:
				frappe.throw(f"Duplicate backup schedule entry: {day} at {backup_time}.")
			seen_slots.add(slot)
		schedule.append((days, backup_time))

	if not schedule:
		frappe.throw("At least one backup schedule row is required.")
	return sorted(schedule, key=lambda row: row[1])


def parse_recipients(value: str | None) -> list[str]:
	"""Parse a comma/newline-separated recipient list."""
	return list(dict.fromkeys(part.strip() for part in re.split(r"[,\n]", value or "") if part.strip()))
