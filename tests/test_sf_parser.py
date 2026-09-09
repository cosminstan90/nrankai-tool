"""
Etapa 5 of docs/IMPROVEMENTS_PLAN.md -- parsing Screaming Frog CSV exports.

Runs against committed fixtures trimmed from a real export (tests/fixtures/sf),
so no Screaming Frog install is needed. The fixtures carry a UTF-8 BOM and CRLF
endings exactly as SF writes them.
"""
import unittest
from pathlib import Path

from core.sf_parser import parse_inlinks, parse_internal_html

FIXTURES = Path(__file__).parent / "fixtures" / "sf"


class TestParseInternalHtml(unittest.TestCase):
    def test_parses_pages_with_depth_and_inlink_counts(self):
        pages = parse_internal_html(FIXTURES / "internal_html.csv")

        self.assertEqual(len(pages), 4)
        home = next(p for p in pages if p["url"] == "https://example.com/")
        self.assertEqual(home["crawl_depth"], 0)
        self.assertEqual(home["status_code"], 200)
        self.assertEqual(home["inlinks_total"], 5)
        self.assertEqual(home["unique_inlinks"], 3)
        self.assertEqual(home["outlinks_total"], 12)
        self.assertEqual(home["indexability"], "Indexable")

    def test_bom_does_not_corrupt_the_first_column(self):
        """
        SF writes UTF-8 WITH a BOM. Parsed as plain utf-8 the first header
        becomes '﻿Address', so row["Address"] is None for every row and
        the whole export silently yields nothing -- data loss with no error.
        The fixtures carry a real BOM so this test actually exercises it.
        """
        pages = parse_internal_html(FIXTURES / "internal_html.csv")
        self.assertTrue(pages, "BOM handling broke: no pages parsed at all")
        self.assertTrue(all(p["url"] for p in pages))
        self.assertTrue(all(p["url"].startswith("https://") for p in pages))

    def test_missing_file_returns_empty_rather_than_raising(self):
        """--skip-empty means SF may not write a file at all."""
        self.assertEqual(parse_internal_html(FIXTURES / "does_not_exist.csv"), [])


class TestParseInlinks(unittest.TestCase):
    def test_only_content_and_broken_edges_are_persisted(self):
        """
        The storage decision the whole feature rests on. On a real crawl,
        174,244 hyperlink edges reduced to 157 content edges -- 87% were
        Navigation, i.e. the same menu repeated once per page. Error edges are
        kept regardless of position because a 404 linked only from the nav is
        still a real bug.
        """
        edges, _ = parse_inlinks(FIXTURES / "all_inlinks.csv")
        kept = {(e["source_url"], e["dest_url"]) for e in edges}

        self.assertIn(("https://example.com/", "https://example.com/pricing"), kept)
        self.assertIn(("https://example.com/pricing", "https://example.com/gone"), kept)
        self.assertIn(("https://example.com/about", "https://example.com/missing"), kept)
        self.assertNotIn(("https://example.com/", "https://example.com/about"), kept)
        self.assertNotIn(("https://example.com/pricing", "https://example.com/about"), kept)
        # 1 content + 2 broken + 1 auth-protected. The self-link row and the
        # plain nav/aside rows are counted but not stored.
        self.assertEqual(len(edges), 4)
        self.assertEqual(sorted(e["reason"] for e in edges),
                         ["auth", "content", "error", "error"])

    def test_javascript_links_are_never_edges(self):
        edges, _ = parse_inlinks(FIXTURES / "all_inlinks.csv")
        self.assertTrue(all(e["dest_url"] != "https://example.com/app.js" for e in edges))

    def test_anchor_text_is_captured_for_content_edges(self):
        edges, _ = parse_inlinks(FIXTURES / "all_inlinks.csv")
        pricing = next(e for e in edges if e["dest_url"] == "https://example.com/pricing")
        self.assertEqual(pricing["anchor"], "See our pricing")
        self.assertEqual(pricing["reason"], "content")

    def test_broken_edges_are_tagged_error_whatever_their_position(self):
        edges, _ = parse_inlinks(FIXTURES / "all_inlinks.csv")
        nav_404 = next(e for e in edges if e["dest_url"] == "https://example.com/missing")
        self.assertEqual(nav_404["reason"], "error")
        self.assertEqual(nav_404["link_position"], "Navigation")
        self.assertEqual(nav_404["dest_status_code"], 404)

    def test_discarded_edges_survive_as_per_page_counts(self):
        """
        Nav and aside edges get no rows, but must not vanish: the prompt needs
        them counted separately, not ignored.
        """
        _, counts = parse_inlinks(FIXTURES / "all_inlinks.csv")

        self.assertEqual(counts["https://example.com/pricing"]["content"], 1)
        self.assertEqual(counts["https://example.com/pricing"]["non_content"], 0)
        # /about is linked from nav (home) and from an aside (pricing)
        self.assertEqual(counts["https://example.com/about"]["content"], 0)
        self.assertEqual(counts["https://example.com/about"]["non_content"], 2)

    def test_counts_ignore_non_hyperlink_types(self):
        """The JavaScript row must not inflate any count."""
        _, counts = parse_inlinks(FIXTURES / "all_inlinks.csv")
        self.assertNotIn("https://example.com/app.js", counts)

    def test_missing_file_returns_empty_rather_than_raising(self):
        edges, counts = parse_inlinks(FIXTURES / "does_not_exist.csv")
        self.assertEqual(edges, [])
        self.assertEqual(counts, {})

    def test_self_links_are_not_counted_as_inbound_links(self):
        """
        From the first real crawl (nrankai.com): the home page linked to itself
        twice via in-page anchors, and both counted as content inlinks. A page
        linking to itself has no inbound link from anywhere -- counting them
        inflates the exact number the prompt's score depends on.
        """
        edges, counts = parse_inlinks(FIXTURES / "all_inlinks.csv")

        self_edges = [e for e in edges if e["source_url"] == e["dest_url"]]
        self.assertEqual(self_edges, [], "a self-link was stored as an edge")
        self.assertEqual(counts.get("https://example.com/", {}).get("content", 0), 0)

    def test_auth_protected_destinations_are_not_called_broken(self):
        """
        Also from the real crawl: app.nrankai.com returned 401 because it sits
        behind BasicAuth. The page exists -- reporting it as a broken link
        would put a false finding in an SEO report.
        """
        edges, _ = parse_inlinks(FIXTURES / "all_inlinks.csv")

        app_edge = next(e for e in edges if e["dest_url"] == "https://app.example.com/")
        self.assertEqual(app_edge["reason"], "auth")
        self.assertEqual(app_edge["dest_status_code"], 401)


if __name__ == "__main__":
    unittest.main()
