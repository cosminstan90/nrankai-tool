"""Pasul 10 of docs/superpowers/plans/2026-09-30-next-steps.md."""
import unittest

from core.url_normalize import normalize_url


class TestNormalizeUrl(unittest.TestCase):
    def test_scheme_is_ignored(self):
        self.assertEqual(normalize_url("http://ing.ro/oferta"), normalize_url("https://ing.ro/oferta"))

    def test_www_is_stripped(self):
        self.assertEqual(normalize_url("https://www.ing.ro/oferta"), normalize_url("https://ing.ro/oferta"))

    def test_trailing_slash_is_stripped_on_a_non_root_path(self):
        self.assertEqual(normalize_url("https://ing.ro/oferta/"), normalize_url("https://ing.ro/oferta"))

    def test_root_path_stays_a_single_slash(self):
        self.assertEqual(normalize_url("https://ing.ro"), "ing.ro/")
        self.assertEqual(normalize_url("https://ing.ro/"), "ing.ro/")

    def test_query_string_and_fragment_are_dropped(self):
        self.assertEqual(normalize_url("https://ing.ro/oferta?utm=abc#section"),
                         normalize_url("https://ing.ro/oferta"))

    def test_bare_domain_without_scheme_is_accepted(self):
        self.assertEqual(normalize_url("ing.ro/oferta"), normalize_url("https://ing.ro/oferta"))

    def test_host_is_lowercased(self):
        self.assertEqual(normalize_url("https://ING.RO/oferta"), normalize_url("https://ing.ro/oferta"))

    def test_empty_input_is_empty_output_not_a_match_for_anything(self):
        self.assertEqual(normalize_url(""), "")
        self.assertEqual(normalize_url(None), "")

    def test_different_pages_stay_different(self):
        self.assertNotEqual(normalize_url("https://ing.ro/oferta"), normalize_url("https://ing.ro/credite"))


if __name__ == "__main__":
    unittest.main()
