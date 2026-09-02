"""
Pins the fix from Etapa 4.9 of the consolidation (docs/CONSOLIDATION_PLAN.md):
SERPFetcher._parse_item() used a blocklist ("paid", "shopping", "app", "map")
to decide which DataForSEO SERP items to surface. That list was incomplete:
real-world SERPs commonly include "ai_overview" and "people_also_ask" blocks
that carry no url/domain/title of their own -- they passed through the
blocklist unfiltered, producing hollow SERPItem rows that consumed a
ranked-position slot in the "top 20" snapshot, silently displacing a real
organic result. Confirmed live: a real DataForSEO response for "best crm
software" had an ai_overview block at rank 1 and a people_also_ask block at
rank 3, both fully null except position -- 2 of 20 persisted rows were
blank junk instead of real competitors.

_VALID_ITEM_TYPES switches this to an allow-list (organic, featured_snippet,
local_pack, video) using the _ORGANIC_TYPES constant that was already
defined but never wired into the filter.
"""
import unittest

from app.modules.serpiq.services.serp_fetcher import SERPFetcher


def _dfs_response(items: list) -> dict:
    return {"tasks": [{"status_code": 20000, "result": [{"items": items}]}]}


class TestSerpItemTypeFiltering(unittest.TestCase):
    def setUp(self):
        # SERPFetcher.__init__ requires DataForSEO credentials; bypass it
        # since these tests only exercise the pure parsing logic.
        self.fetcher = SERPFetcher.__new__(SERPFetcher)

    def test_ai_overview_block_is_excluded(self):
        raw = {
            "tasks": [{
                "status_code": 20000,
                "result": [{"items": [
                    {"type": "ai_overview", "rank_absolute": 1, "markdown": "..."},
                    {"type": "organic", "rank_absolute": 2, "url": "https://a.com", "domain": "a.com", "title": "A"},
                ]}],
            }]
        }
        items = self.fetcher._parse_response(raw, depth=20)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].domain, "a.com")

    def test_people_also_ask_block_is_excluded(self):
        raw = _dfs_response([
            {"type": "people_also_ask", "rank_absolute": 1, "items": []},
            {"type": "organic", "rank_absolute": 2, "url": "https://a.com", "domain": "a.com", "title": "A"},
        ])
        items = self.fetcher._parse_response(raw, depth=20)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].domain, "a.com")

    def test_organic_featured_snippet_local_pack_video_are_kept(self):
        raw = _dfs_response([
            {"type": "organic", "rank_absolute": 1, "url": "https://a.com", "domain": "a.com", "title": "A"},
            {"type": "featured_snippet", "rank_absolute": 2, "url": "https://b.com", "domain": "b.com", "title": "B"},
            {"type": "local_pack", "rank_absolute": 3, "url": "https://c.com", "domain": "c.com", "title": "C"},
            {"type": "video", "rank_absolute": 4, "url": "https://d.com", "domain": "d.com", "title": "D"},
        ])
        items = self.fetcher._parse_response(raw, depth=20)
        self.assertEqual(len(items), 4)
        self.assertEqual([i.domain for i in items], ["a.com", "b.com", "c.com", "d.com"])

    def test_no_blank_rows_ever_persisted(self):
        """Every parsed item must carry real content -- no junk block should
        ever produce a row with everything None but position."""
        raw = _dfs_response([
            {"type": "ai_overview", "rank_absolute": 1},
            {"type": "organic", "rank_absolute": 2, "url": "https://a.com", "domain": "a.com", "title": "A"},
            {"type": "people_also_ask", "rank_absolute": 3},
            {"type": "knowledge_graph", "rank_absolute": 4},
            {"type": "organic", "rank_absolute": 5, "url": "https://b.com", "domain": "b.com", "title": "B"},
        ])
        items = self.fetcher._parse_response(raw, depth=20)
        self.assertEqual(len(items), 2)
        for item in items:
            self.assertIsNotNone(item.domain)


if __name__ == "__main__":
    unittest.main()
