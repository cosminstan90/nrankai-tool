"""
Pins the fix from Etapa 4.9 of the consolidation (docs/CONSOLIDATION_PLAN.md):
domain.lstrip("www.") strips a character SET ({'w', '.'}), not a literal
prefix -- silently corrupting any domain starting with the letter "w" that
isn't actually "www.". This exact bug was duplicated across ~10 files
wherever a domain needed www-normalisation before comparison. strip_www()
replaces every occurrence.
"""
import unittest

from api.utils.domain import strip_www


class TestStripWww(unittest.TestCase):
    def test_strips_literal_www_prefix(self):
        self.assertEqual(strip_www("www.example.com"), "example.com")

    def test_does_not_corrupt_domains_starting_with_w(self):
        self.assertEqual(strip_www("wework.com"), "wework.com")
        self.assertEqual(strip_www("webflow.com"), "webflow.com")
        self.assertEqual(strip_www("weebly.com"), "weebly.com")
        self.assertEqual(strip_www("wetransfer.com"), "wetransfer.com")

    def test_no_www_prefix_unchanged(self):
        self.assertEqual(strip_www("example.com"), "example.com")

    def test_case_insensitive_prefix_check(self):
        self.assertEqual(strip_www("WWW.example.com"), "example.com")

    def test_lstrip_would_have_corrupted_this(self):
        """Sanity check documenting the exact bug being fixed."""
        self.assertNotEqual("wework.com".lstrip("www."), "wework.com")
        self.assertEqual(strip_www("wework.com"), "wework.com")


if __name__ == "__main__":
    unittest.main()
