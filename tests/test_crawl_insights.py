"""
Etapa 5 of docs/IMPROVEMENTS_PLAN.md -- deriving findings from stored edges.

Pure functions over dicts, so no database and no fixtures needed here.
"""
import unittest

from core.crawl_insights import (
    anchor_distribution,
    broken_internal_links,
    depth_histogram,
)


class TestAnchorDistribution(unittest.TestCase):
    def test_counts_anchors_and_flags_generic_ones(self):
        edges = [
            {"anchor": "See our pricing", "dest_url": "/pricing", "reason": "content"},
            {"anchor": "click here", "dest_url": "/gone", "reason": "content"},
            {"anchor": "Click Here", "dest_url": "/x", "reason": "content"},
            {"anchor": None, "dest_url": "/y", "reason": "content"},
        ]
        result = anchor_distribution(edges)

        self.assertEqual(result["total"], 4)
        self.assertEqual(result["generic"], 2)      # case-insensitive
        self.assertEqual(result["empty"], 1)
        self.assertEqual(result["descriptive"], 1)

    def test_generic_detection_is_not_substring_greedy(self):
        """
        'Here is our pricing guide' contains 'here' but is descriptive. A
        substring match would flag it and report a real anchor as a MAJOR
        issue, which is worse than missing a generic one.
        """
        edges = [{"anchor": "Here is our pricing guide", "dest_url": "/p", "reason": "content"}]
        self.assertEqual(anchor_distribution(edges)["generic"], 0)
        self.assertEqual(anchor_distribution(edges)["descriptive"], 1)

    def test_surrounding_whitespace_does_not_hide_a_generic_anchor(self):
        edges = [{"anchor": "  Read More  ", "dest_url": "/p", "reason": "content"}]
        self.assertEqual(anchor_distribution(edges)["generic"], 1)

    def test_only_content_edges_are_counted(self):
        """Anchor quality is about body links; a broken nav link is a different finding."""
        edges = [
            {"anchor": "click here", "dest_url": "/a", "reason": "error"},
            {"anchor": "Real anchor", "dest_url": "/b", "reason": "content"},
        ]
        result = anchor_distribution(edges)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["generic"], 0)

    def test_reports_the_most_repeated_anchors(self):
        edges = [
            {"anchor": "pricing", "dest_url": "/p", "reason": "content"},
            {"anchor": "pricing", "dest_url": "/p", "reason": "content"},
            {"anchor": "about", "dest_url": "/a", "reason": "content"},
        ]
        self.assertEqual(anchor_distribution(edges)["most_common"][0], ("pricing", 2))


class TestBrokenInternalLinks(unittest.TestCase):
    def test_groups_broken_destinations_with_their_sources(self):
        edges = [
            {"source_url": "/a", "dest_url": "/gone", "dest_status_code": 404, "reason": "error", "anchor": "x"},
            {"source_url": "/b", "dest_url": "/gone", "dest_status_code": 404, "reason": "error", "anchor": "y"},
            {"source_url": "/a", "dest_url": "/ok", "dest_status_code": 200, "reason": "content", "anchor": "z"},
        ]
        broken = broken_internal_links(edges)

        self.assertEqual(len(broken), 1)
        self.assertEqual(broken[0]["dest_url"], "/gone")
        self.assertEqual(broken[0]["status_code"], 404)
        self.assertEqual(sorted(broken[0]["linked_from"]), ["/a", "/b"])

    def test_server_errors_count_as_broken_too(self):
        edges = [{"source_url": "/a", "dest_url": "/boom", "dest_status_code": 500,
                  "reason": "error", "anchor": None}]
        self.assertEqual(len(broken_internal_links(edges)), 1)

    def test_redirects_are_not_reported_as_broken(self):
        """A 301 is a finding, but not the same finding -- do not conflate them."""
        edges = [{"source_url": "/a", "dest_url": "/moved", "dest_status_code": 301,
                  "reason": "redirect", "anchor": None}]
        self.assertEqual(broken_internal_links(edges), [])

    def test_auth_protected_pages_are_not_reported_as_broken(self):
        """
        From the first real crawl: the footer linked to app.nrankai.com, which
        returned 401 because it is behind BasicAuth. The page exists -- calling
        it a broken link would put a false finding in an SEO report.
        """
        edges = [
            {"source_url": "/a", "dest_url": "/admin", "dest_status_code": 401,
             "reason": "auth", "anchor": None},
            {"source_url": "/a", "dest_url": "/secret", "dest_status_code": 403,
             "reason": "auth", "anchor": None},
        ]
        self.assertEqual(broken_internal_links(edges), [])

    def test_unknown_status_is_not_guessed_as_broken(self):
        edges = [{"source_url": "/a", "dest_url": "/x", "dest_status_code": None,
                  "reason": "content", "anchor": None}]
        self.assertEqual(broken_internal_links(edges), [])


class TestDepthHistogram(unittest.TestCase):
    def test_buckets_pages_by_crawl_depth(self):
        pages = [
            {"crawl_depth": 0}, {"crawl_depth": 1}, {"crawl_depth": 1},
            {"crawl_depth": 4}, {"crawl_depth": None},
        ]
        self.assertEqual(depth_histogram(pages), {0: 1, 1: 2, 4: 1})

    def test_pages_with_unknown_depth_are_skipped_not_bucketed_as_zero(self):
        """Bucketing unknown as 0 would report uncrawled pages as the home page."""
        self.assertEqual(depth_histogram([{"crawl_depth": None}]), {})


if __name__ == "__main__":
    unittest.main()
