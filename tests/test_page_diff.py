"""
Etapa 6 of docs/IMPROVEMENTS_PLAN.md -- the SEO diff between two snapshots.

The plan is explicit that this must not be a raw text diff: the question being
answered is "the client changed the page, what did that break?", so the output
is a list of SEO-meaningful changes, each with a severity a human can triage.
"""
import unittest
from pathlib import Path

from core.page_diff import diff_snapshots
from core.page_snapshot import extract_page_fields

FIXTURES = Path(__file__).parent / "fixtures" / "pages"


def _snap(name: str) -> dict:
    html = (FIXTURES / name).read_text(encoding="utf-8")
    return extract_page_fields(html, "https://ing.ro/credit")


class TestDiffSnapshots(unittest.TestCase):
    def setUp(self):
        self.changes = diff_snapshots(_snap("credit_v1.html"), _snap("credit_v2.html"))
        self.kinds = {c["kind"] for c in self.changes}

    def test_identical_snapshots_produce_no_changes(self):
        self.assertEqual(diff_snapshots(_snap("credit_v1.html"), _snap("credit_v1.html")), [])

    def test_detects_a_rewritten_h1(self):
        change = next(c for c in self.changes if c["kind"] == "h1_changed")
        self.assertIn("Credit ipotecar cu dobandă fixă", change["before"])
        self.assertIn("Credit ipotecar cu dobândă fixă 2026", change["after"])

    def test_detects_an_added_heading(self):
        added = next(c for c in self.changes if c["kind"] == "h2_added")
        self.assertIn("Întrebări frecvente", added["after"])

    def test_detects_internal_links_added_and_removed(self):
        removed = next(c for c in self.changes if c["kind"] == "internal_links_removed")
        added = next(c for c in self.changes if c["kind"] == "internal_links_added")
        self.assertIn("/persoane-fizice/simulator", removed["before"])
        self.assertIn("/persoane-fizice/dobanzi", added["after"])

    def test_detects_an_image_that_lost_its_alt_text(self):
        self.assertIn("images_without_alt_increased", self.kinds)

    def test_detects_added_schema(self):
        change = next(c for c in self.changes if c["kind"] == "schema_added")
        self.assertIn("FAQPage", change["after"])

    def test_reports_the_word_count_drop(self):
        change = next(c for c in self.changes if c["kind"] == "word_count_dropped")
        self.assertGreater(change["before"], change["after"])

    def test_every_change_carries_a_severity(self):
        self.assertTrue(all(c["severity"] in ("high", "medium", "low") for c in self.changes))

    def test_losing_content_outranks_gaining_a_heading(self):
        """Triage only works if severity actually separates the findings."""
        by_kind = {c["kind"]: c["severity"] for c in self.changes}
        self.assertEqual(by_kind["internal_links_removed"], "high")
        self.assertEqual(by_kind["h2_added"], "low")

    def test_uncaptured_fields_are_never_reported_as_removed(self):
        """
        The scraper stores document.body, so title is None on both sides for
        every real page. None -> None must produce nothing; reporting "title
        removed" on every page would bury the real findings.
        """
        before = {**_snap("credit_v1.html"), "title": None}
        after = {**_snap("credit_v1.html"), "title": None}
        self.assertEqual(diff_snapshots(before, after), [])

    def test_a_field_going_from_unknown_to_known_is_not_a_change(self):
        """
        Happens the first time head metadata starts being captured: the old
        snapshot has None because nobody looked, not because the page lacked a
        title. Calling that "title added" would flag every page at once.
        """
        before = {**_snap("credit_v1.html"), "title": None}
        after = {**_snap("credit_v1.html"), "title": "Credit ipotecar"}
        self.assertEqual(diff_snapshots(before, after), [])

    def test_a_real_title_rewrite_is_reported(self):
        before = {**_snap("credit_v1.html"), "title": "Credit ipotecar"}
        after = {**_snap("credit_v1.html"), "title": "Credite 2026"}
        change = next(c for c in diff_snapshots(before, after) if c["kind"] == "title_changed")
        self.assertEqual(change["severity"], "high")


if __name__ == "__main__":
    unittest.main()
