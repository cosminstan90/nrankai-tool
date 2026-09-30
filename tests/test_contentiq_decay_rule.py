"""
Pasul 15 of docs/superpowers/plans/2026-09-30-next-steps.md.

A page whose static ContentIQ scores look strong (would otherwise KEEP) but
whose real GSC traffic is measurably decaying must not read as a clean
KEEP. content_decay is None when never measured -- that must never be
treated as "not decaying", matching the project's "missing != zero" rule
(tests/test_contentiq_unknown_metrics.py covers the same rule for
backlinks/traffic).
"""
import unittest

from api.workers.contentiq.verdict import assign_verdict


def _strong_page(**kw):
    base = {"score_freshness": 50, "score_geo": 70, "score_eeat": 70,
           "score_seo_health": 70, "score_total": 70,
           "gsc_clicks": 500, "ahrefs_backlinks": 10, "word_count": 1200}
    base.update(kw)
    return base


class TestContentDecayVerdictRule(unittest.TestCase):
    def test_measured_decay_downgrades_an_otherwise_keep_page_to_update(self):
        verdict, reason = assign_verdict(_strong_page(content_decay=True))
        self.assertEqual(verdict, "UPDATE")
        self.assertIn("decaying", reason.lower())

    def test_measured_no_decay_still_keeps(self):
        verdict, _ = assign_verdict(_strong_page(content_decay=False))
        self.assertEqual(verdict, "KEEP")

    def test_unmeasured_decay_does_not_affect_the_verdict(self):
        """content_decay absent/None (insufficient_history) must not be treated as False."""
        verdict, _ = assign_verdict(_strong_page())
        self.assertEqual(verdict, "KEEP")
        verdict, _ = assign_verdict(_strong_page(content_decay=None))
        self.assertEqual(verdict, "KEEP")

    def test_decay_does_not_change_a_verdict_that_was_already_not_keep(self):
        """The rule only intercepts the KEEP path -- a page failing on other grounds keeps its own reason."""
        weak_page = {"score_freshness": 10, "score_geo": 10, "score_eeat": 10,
                    "score_seo_health": 10, "score_total": 10,
                    "gsc_clicks": 0, "ahrefs_traffic": 0, "ahrefs_backlinks": 0,
                    "word_count": 80, "content_decay": True}
        verdict, reason = assign_verdict(weak_page)
        self.assertEqual(verdict, "DELETE")
        self.assertNotIn("decaying", reason.lower())


if __name__ == "__main__":
    unittest.main()
