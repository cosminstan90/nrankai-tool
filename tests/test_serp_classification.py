"""
Pins the fix from Etapa 4.2 of the consolidation (docs/CONSOLIDATION_PLAN.md):
api/workers/gsc_fanout_crossref.py and api/routes/fanout.py's validate_serp
each independently computed a 4-way ai_found/serp_found matrix (synced/
ai_gap/ai_only/double_gap) with ai_found hardcoded to True "by definition --
these are fan-out queries". That made ai_gap and double_gap structurally
unreachable in both places, since no per-query "the AI cited my brand here"
signal exists anywhere in the data model. api/utils/serp_classification.py
replaces both with a single synced/gap split reflecting what the data
actually supports.
"""
import unittest

from api.utils.serp_classification import classify_serp_presence


class TestSerpClassification(unittest.TestCase):
    def test_splits_synced_and_gap_by_serp_found(self):
        entries = [
            {"query": "q1", "serp_found": True},
            {"query": "q2", "serp_found": False},
            {"query": "q3", "serp_found": False},
        ]
        result = classify_serp_presence(entries)
        self.assertEqual(result["total_queries"], 3)
        self.assertEqual([e["query"] for e in result["synced"]], ["q1"])
        self.assertEqual([e["query"] for e in result["gap"]], ["q2", "q3"])
        self.assertEqual(result["summary"], {"synced_count": 1, "gap_count": 2})

    def test_preserves_caller_specific_fields(self):
        entries = [{"query": "q1", "serp_found": True, "target_position": 2, "top_10": ["a.com"]}]
        result = classify_serp_presence(entries)
        self.assertEqual(result["synced"][0]["target_position"], 2)
        self.assertEqual(result["synced"][0]["top_10"], ["a.com"])

    def test_empty_entries(self):
        result = classify_serp_presence([])
        self.assertEqual(result["total_queries"], 0)
        self.assertEqual(result["synced"], [])
        self.assertEqual(result["gap"], [])

    def test_no_ai_gap_or_double_gap_keys(self):
        """The old 4-bucket keys must not resurface -- they were always
        empty because there was no real ai_found signal behind them."""
        result = classify_serp_presence([{"query": "q1", "serp_found": False}])
        self.assertNotIn("ai_gap", result)
        self.assertNotIn("ai_only", result)
        self.assertNotIn("double_gap", result)


class TestCrossrefFanoutGsc(unittest.TestCase):
    def test_classifies_by_gsc_found_flag(self):
        from api.workers.gsc_fanout_crossref import crossref_fanout_gsc

        fanout_queries = ["ranked query", "unranked query"]
        gsc_data = {"ranked query": {"clicks": 10, "found": True}}
        result = crossref_fanout_gsc(fanout_queries, gsc_data, "example.com")

        self.assertEqual(result["summary"], {"synced_count": 1, "gap_count": 1})
        self.assertEqual(result["synced"][0]["query"], "ranked query")
        self.assertEqual(result["gap"][0]["query"], "unranked query")


if __name__ == "__main__":
    unittest.main()
