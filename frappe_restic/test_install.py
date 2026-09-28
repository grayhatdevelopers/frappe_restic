import unittest

from frappe_restic.install import SUPPORTED_MAJORS, is_supported_version


class TestSupportedVersion(unittest.TestCase):
	def test_accepts_v15_and_v16(self):
		for version in ("15.40.4", "15.121.0", "16.0.0", "16.27.0", "16.36.0-dev"):
			with self.subTest(version=version):
				self.assertTrue(is_supported_version(version))

	def test_rejects_other_majors(self):
		for version in ("14.99.0", "17.0.0", "", "develop"):
			with self.subTest(version=version):
				self.assertFalse(is_supported_version(version))

	def test_supported_majors_are_documented(self):
		self.assertEqual(SUPPORTED_MAJORS, ("15", "16"))
