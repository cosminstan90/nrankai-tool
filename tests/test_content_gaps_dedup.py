"""
Pins the fix from Etapa 5.2 of the consolidation (docs/CONSOLIDATION_PLAN.md):
deduplicate_gaps() in api/routes/content_gaps.py merged similar-topic gaps
via existing["sources"].extend(gap["sources"]) -- but every gap generator
(collect_gap_signals) sets only a singular "source" key; "sources" (plural,
list) is only created for the first occurrence of a topic (the "existing"
side of the merge), never on the incoming duplicate. So the very scenario
this function exists for -- multiple sources agreeing on the same gap --
crashed with KeyError: 'sources' on every call, silently swallowed by the
bare except in analyze_content_gaps_task's background task.
"""
import unittest

from api.routes.content_gaps import deduplicate_gaps


class TestDeduplicateGaps(unittest.TestCase):
    def test_merges_similar_topics_from_different_sources_without_crashing(self):
        gaps = [
            {"topic": "best crm software", "source": "geo_monitor", "confidence": 0.6},
            {"topic": "best crm software!", "source": "citation_tracker", "confidence": 0.5},
        ]
        result = deduplicate_gaps(gaps)
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0]["sources"]), {"geo_monitor", "citation_tracker"})

    def test_confidence_boosted_on_merge(self):
        gaps = [
            {"topic": "best crm software", "source": "geo_monitor", "confidence": 0.6},
            {"topic": "best crm software!", "source": "citation_tracker", "confidence": 0.5},
        ]
        result = deduplicate_gaps(gaps)
        self.assertAlmostEqual(result[0]["confidence"], 0.7)

    def test_three_way_merge(self):
        gaps = [
            {"topic": "best crm software", "source": "geo_monitor", "confidence": 0.6},
            {"topic": "best crm software!", "source": "citation_tracker", "confidence": 0.5},
            {"topic": "best crm software.", "source": "competitor", "confidence": 0.4},
        ]
        result = deduplicate_gaps(gaps)
        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0]["sources"]), {"geo_monitor", "citation_tracker", "competitor"})

    def test_dissimilar_topics_stay_separate(self):
        gaps = [
            {"topic": "best crm software", "source": "geo_monitor", "confidence": 0.6},
            {"topic": "completely different topic here", "source": "manual", "confidence": 0.3},
        ]
        result = deduplicate_gaps(gaps)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["sources"], ["geo_monitor"])
        self.assertEqual(result[1]["sources"], ["manual"])


if __name__ == "__main__":
    unittest.main()
