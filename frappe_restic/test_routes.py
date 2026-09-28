import re
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parent


class TestDeskRoutes(unittest.TestCase):
	def test_client_scripts_do_not_hard_code_the_desk_prefix(self):
		# Frappe v15 serves the desk at /app, v16 at /desk; frappe.router.make_url picks.
		offenders = [
			f"{path.relative_to(APP)}:{number}"
			for path in APP.rglob("*.js")
			if "dist" not in path.parts
			for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
			if re.search(r"""["'`]/(app|desk)(/|["'`])""", line)
		]
		self.assertEqual(offenders, [])
