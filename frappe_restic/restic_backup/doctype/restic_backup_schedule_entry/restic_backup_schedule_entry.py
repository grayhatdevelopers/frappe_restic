"""Child rows for the configurable backup schedule."""

from frappe.model.document import Document


class ResticBackupScheduleEntry(Document):
	"""One set of weekdays and one backup time in the site time zone."""
