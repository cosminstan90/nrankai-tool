"""
Pasul 14 of docs/superpowers/plans/2026-09-30-next-steps.md.

Synthetic crawl, as the plan asks for -- core.internal_links is pure logic
with no DB/network access. Fixed vectors stand in for real embeddings:
close vectors = topically similar pages, opposite/orthogonal = unrelated.
"""
import unittest

from core.internal_links import (
    build_suggestions, find_candidate_sources, find_link_targets, suggest_anchor,
)

CLOSE_A = [1.0, 0.0, 0.0]
CLOSE_B = [0.95, 0.05, 0.0]   # close to CLOSE_A, similarity well above the 0.75 threshold
UNRELATED = [0.0, 1.0, 0.0]   # orthogonal to CLOSE_A -- similarity 0.0


class TestFindLinkTargets(unittest.TestCase):
    def _pages(self):
        return [
            {"url": "/orphan", "content_inlinks": 5, "is_orphan": True, "indexability": "indexable"},
            {"url": "/starved", "content_inlinks": 1, "is_orphan": False, "indexability": "indexable"},
            {"url": "/healthy", "content_inlinks": 10, "is_orphan": False, "indexability": "indexable"},
            {"url": "/noindex-starved", "content_inlinks": 0, "is_orphan": False, "indexability": "noindex"},
            {"url": "/broken-starved", "content_inlinks": 0, "is_orphan": False, "indexability": "indexable",
             "status_code": 404},
        ]

    def test_orphaned_and_starved_pages_are_targets(self):
        targets = find_link_targets(self._pages())
        urls = {t["url"] for t in targets}
        self.assertEqual(urls, {"/orphan", "/starved"})

    def test_healthy_pages_are_not_targets(self):
        targets = find_link_targets(self._pages())
        urls = {t["url"] for t in targets}
        self.assertNotIn("/healthy", urls)

    def test_non_indexable_pages_are_never_targets_even_if_starved(self):
        targets = find_link_targets(self._pages())
        urls = {t["url"] for t in targets}
        self.assertNotIn("/noindex-starved", urls)

    def test_4xx_pages_are_never_targets_even_if_starved(self):
        targets = find_link_targets(self._pages())
        urls = {t["url"] for t in targets}
        self.assertNotIn("/broken-starved", urls)

    def test_orphaned_targets_come_before_merely_starved_ones(self):
        targets = find_link_targets(self._pages())
        self.assertEqual(targets[0]["url"], "/orphan")

    def test_restricting_to_opportunity_urls_excludes_everything_else(self):
        targets = find_link_targets(self._pages(), opportunity_urls={"/starved"})
        urls = {t["url"] for t in targets}
        self.assertEqual(urls, {"/starved"})


class TestFindCandidateSources(unittest.TestCase):
    def _pages(self):
        return [
            {"url": "/target", "indexability": "indexable"},
            {"url": "/close-source", "indexability": "indexable"},
            {"url": "/already-linked", "indexability": "indexable"},
            {"url": "/unrelated-source", "indexability": "indexable"},
            {"url": "/noindex-source", "indexability": "noindex"},
            {"url": "/broken-source", "indexability": "indexable", "status_code": 500},
        ]

    def _vectors(self):
        return {
            "/target": CLOSE_A, "/close-source": CLOSE_B, "/already-linked": CLOSE_B,
            "/unrelated-source": UNRELATED, "/noindex-source": CLOSE_B, "/broken-source": CLOSE_B,
        }

    def test_a_topically_close_indexable_page_is_a_candidate(self):
        sources = find_candidate_sources("/target", CLOSE_A, self._pages(), self._vectors(), existing_links=set())
        urls = {s["url"] for s in sources}
        self.assertIn("/close-source", urls)

    def test_an_unrelated_page_is_not_a_candidate(self):
        sources = find_candidate_sources("/target", CLOSE_A, self._pages(), self._vectors(), existing_links=set())
        urls = {s["url"] for s in sources}
        self.assertNotIn("/unrelated-source", urls)

    def test_a_page_that_already_links_to_the_target_is_excluded(self):
        existing = {("/already-linked", "/target")}
        sources = find_candidate_sources("/target", CLOSE_A, self._pages(), self._vectors(), existing_links=existing)
        urls = {s["url"] for s in sources}
        self.assertNotIn("/already-linked", urls)

    def test_a_noindex_page_is_never_a_candidate_even_if_topically_close(self):
        sources = find_candidate_sources("/target", CLOSE_A, self._pages(), self._vectors(), existing_links=set())
        urls = {s["url"] for s in sources}
        self.assertNotIn("/noindex-source", urls)

    def test_a_4xx_5xx_page_is_never_a_candidate_even_if_topically_close(self):
        sources = find_candidate_sources("/target", CLOSE_A, self._pages(), self._vectors(), existing_links=set())
        urls = {s["url"] for s in sources}
        self.assertNotIn("/broken-source", urls)

    def test_the_target_itself_is_never_its_own_candidate(self):
        sources = find_candidate_sources("/target", CLOSE_A, self._pages(), self._vectors(), existing_links=set())
        urls = {s["url"] for s in sources}
        self.assertNotIn("/target", urls)

    def test_a_page_with_no_vector_yet_is_skipped_not_an_error(self):
        pages = self._pages() + [{"url": "/not-embedded-yet", "indexability": "indexable"}]
        sources = find_candidate_sources("/target", CLOSE_A, pages, self._vectors(), existing_links=set())
        urls = {s["url"] for s in sources}
        self.assertNotIn("/not-embedded-yet", urls)


