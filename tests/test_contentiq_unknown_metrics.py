"""
ContentIQ must not treat missing data as zero.

AHREFS_API_KEY is not configured on this install, so ahrefs_backlinks and
ahrefs_dr arrive as None, and GSC is often not connected either. Three
consumers collapsed None to 0 with `or 0`:

  * E-E-A-T gives 60 of its 100 points to backlinks and DR, so every page was
    capped at 40 and its reason claimed "0 backlinks, DR=0" as if measured.
  * The verdict engine's DELETE rule needs "no traffic, no backlinks". Unknown
    became "none", so a thin page nobody had measured could be recommended
    for deletion with a reason asserting facts nobody checked.
  * The content brief handed the LLM "0 backlinks".

The rule everywhere: None means not measured, and not measured is never
evidence of absence. ContentIQ has no pages yet, so no real verdict was ever
affected -- this closes it before one is.
"""
import unittest

from api.workers.contentiq.engines.eeat import score_eeat
from api.workers.contentiq.verdict import assign_verdict


def _page(**kw):
    base = {"word_count": 1200, "last_modified": None,
            "ahrefs_backlinks": None, "ahrefs_dr": None,
            "gsc_clicks": None, "ahrefs_traffic": None}
    base.update(kw)
    return base


class TestEeatUnknownAuthority(unittest.TestCase):
    def test_unmeasured_authority_is_not_scored_as_zero(self):
        score, reason = score_eeat(_page())
        # depth 18/25 + unknown freshness 5/15 over the 40 points that were
        # measurable, rescaled -- not capped at 23/100 by two fake zeros.
        self.assertGreater(score, 40)
        self.assertNotIn("0 backlinks", reason)
        self.assertIn("not measured", reason)

    def test_measured_zero_still_scores_zero(self):
        """A page that really has no backlinks must still be penalised."""
        unknown, _ = score_eeat(_page())
        measured_zero, reason = score_eeat(_page(ahrefs_backlinks=0, ahrefs_dr=0))
        self.assertLess(measured_zero, unknown)
        self.assertIn("0 backlinks", reason)

    def test_measured_authority_scores_as_before(self):
        score, _ = score_eeat(_page(ahrefs_backlinks=60, ahrefs_dr=55, word_count=2500))
        self.assertEqual(score, 35 + 25 + 25 + 5)


class TestVerdictUnknownSignals(unittest.TestCase):
    def test_thin_unmeasured_page_is_never_recommended_for_deletion(self):
        """Deleting a page on the strength of data nobody fetched is the costliest mistake available."""
        verdict, reason = assign_verdict(_page(word_count=80, score_total=10, score_seo_health=10))
        self.assertNotEqual(verdict, "DELETE")
        self.assertNotIn("no backlinks", reason.lower())
        self.assertIn("not measured", reason)   # says why it was not deleted

    def test_thin_page_with_measured_absence_can_still_be_deleted(self):
        verdict, _ = assign_verdict(_page(
            word_count=80, score_total=10, score_seo_health=10,
            gsc_clicks=0, ahrefs_traffic=0, ahrefs_backlinks=0,
        ))
        self.assertEqual(verdict, "DELETE")

    def test_unknown_traffic_does_not_trigger_the_no_traffic_consolidation(self):
        verdict, reason = assign_verdict(_page(score_total=30, score_seo_health=50))
        self.assertNotIn("no meaningful traffic", reason.lower())

    def test_measured_traffic_behaves_as_before(self):
        verdict, _ = assign_verdict(_page(score_total=40, score_seo_health=50, gsc_clicks=500))
        self.assertEqual(verdict, "UPDATE")


class TestBriefUnknownMetrics(unittest.TestCase):
    def test_brief_says_unknown_rather_than_zero(self):
        from api.workers.contentiq.brief import _build_user_prompt
        prompt = _build_user_prompt({"url": "https://ing.ro/x", "verdict": "UPDATE",
                                     "ahrefs_backlinks": None, "gsc_clicks": None}, "ing.ro")
        self.assertNotIn("Backlinks: 0", prompt)
        self.assertIn("not measured", prompt)


if __name__ == "__main__":
    unittest.main()
