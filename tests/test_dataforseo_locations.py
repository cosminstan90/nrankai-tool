"""
DataForSEO location codes and market resolution.

The codes were checked against DataForSEO's own reference lists before this
module existed; these tests pin them so a well-meaning edit cannot quietly
reintroduce the values that were wrong in four places (Romania as 2040, which
is Austria, and as 1037, which does not exist).
"""
import unittest

from core.dataforseo_locations import LOCATIONS, get, resolve_serp_location


class TestVerifiedCodes(unittest.TestCase):
    def test_romania_is_2642(self):
        """2040 is Austria; 1037 is on neither DataForSEO list."""
        self.assertEqual(get("RO").location_code, 2642)
        self.assertEqual(get("RO").language_code, "ro")

    def test_no_preset_uses_a_code_known_to_be_wrong_for_romania(self):
        codes = {loc.location_code for loc in LOCATIONS.values()}
        self.assertNotIn(2040, codes)
        self.assertNotIn(1037, codes)

    def test_other_countries_match_the_verified_list(self):
        expected = {"US": 2840, "UK": 2826, "DE": 2276, "FR": 2250,
                    "IT": 2380, "ES": 2724, "PL": 2616, "NL": 2528, "BG": 2100}
        for key, code in expected.items():
            with self.subTest(key=key):
                self.assertEqual(get(key).location_code, code)

    def test_lookup_is_case_insensitive(self):
        self.assertEqual(get("ro"), get("RO"))


class TestKeywordResearchPresets(unittest.TestCase):
    def test_every_country_the_ui_offered_is_still_offered(self):
        """Deriving the presets from this module must not drop a country."""
        from api.routes.keyword_research import LOCATION_PRESETS
        self.assertEqual(sorted(LOCATION_PRESETS),
                         ["BG", "DE", "ES", "FR", "IT", "NL", "PL", "RO", "UK", "US"])

    def test_romanian_keyword_research_now_uses_a_real_code(self):
        from api.routes.keyword_research import LOCATION_PRESETS
        self.assertEqual(LOCATION_PRESETS["RO"]["location_code"], 2642)


class TestResolveSerpLocation(unittest.TestCase):
    def test_tld_outranks_a_default_language(self):
        """
        The real tracker: ing.ro with language="English" (the column default)
        and Romanian queries. It must resolve to Romania, not the US.
        """
        self.assertEqual(resolve_serp_location("ing.ro", language="English").key, "RO")

    def test_full_urls_and_subdomains_resolve_by_tld(self):
        self.assertEqual(resolve_serp_location("https://www.bancatransilvania.ro/credite").key, "RO")
        self.assertEqual(resolve_serp_location("shop.example.co.uk").key, "UK")

    def test_generic_tld_falls_back_to_language(self):
        self.assertEqual(resolve_serp_location("example.com", language="Romanian").key, "RO")

    def test_generic_tld_and_no_useful_language_falls_back_to_us(self):
        self.assertEqual(resolve_serp_location("example.com", language="English").key, "US")
        self.assertEqual(resolve_serp_location("example.io").key, "US")

    def test_explicit_override_wins_over_everything(self):
        """A Romanian brand on .com, or a .ro site deliberately tracked abroad."""
        self.assertEqual(resolve_serp_location("brand.com", language="English", override="RO").key, "RO")
        self.assertEqual(resolve_serp_location("ing.ro", override="UK").key, "UK")

    def test_unknown_override_is_ignored_rather_than_crashing(self):
        self.assertEqual(resolve_serp_location("ing.ro", override="XX").key, "RO")


if __name__ == "__main__":
    unittest.main()