class TestSuggestAnchor(unittest.TestCase):
    def test_the_highest_impression_query_wins(self):
        result = suggest_anchor([
            {"query": "low volume term", "impressions": 10},
            {"query": "credit ipotecar", "impressions": 500},
        ])
        self.assertEqual(result["anchor"], "credit ipotecar")

    def test_no_queries_at_all_is_none_not_invented_text(self):
        self.assertIsNone(suggest_anchor([]))


class TestBuildSuggestions(unittest.TestCase):
    def test_a_target_with_a_qualifying_source_gets_a_suggestion(self):
        pages = [
            {"url": "/target", "indexability": "indexable"},
            {"url": "/source", "indexability": "indexable"},
        ]
        vectors = {"/target": CLOSE_A, "/source": CLOSE_B}
        targets = [{"url": "/target", "content_inlinks": 0, "is_orphan": True}]
        queries = {"/target": [{"query": "credit ipotecar", "impressions": 500}]}

        suggestions = build_suggestions(targets, pages, vectors, existing_links=set(), queries_by_url=queries)

        self.assertEqual(len(suggestions), 1)
        s = suggestions[0]
        self.assertEqual(s["target_url"], "/target")
        self.assertEqual(s["anchor"], "credit ipotecar")
        self.assertEqual(s["candidate_sources"][0]["url"], "/source")
        self.assertIn("orphaned", s["reason"])

    def test_a_target_with_no_qualifying_source_is_left_out(self):
        pages = [
            {"url": "/target", "indexability": "indexable"},
            {"url": "/unrelated", "indexability": "indexable"},
        ]
        vectors = {"/target": CLOSE_A, "/unrelated": UNRELATED}
        targets = [{"url": "/target", "content_inlinks": 0, "is_orphan": True}]

        suggestions = build_suggestions(targets, pages, vectors, existing_links=set(), queries_by_url={})
        self.assertEqual(suggestions, [])

    def test_a_target_with_no_embedding_yet_is_left_out_not_an_error(self):
        pages = [{"url": "/target", "indexability": "indexable"},
                {"url": "/source", "indexability": "indexable"}]
        targets = [{"url": "/target", "content_inlinks": 0, "is_orphan": True}]

        suggestions = build_suggestions(targets, pages, vectors={}, existing_links=set(), queries_by_url={})
        self.assertEqual(suggestions, [])

    def test_reason_mentions_content_inlink_count_for_a_merely_starved_target(self):
        pages = [{"url": "/target", "indexability": "indexable"},
                {"url": "/source", "indexability": "indexable"}]
        vectors = {"/target": CLOSE_A, "/source": CLOSE_B}
        targets = [{"url": "/target", "content_inlinks": 1, "is_orphan": False}]

        suggestions = build_suggestions(targets, pages, vectors, existing_links=set(), queries_by_url={})
        self.assertIn("1 content link", suggestions[0]["reason"])


if __name__ == "__main__":
    unittest.main()
