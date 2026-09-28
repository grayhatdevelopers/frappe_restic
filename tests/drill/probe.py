"""Seed, change and fingerprint what a restore or rollback must bring back.

Run inside the drill image: env/bin/python /drill/probe.py {seed,add,damage,fingerprint}
"""

from __future__ import annotations

import hashlib
import json
import os
import sys

import frappe

SITE = os.environ["SITE_NAME"]
SECRET = ("ToDo", "drill-secret", "drill_secret")


def add(count: int) -> None:
	for index in range(count):
		frappe.get_doc(
			{"doctype": "ToDo", "description": f"drill record {os.urandom(4).hex()} {index}"}
		).insert()


def seed() -> None:
	from frappe.utils.password import set_encrypted_password

	add(25)
	# Creates the site encryption key; a restore must bring back a key that decrypts it.
	doctype, name, fieldname = SECRET
	set_encrypted_password(doctype, name, os.urandom(16).hex(), fieldname)
	for private in (0, 1):
		frappe.get_doc(
			{
				"doctype": "File",
				"file_name": f"drill-{'private' if private else 'public'}.txt",
				"is_private": private,
				"content": os.urandom(4096).hex(),
			}
		).insert()


def damage() -> None:
	"""Stand-in for a migration that fails after changing data."""
	frappe.db.delete("ToDo", {"description": ["like", "drill record %"]})
	for name in frappe.get_all("File", filters={"file_name": ["like", "drill-%"]}, pluck="name"):
		frappe.delete_doc("File", name)


def fingerprint() -> dict:
	from frappe.utils.password import get_decrypted_password

	files = {}
	for name in frappe.get_all("File", filters={"file_name": ["like", "drill-%"]}, pluck="name"):
		document = frappe.get_doc("File", name)
		content = document.get_content()
		files[document.file_name] = hashlib.sha256(
			content.encode() if isinstance(content, str) else content
		).hexdigest()
	secret = get_decrypted_password(*SECRET, raise_exception=False) or ""
	return {
		"todos": frappe.db.count("ToDo", {"description": ["like", "drill record %"]}),
		"files": dict(sorted(files.items())),
		"encryption_key_sha256": hashlib.sha256(
			(frappe.conf.get("encryption_key") or "").encode()
		).hexdigest(),
		"secret_sha256": hashlib.sha256(secret.encode()).hexdigest(),
	}


def main() -> None:
	os.chdir("/home/frappe/frappe-bench/sites")
	frappe.init(site=SITE)
	frappe.connect()
	try:
		action = sys.argv[1]
		if action == "fingerprint":
			print(json.dumps(fingerprint(), sort_keys=True))
		else:
			{"seed": seed, "add": lambda: add(5), "damage": damage}[action]()
			frappe.db.commit()
	finally:
		frappe.destroy()


if __name__ == "__main__":
	main()
