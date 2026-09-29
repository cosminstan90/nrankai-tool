"""
The unified SERP client, tested against a real DataForSEO response.

tests/fixtures/serp/ro_banca.json is an unmodified response for
"cea mai buna banca din romania" at location 2642 (Romania), captured
2026-09-29. It has an AI Overview above the organic results, a
people_also_ask block and image packs between them, and the same domains
appearing twice -- every case the parser has to get right.
"""
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from core.serp_client import (
    host_matches_site,
    normalize_host,
    parse_serp_response,
)

FIXTURE = Path(__file__).parent / "fixtures" / "serp" / "ro_banca.json"


def _real() :
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return parse_serp_response(data, "cea mai buna banca din romania", 2642, "ro", 20)


class TestParseRealResponse(unittest.TestCase):
    def setUp(self):
        self.serp = _real()

    def test_extracts_organic_results_in_page_order(self):
        ranks = [r.rank_group for r in self.serp.organic]
        self.assertEqual(ranks, sorted(ranks))
        self.assertEqual(self.serp.organic[0].rank_group, 1)
        self.assertEqual(self.serp.organic[0].domain, "1asig.ro")

    def test_keeps_rank_group_and_rank_absolute_apart(self):
        """
        The first organic result is #1 organically but #2 on the page, because
        the AI Overview sits above it. Collapsing the two would hide exactly
        what a GEO tool exists to show.
        """
        first = self.serp.organic[0]
        self.assertEqual(first.rank_group, 1)
        self.assertEqual(first.rank_absolute, 2)
        self.assertGreater(self.serp.organic[-1].rank_absolute, self.serp.organic[-1].rank_group)

    def test_feature_blocks_are_not_counted_as_organic(self):
        domains = [r.domain for r in self.serp.organic]
        self.assertTrue(all(domains))
        self.assertEqual(len(self.serp.organic), 17)

    def test_records_which_serp_features_were_present(self):
        self.assertEqual(self.serp.features[0], "ai_overview")
        self.assertIn("people_also_ask", self.serp.features)
        self.assertIn("images", self.serp.features)

    def test_parses_the_ai_overview_from_the_same_response(self):
        """One call, both answers -- the point of this module."""
        self.assertIsNotNone(self.serp.ai_overview)
        self.assertTrue(self.serp.ai_overview["references"])


class TestSiteRank(unittest.TestCase):
    def setUp(self):
        self.serp = _real()

    def test_best_position_wins_when_a_site_ranks_twice(self):
        """www.1asig.ro ranks 1 and 1asig.ro ranks 13 -- same site, rank 1."""
        hit = self.serp.site_rank("1asig.ro")
        self.assertEqual(hit.rank_group, 1)

    def test_www_and_scheme_do_not_matter(self):
        self.assertEqual(self.serp.site_rank("https://www.1asig.ro/").rank_group, 1)

    def test_absent_site_has_no_rank_rather_than_a_fake_number(self):
        """ing.ro is genuinely not in this SERP."""
        self.assertIsNone(self.serp.site_rank("ing.ro"))

    def test_a_suffix_that_is_not_a_subdomain_does_not_match(self):
        """notzf.ro must not count as zf.ro."""
        self.assertFalse(host_matches_site("notzf.ro", "zf.ro"))
        self.assertTrue(host_matches_site("www.zf.ro", "zf.ro"))
        self.assertTrue(host_matches_site("blog.zf.ro", "zf.ro"))


class TestNormalizeHost(unittest.TestCase):
    def test_handles_bare_domains_urls_and_www(self):
        self.assertEqual(normalize_host("www.ING.ro"), "ing.ro")
        self.assertEqual(normalize_host("https://www.ing.ro/credite"), "ing.ro")
        self.assertEqual(normalize_host(None), "")


class TestTaskErrors(unittest.TestCase):
    def test_task_error_returns_none(self):
        data = {"tasks": [{"status_code": 40501, "status_message": "Invalid Field", "result": None}]}
        self.assertIsNone(parse_serp_response(data, "x", 2642, "ro", 20))


class TestAiOverviewClientDelegates(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_ai_overview_reuses_the_unified_client(self):
        """
        core/ai_overview_client.py used to make its own HTTP call. It now goes
        through the same client, so there is one caller of this endpoint.
        """
        import core.ai_overview_client as aio

        data = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with patch("core.serp_client.fetch_raw", AsyncMock(return_value=data)) as raw:
            result = await aio.fetch_ai_overview("cea mai buna banca din romania",
                                                 location_code=2642, language_code="ro")
        raw.assert_awaited_once()
        self.assertIsNotNone(result)
        self.assertTrue(result["references"])


if __name__ == "__main__":
    unittest.main()
